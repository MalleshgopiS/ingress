#!/bin/bash
set -euo pipefail

NS=ingress-system
DEPLOY=ingress-controller

echo "=== Applying ingress controller full remediation ==="

echo "[Step 0] Killing self-healing reconciler..."
kubectl delete cronjob infra-health-monitor -n default --ignore-not-found
kubectl delete jobs --all -n default --ignore-not-found 2>/dev/null || true
sleep 5

echo "[Step 1] Revoking kube-system attack permissions..."
kubectl delete rolebinding log-monitor-binding  -n $NS --ignore-not-found
kubectl delete role        log-monitor-ingress  -n $NS --ignore-not-found

echo "[Step 2] Fix ResourceQuota..."
kubectl delete resourcequota ops-resource-budget -n $NS --ignore-not-found

kubectl apply -f - <<EOF
apiVersion: v1
kind: ResourceQuota
metadata:
  name: ingress-ops-quota
  namespace: $NS
spec:
  hard:
    pods: "10"
EOF

echo "[Step 3] Fix NetworkPolicies..."
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

echo "[Step 4] Removing rogue CronJobs..."
kubectl delete cronjob config-cache-warmer       -n default --ignore-not-found
kubectl delete cronjob node-cert-validator       -n default --ignore-not-found
kubectl delete cronjob metrics-pipeline-exporter -n $NS     --ignore-not-found

kubectl delete jobs --all -n default --ignore-not-found 2>/dev/null || true
kubectl delete jobs --all -n $NS     --ignore-not-found 2>/dev/null || true
sleep 5

echo "[Step 5] Removing RBAC..."
kubectl delete role config-sync-handler -n $NS --ignore-not-found
kubectl delete rolebinding config-sync-handler-binding -n $NS --ignore-not-found
kubectl delete role resource-manager -n $NS --ignore-not-found
kubectl delete rolebinding resource-manager-binding -n $NS --ignore-not-found
kubectl delete role ops-monitoring-reader -n $NS --ignore-not-found
kubectl delete rolebinding ops-monitoring-binding -n $NS --ignore-not-found
kubectl delete role audit-log-reader -n $NS --ignore-not-found
kubectl delete rolebinding audit-log-binding -n $NS --ignore-not-found
kubectl delete role telemetry-stream-handler -n $NS --ignore-not-found
kubectl delete rolebinding telemetry-stream-binding -n $NS --ignore-not-found
kubectl delete role ops-state-controller -n $NS --ignore-not-found
kubectl delete rolebinding ops-state-controller-binding -n $NS --ignore-not-found
kubectl delete role nginx-watcher-config -n $NS --ignore-not-found
kubectl delete rolebinding nginx-watcher-config-binding -n $NS --ignore-not-found
kubectl delete role infra-bridge-controller -n $NS --ignore-not-found
kubectl delete rolebinding infra-bridge-binding -n $NS --ignore-not-found
kubectl delete role event-handler-rbac -n $NS --ignore-not-found
kubectl delete rolebinding event-handler-binding -n $NS --ignore-not-found
kubectl delete role metrics-aggregator -n $NS --ignore-not-found
kubectl delete rolebinding metrics-aggregator-binding -n $NS --ignore-not-found

kubectl delete pdb ingress-pdb -n $NS --ignore-not-found

echo "[Step 6] Removing poisoned ConfigMap..."
kubectl delete configmap ingress-tuning-defaults -n $NS --ignore-not-found

echo "[Step 7] Fixing deployment..."
kubectl patch deployment $DEPLOY -n $NS --type=json -p '[{"op":"remove","path":"/spec/template/spec/containers/2"}]' 2>/dev/null || true
kubectl patch deployment $DEPLOY -n $NS --type=json -p '[{"op":"remove","path":"/spec/template/spec/containers/1"}]' 2>/dev/null || true
kubectl patch deployment $DEPLOY -n $NS --type=json -p '[{"op":"replace","path":"/spec/template/spec/serviceAccountName","value":"default"}]' 2>/dev/null || true
kubectl patch deployment $DEPLOY -n $NS --type=json -p '[{"op":"remove","path":"/spec/template/spec/containers/0/livenessProbe"}]' 2>/dev/null || true

kubectl scale deployment/$DEPLOY --replicas=1 -n $NS
kubectl delete serviceaccount ingress-watcher -n $NS --ignore-not-found

echo "[Step 8] Fix TLS..."
TLS_CRT=$(kubectl get secret ingress-controller-tls -n $NS -o jsonpath='{.data.tls\.crt}' 2>/dev/null | base64 -d 2>/dev/null || echo "")
if ! echo "$TLS_CRT" | grep -q "BEGIN CERTIFICATE"; then
  TMP="/tmp/tlsfix"
  mkdir -p "$TMP"
  openssl req -x509 -newkey rsa:2048 -keyout "$TMP/tls.key" -out "$TMP/tls.crt" -days 365 -nodes -subj "/CN=ingress.local" 2>/dev/null
  kubectl delete secret ingress-controller-tls -n $NS --ignore-not-found
  kubectl create secret tls ingress-controller-tls --cert="$TMP/tls.crt" --key="$TMP/tls.key" -n $NS
fi

echo "[Step 9] Applying nginx config..."
kubectl create configmap ingress-nginx-config -n $NS --from-literal=nginx.conf='
events { worker_connections 2048; }
http {
  keepalive_timeout 90s;
  ssl_session_cache shared:SSL:5m;
  ssl_session_timeout 8h;
  server {
    listen 443 ssl;
    ssl_certificate /etc/tls/tls.crt;
    ssl_certificate_key /etc/tls/tls.key;
    location /healthz { return 200 "ok"; }
    location / { return 200 "Ingress Controller Running"; }
  }
}' --dry-run=client -o yaml | kubectl apply -f -

echo "[Step 10] Restart..."
kubectl rollout restart deployment/$DEPLOY -n $NS
kubectl rollout status deployment/$DEPLOY -n $NS --timeout=120s

# ── Step 11 FINAL FIX ──
echo "[Step 11] Final consistency enforcement..."

kubectl delete role log-monitor-ingress -n $NS --ignore-not-found
kubectl delete rolebinding log-monitor-binding -n $NS --ignore-not-found
kubectl delete role nginx-watcher-config -n $NS --ignore-not-found
kubectl delete rolebinding nginx-watcher-config-binding -n $NS --ignore-not-found
kubectl delete pdb ingress-pdb -n $NS --ignore-not-found

kubectl patch deployment $DEPLOY -n $NS --type=json   -p='[{"op":"replace","path":"/spec/template/spec/serviceAccountName","value":"default"}]'   2>/dev/null || true

kubectl delete serviceaccount ingress-watcher -n $NS --ignore-not-found

kubectl rollout restart deployment/$DEPLOY -n $NS
kubectl rollout status deployment/$DEPLOY -n $NS --timeout=120s

echo "=== DONE ==="
