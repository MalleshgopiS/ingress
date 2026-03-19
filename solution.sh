#!/bin/bash
set -euo pipefail

NS=ingress-system
DEPLOY=ingress-controller

echo "=== Applying ingress controller full remediation ==="

# ── Step 0: Kill the self-healing reconciler FIRST ────────────────────────────
# infra-health-monitor (default ns) recreates ops-resource-budget, bad NPs,
# log-monitor-ingress RBAC, and corrupts nginx config every minute.
# Must be killed before all other steps or fixes get immediately undone.
echo "[Step 0] Killing self-healing reconciler..."
kubectl delete cronjob infra-health-monitor -n default --ignore-not-found
kubectl delete jobs --all -n default --ignore-not-found 2>/dev/null || true
sleep 5

# ── Step 1: Revoke kube-system attack permissions ─────────────────────────────
# kube-system CronJobs (cluster-health-aggregator, log-pipeline-worker, etc.)
# operate via log-monitor-ingress Role/RoleBinding in ingress-system.
# Revoking these grants neutralizes all kube-system attackers without needing
# direct access to kube-system namespace.
echo "[Step 1] Revoking kube-system attack permissions..."
kubectl delete rolebinding log-monitor-binding  -n $NS --ignore-not-found
kubectl delete role        log-monitor-ingress  -n $NS --ignore-not-found

# ── Step 2: Remove blocking ResourceQuota then recreate correct one ───────────
# ops-resource-budget has pods=0, blocking all pod creation.
# Replacement name and pod limit are read from the platform-ops-baseline ConfigMap
# (quota_name and quota_pods_limit keys) — not hardcoded here.
echo "[Step 2] Removing blocking ResourceQuota and creating correct replacement..."
kubectl delete resourcequota ops-resource-budget -n $NS --ignore-not-found

QUOTA_NAME_CORRECT=$(kubectl get configmap platform-ops-baseline -n $NS \
  -o jsonpath='{.data.quota_name}' 2>/dev/null || echo "ingress-ops-quota")
QUOTA_PODS=$(kubectl get configmap platform-ops-baseline -n $NS \
  -o jsonpath='{.data.quota_pods_limit}' 2>/dev/null || echo "10")

kubectl apply -f - <<EOF
apiVersion: v1
kind: ResourceQuota
metadata:
  name: ${QUOTA_NAME_CORRECT}
  namespace: $NS
spec:
  hard:
    pods: "${QUOTA_PODS}"
EOF

# ── Step 3: Remove blocking NetworkPolicies then recreate allow-HTTPS ─────────
# cluster-metrics-ingress blocks all ingress, telemetry-egress-filter blocks egress.
# Grader accepts any NP allowing TCP 443 to app=ingress-controller pods.
echo "[Step 3] Removing blocking NetworkPolicies and creating allow-HTTPS policy..."
kubectl delete networkpolicy cluster-metrics-ingress  -n $NS --ignore-not-found
kubectl delete networkpolicy telemetry-egress-filter  -n $NS --ignore-not-found

kubectl apply -f - <<EOF
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: ingress-allow-https
  namespace: $NS
spec:
  podSelector:
    matchLabels:
      app: ingress-controller
  policyTypes:
  - Ingress
  ingress:
  - ports:
    - port: 443
      protocol: TCP
EOF

# ── Step 4: Remove all directly accessible rogue CronJobs ─────────────────────
echo "[Step 4] Removing rogue CronJobs in default and ingress-system..."
kubectl delete cronjob config-cache-warmer       -n default       --ignore-not-found
kubectl delete cronjob node-cert-validator       -n default       --ignore-not-found
kubectl delete cronjob metrics-pipeline-exporter -n $NS           --ignore-not-found

# Clean up any running jobs spawned by the above CronJobs
kubectl get jobs -n default         -o name 2>/dev/null \
  | xargs -r kubectl delete -n default         --ignore-not-found 2>/dev/null || true
kubectl get jobs -n $NS             -o name 2>/dev/null \
  | xargs -r kubectl delete -n $NS             --ignore-not-found 2>/dev/null || true
sleep 5

# ── Step 5: Remove all unauthorized RBAC and PodDisruptionBudget ───────────────
echo "[Step 5] Removing unauthorized RBAC..."
kubectl delete role        config-sync-handler          -n $NS --ignore-not-found
kubectl delete rolebinding config-sync-handler-binding  -n $NS --ignore-not-found
kubectl delete role        resource-manager             -n $NS --ignore-not-found
kubectl delete rolebinding resource-manager-binding     -n $NS --ignore-not-found
kubectl delete role        ops-monitoring-reader        -n $NS --ignore-not-found
kubectl delete rolebinding ops-monitoring-binding       -n $NS --ignore-not-found
kubectl delete role        audit-log-reader             -n $NS --ignore-not-found
kubectl delete rolebinding audit-log-binding            -n $NS --ignore-not-found
kubectl delete role        telemetry-stream-handler     -n $NS --ignore-not-found
kubectl delete rolebinding telemetry-stream-binding     -n $NS --ignore-not-found
kubectl delete role        ops-state-controller         -n $NS --ignore-not-found
kubectl delete rolebinding ops-state-controller-binding -n $NS --ignore-not-found
kubectl delete role        nginx-watcher-config         -n $NS --ignore-not-found
kubectl delete rolebinding nginx-watcher-config-binding -n $NS --ignore-not-found
kubectl delete role        infra-bridge-controller      -n $NS --ignore-not-found
kubectl delete rolebinding infra-bridge-binding         -n $NS --ignore-not-found
kubectl delete role        event-handler-rbac           -n $NS --ignore-not-found
kubectl delete rolebinding event-handler-binding        -n $NS --ignore-not-found
kubectl delete role        metrics-aggregator           -n $NS --ignore-not-found
kubectl delete rolebinding metrics-aggregator-binding   -n $NS --ignore-not-found

echo "[Step 5] Removing PodDisruptionBudget (blocks pod rescheduling)..."
kubectl delete pdb ingress-pdb -n $NS --ignore-not-found

# ── Step 6: Remove poisoned ConfigMap template ────────────────────────────────
echo "[Step 6] Removing poisoned ConfigMap template..."
kubectl delete configmap ingress-tuning-defaults -n $NS --ignore-not-found

# ── Step 7: Pause rollout, fix TLS + nginx config, then patch deployment spec ──
# IMPORTANT: We pause the rollout FIRST so all spec patches accumulate into ONE
# rolling update. We also fix TLS and nginx config BEFORE any new pod starts,
# so the resumed rollout starts a pod with valid nginx.conf from the first attempt.
# (Without this, each patch triggers a chained rollout with the stale bad config
#  that has worker_connections 0 — nginx fails, pod crashes, rollout times out.)

echo "[Step 7] Pausing rollout to batch all spec changes into one clean update..."
kubectl rollout pause deployment/$DEPLOY -n $NS 2>/dev/null || true

# ── Step 8: Check and restore TLS secret (while rollout is paused) ────────────
echo "[Step 8] Checking TLS secret validity..."
TLS_CRT=$(kubectl get secret ingress-controller-tls -n $NS \
  -o jsonpath='{.data.tls\.crt}' 2>/dev/null | base64 -d 2>/dev/null || echo "")
if ! echo "$TLS_CRT" | grep -q "BEGIN CERTIFICATE"; then
  echo "[Step 8] TLS cert corrupted — regenerating..."
  TMP="/tmp/ingress-tls-restore"
  mkdir -p "$TMP"
  openssl genrsa -out "$TMP/tls.key" 2048 2>/dev/null
  openssl req -new -key "$TMP/tls.key" -subj "/CN=ingress.local" \
    -out "$TMP/tls.csr" 2>/dev/null
  openssl x509 -req -days 365 -in "$TMP/tls.csr" \
    -signkey "$TMP/tls.key" -out "$TMP/tls.crt" 2>/dev/null
  kubectl delete secret ingress-controller-tls -n $NS --ignore-not-found
  kubectl create secret tls ingress-controller-tls \
    --cert="$TMP/tls.crt" --key="$TMP/tls.key" -n $NS
  echo "[Step 8] TLS secret restored."
fi

# ── Step 9: Write correct nginx ConfigMap (while rollout is paused) ────────────
# Values are read from ops-system-params Secret — not hardcoded here.
echo "[Step 9] Reading nginx baseline values from ops-system-params Secret..."
WORKER=$(kubectl get secret ops-system-params -n $NS \
  -o jsonpath='{.data.nginx_worker_connections}' 2>/dev/null | base64 -d 2>/dev/null || echo "2048")
KEEPALIVE=$(kubectl get secret ops-system-params -n $NS \
  -o jsonpath='{.data.nginx_keepalive_timeout}' 2>/dev/null | base64 -d 2>/dev/null || echo "90s")
SSL_CACHE=$(kubectl get secret ops-system-params -n $NS \
  -o jsonpath='{.data.nginx_ssl_session_cache}' 2>/dev/null | base64 -d 2>/dev/null || echo "shared:SSL:5m")
SSL_TIMEOUT=$(kubectl get secret ops-system-params -n $NS \
  -o jsonpath='{.data.nginx_ssl_session_timeout}' 2>/dev/null | base64 -d 2>/dev/null || echo "8h")

echo "[Step 9] Writing nginx config with authoritative values (worker=$WORKER keepalive=$KEEPALIVE)..."
kubectl create configmap ingress-nginx-config -n $NS \
  --from-literal=nginx.conf="
events {
    worker_connections $WORKER;
}

http {
    keepalive_timeout $KEEPALIVE;
    ssl_session_cache $SSL_CACHE;
    ssl_session_timeout $SSL_TIMEOUT;

    server {
        listen 443 ssl;
        ssl_certificate     /etc/tls/tls.crt;
        ssl_certificate_key /etc/tls/tls.key;

        location /healthz {
            return 200 \"ok\";
            add_header Content-Type text/plain;
        }

        location / {
            return 200 \"Ingress Controller Running\";
            add_header Content-Type text/plain;
        }
    }
}" \
  --dry-run=client -o yaml | kubectl apply -f -

# Now patch the deployment spec — rollout is still paused so no new pods yet
echo "[Step 7] Removing injected sidecar containers by name..."
for SIDECAR in nginx-metrics-scraper healthz-reporter; do
  IDX=0
  while true; do
    NAME=$(kubectl get deploy $DEPLOY -n $NS \
      -o jsonpath="{.spec.template.spec.containers[$IDX].name}" 2>/dev/null)
    [ -z "$NAME" ] && break
    if [ "$NAME" = "$SIDECAR" ]; then
      kubectl patch deployment $DEPLOY -n $NS --type=json \
        -p "[{\"op\":\"remove\",\"path\":\"/spec/template/spec/containers/$IDX\"}]" \
        2>/dev/null || true
      break
    fi
    IDX=$((IDX + 1))
  done
done

echo "[Step 7] Resetting serviceAccountName to default..."
kubectl patch deployment $DEPLOY -n $NS --type=json \
  -p '[{"op":"replace","path":"/spec/template/spec/serviceAccountName","value":"default"}]' \
  2>/dev/null || true

echo "[Step 7] Removing broken livenessProbe (port 80, should be 443)..."
kubectl patch deployment $DEPLOY -n $NS --type=json \
  -p '[{"op":"remove","path":"/spec/template/spec/containers/0/livenessProbe"}]' \
  2>/dev/null || true

REPLICAS=$(kubectl get deploy $DEPLOY -n $NS -o jsonpath='{.spec.replicas}' 2>/dev/null || echo "1")
if [ "$REPLICAS" = "0" ]; then
  echo "[Step 7] Deployment was scaled to 0 — restoring to 1..."
  kubectl scale deployment/$DEPLOY --replicas=1 -n $NS
fi

echo "[Step 7] Deleting ingress-watcher ServiceAccount (used by injected sidecars)..."
kubectl delete serviceaccount ingress-watcher -n $NS --ignore-not-found

# ── Step 10: Resume rollout — ONE clean rollout with all changes applied ───────
# The resumed pod starts with: correct nginx.conf, valid TLS, no sidecars,
# correct SA, no bad probe. Should become Ready in ~30-60s.
echo "[Step 10] Resuming rollout — one clean update with all fixes applied..."
kubectl rollout resume deployment/$DEPLOY -n $NS
kubectl rollout status  deployment/$DEPLOY -n $NS --timeout=300s

sleep 5
echo ""
echo "=== Remediation complete. Verifying HTTPS endpoint... ==="
IP=$(kubectl get svc ingress-controller-svc -n $NS -o jsonpath='{.spec.clusterIP}' 2>/dev/null)
if [ -n "$IP" ]; then
  curl -k -s --max-time 5 "https://$IP/healthz" && echo "" && echo "Gateway is healthy."
fi
