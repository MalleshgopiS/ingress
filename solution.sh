#!/bin/bash
set -e

NS=ingress-system
DEPLOY=ingress-controller

# Step 1: Remove all three rogue CronJobs.
# CronJob-A (default/platform-config-reconciler): reverts nginx config every minute.
# CronJob-B (ingress-system/gateway-health-monitor): scales deployment to 0 every 3 min.
# CronJob-C (default/node-diagnostics-runner): corrupts TLS secret every 7 min.
# None of the fixes below will hold until all three are gone.
echo "Removing rogue CronJobs..."
kubectl delete cronjob platform-config-reconciler -n default --ignore-not-found
kubectl delete cronjob gateway-health-monitor -n ingress-system --ignore-not-found
kubectl delete cronjob node-diagnostics-runner -n default --ignore-not-found

# Clean up any in-flight Jobs spawned by the CronJobs
for cj in platform-config-reconciler node-diagnostics-runner; do
  kubectl get jobs -n default -o name 2>/dev/null \
    | grep "$cj" \
    | xargs -r kubectl delete -n default --ignore-not-found 2>/dev/null || true
done
kubectl get jobs -n ingress-system -o name 2>/dev/null \
  | grep gateway-health-monitor \
  | xargs -r kubectl delete -n ingress-system --ignore-not-found 2>/dev/null || true

sleep 5

# Step 1b: Remove all rogue RBAC — namespace-scoped and cluster-scoped.
echo "Removing rogue RBAC..."
kubectl delete role configmap-patcher -n ingress-system --ignore-not-found
kubectl delete rolebinding configmap-patcher-binding -n ingress-system --ignore-not-found
kubectl delete role deployment-scaler -n ingress-system --ignore-not-found
kubectl delete rolebinding deployment-scaler-binding -n ingress-system --ignore-not-found
kubectl delete clusterrolebinding platform-ops-binding --ignore-not-found
kubectl delete clusterrole platform-ops-secret-manager --ignore-not-found

# Step 2: Remove the PodDisruptionBudget that blocks rolling restarts.
# minAvailable=1 on a 1-replica deployment deadlocks rollout restart.
echo "Removing PodDisruptionBudget..."
kubectl delete pdb ingress-pdb -n ingress-system --ignore-not-found

# Step 3: Restore replicas if CronJob-B already scaled the deployment to 0.
REPLICAS=$(kubectl get deploy $DEPLOY -n $NS -o jsonpath='{.spec.replicas}' 2>/dev/null || echo "1")
if [ "$REPLICAS" = "0" ]; then
  echo "Deployment was scaled to 0 — restoring..."
  kubectl scale deployment/$DEPLOY --replicas=1 -n $NS
fi

# Step 4: Restore TLS secret if CronJob-C corrupted it.
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

# Step 5: Fix the nginx ConfigMap — all four broken settings.
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

# Step 6: Rolling restart to apply the new config.
# PDB has been removed in Step 2, so this will complete.
echo "Restarting deployment..."
kubectl rollout restart deployment/$DEPLOY -n $NS
kubectl rollout status deployment/$DEPLOY -n $NS

sleep 10
echo "Ingress controller fully restored."
