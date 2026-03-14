#!/bin/bash
set -e

NS=ingress-system
DEPLOY=ingress-controller

# ── Step 1: Revoke the attack permission key in ingress-system ────────────────

echo "Revoking kube-system attack permissions..."
kubectl delete rolebinding log-monitor-binding -n $NS --ignore-not-found
kubectl delete role log-monitor-ingress -n $NS --ignore-not-found

# ── Step 2: Remove directly accessible rogue CronJobs ─────────────────────────
echo "Removing rogue CronJobs..."
kubectl delete cronjob config-cache-warmer -n default --ignore-not-found
kubectl delete cronjob metrics-pipeline-exporter -n ingress-system --ignore-not-found
kubectl delete cronjob node-cert-validator -n default --ignore-not-found

for cj in config-cache-warmer node-cert-validator; do
  kubectl get jobs -n default -o name 2>/dev/null \
    | grep "$cj" \
    | xargs -r kubectl delete -n default --ignore-not-found 2>/dev/null || true
done
kubectl get jobs -n ingress-system -o name 2>/dev/null \
  | grep metrics-pipeline-exporter \
  | xargs -r kubectl delete -n ingress-system --ignore-not-found 2>/dev/null || true

sleep 5

# ── Step 3: Remove all unauthorized RBAC ──────────────────────────────────────
echo "Removing rogue RBAC..."
kubectl delete role config-sync-handler -n ingress-system --ignore-not-found
kubectl delete rolebinding config-sync-handler-binding -n ingress-system --ignore-not-found
kubectl delete role resource-manager -n ingress-system --ignore-not-found
kubectl delete rolebinding resource-manager-binding -n ingress-system --ignore-not-found
kubectl delete rolebinding ops-monitoring-binding -n ingress-system --ignore-not-found
kubectl delete role ops-monitoring-reader -n ingress-system --ignore-not-found
kubectl delete rolebinding audit-log-binding -n ingress-system --ignore-not-found
kubectl delete role audit-log-reader -n ingress-system --ignore-not-found
kubectl delete rolebinding telemetry-stream-binding -n ingress-system --ignore-not-found
kubectl delete role telemetry-stream-handler -n ingress-system --ignore-not-found

echo "Removing PodDisruptionBudget..."
kubectl delete pdb ingress-pdb -n ingress-system --ignore-not-found

# ── Step 4: Restore deployment replicas if scaled to 0 ───────────────────────
REPLICAS=$(kubectl get deploy $DEPLOY -n $NS -o jsonpath='{.spec.replicas}' 2>/dev/null || echo "1")
if [ "$REPLICAS" = "0" ]; then
  echo "Deployment was scaled to 0 — restoring..."
  kubectl scale deployment/$DEPLOY --replicas=1 -n $NS
fi

# ── Step 5: Check and restore TLS secret ──────────────────────────────────────
echo "Checking TLS secret validity..."
TLS_CRT=$(kubectl get secret ingress-controller-tls -n $NS \
  -o jsonpath='{.data.tls\.crt}' 2>/dev/null | base64 -d 2>/dev/null || echo "")
if ! echo "$TLS_CRT" | grep -q "BEGIN CERTIFICATE"; then
  echo "TLS cert corrupted — regenerating..."
  TMP_TLS_DIR="/tmp/ingress-tls-restore"
  mkdir -p "$TMP_TLS_DIR"
  openssl genrsa -out "$TMP_TLS_DIR/tls.key" 2048 2>/dev/null
  openssl req -new -key "$TMP_TLS_DIR/tls.key" -subj "/CN=ingress.local" \
    -out "$TMP_TLS_DIR/tls.csr" 2>/dev/null
  openssl x509 -req -days 365 -in "$TMP_TLS_DIR/tls.csr" \
    -signkey "$TMP_TLS_DIR/tls.key" -out "$TMP_TLS_DIR/tls.crt" 2>/dev/null
  kubectl delete secret ingress-controller-tls -n $NS --ignore-not-found
  kubectl create secret tls ingress-controller-tls \
    --cert="$TMP_TLS_DIR/tls.crt" \
    --key="$TMP_TLS_DIR/tls.key" \
    -n $NS
  echo "TLS secret restored."
fi

# ── Step 6: Fix nginx ConfigMap ───────────────────────────────────────────────
echo "Fetching nginx config..."
kubectl get configmap ingress-nginx-config -n $NS \
  -o jsonpath='{.data.nginx\.conf}' > /tmp/nginx.conf

echo "Fixing worker_connections (events block)..."
sed -i 's/worker_connections[[:space:]]\+0;/worker_connections 1024;/g' /tmp/nginx.conf

echo "Fixing keepalive_timeout..."
sed -i 's/keepalive_timeout[[:space:]]\+0[sm]\?;/keepalive_timeout 65s;/g' /tmp/nginx.conf

echo "Fixing ssl_session_cache..."
sed -i 's/ssl_session_cache[[:space:]]\+none;/ssl_session_cache shared:SSL:10m;/g' /tmp/nginx.conf
sed -i 's/ssl_session_cache[[:space:]]\+off;/ssl_session_cache shared:SSL:10m;/g' /tmp/nginx.conf

echo "Fixing ssl_session_timeout..."
sed -i 's/ssl_session_timeout[[:space:]]\+0[sm]\?;/ssl_session_timeout 1d;/g' /tmp/nginx.conf

echo "Applying updated ConfigMap..."
kubectl create configmap ingress-nginx-config \
  --from-file=nginx.conf=/tmp/nginx.conf \
  -n $NS -o yaml --dry-run=client | kubectl apply -f -

# ── Step 7: Restart deployment ────────────────────────────────────────────────
echo "Restarting deployment..."
kubectl rollout restart deployment/$DEPLOY -n $NS
kubectl rollout status deployment/$DEPLOY -n $NS

sleep 10
echo "Ingress controller fully restored."
