#!/bin/bash
set -e

NS=ingress-system
DEPLOY=ingress-controller

# Step 1: Remove the rogue CronJob that keeps reverting the config.
# Without this, any config fix will be undone within 2 minutes.
echo "Removing rogue CronJob..."
kubectl delete cronjob platform-config-reconciler -n default --ignore-not-found

# Clean up any in-flight Jobs spawned by the CronJob
kubectl get jobs -n default -o name 2>/dev/null \
  | grep platform-config-reconciler \
  | xargs -r kubectl delete -n default --ignore-not-found 2>/dev/null || true

sleep 5

# Step 2: Fix the nginx ConfigMap — all three broken settings.
echo "Fetching nginx config..."
kubectl get configmap ingress-nginx-config -n $NS \
  -o jsonpath='{.data.nginx\.conf}' > /tmp/nginx.conf

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

# Step 3: Rolling restart to apply the new config.
echo "Restarting deployment..."
kubectl rollout restart deployment/$DEPLOY -n $NS
kubectl rollout status deployment/$DEPLOY -n $NS

sleep 10
echo "Ingress controller restored."
