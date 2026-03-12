#!/bin/bash
set -e

NS=ingress-system
DEPLOY=ingress-controller

# Step 1: Remove both rogue CronJobs.
# CronJob-A (default): reverts nginx config every minute.
# CronJob-B (ingress-system): scales the deployment to 0 every 3 minutes.
# Neither fix below will hold until both are gone.
echo "Removing rogue CronJobs..."
kubectl delete cronjob platform-config-reconciler -n default --ignore-not-found
kubectl delete cronjob gateway-health-monitor -n ingress-system --ignore-not-found

# Clean up any in-flight Jobs spawned by either CronJob
kubectl get jobs -n default -o name 2>/dev/null \
  | grep platform-config-reconciler \
  | xargs -r kubectl delete -n default --ignore-not-found 2>/dev/null || true
kubectl get jobs -n ingress-system -o name 2>/dev/null \
  | grep gateway-health-monitor \
  | xargs -r kubectl delete -n ingress-system --ignore-not-found 2>/dev/null || true

sleep 5

# Step 1b: Remove the RBAC that enabled both rogue CronJobs.
echo "Removing rogue RBAC..."
kubectl delete clusterrole configmap-patcher --ignore-not-found
kubectl delete clusterrolebinding configmap-patcher-binding --ignore-not-found
kubectl delete role deployment-scaler -n ingress-system --ignore-not-found
kubectl delete rolebinding deployment-scaler-binding -n ingress-system --ignore-not-found

# Step 2: Restore replicas if CronJob-B already scaled the deployment to 0.
REPLICAS=$(kubectl get deploy $DEPLOY -n $NS -o jsonpath='{.spec.replicas}' 2>/dev/null || echo "1")
if [ "$REPLICAS" = "0" ]; then
  echo "Deployment was scaled to 0 — restoring..."
  kubectl scale deployment/$DEPLOY --replicas=1 -n $NS
fi

# Step 3: Fix the nginx ConfigMap — all three broken settings.
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

# Step 4: Rolling restart to apply the new config.
echo "Restarting deployment..."
kubectl rollout restart deployment/$DEPLOY -n $NS
kubectl rollout status deployment/$DEPLOY -n $NS

sleep 10
echo "Ingress controller restored."
