#!/bin/bash
set -e

NS=default
DEPLOY=ingress-controller

echo "Fetching nginx config..."
kubectl get configmap ingress-nginx-config -n $NS -o jsonpath='{.data.nginx\.conf}' > nginx.conf

echo "Fixing keepalive timeout..."
sed -i 's/keepalive_timeout[[:space:]]\+0s\?;/keepalive_timeout 65s;/g' nginx.conf

echo "Updating configmap..."
kubectl create configmap ingress-nginx-config \
  --from-file=nginx.conf \
  -n $NS -o yaml --dry-run=client | kubectl apply -f -

echo "Restarting deployment..."
kubectl rollout restart deployment $DEPLOY -n $NS
kubectl rollout status deployment $DEPLOY -n $NS

echo "Waiting for stabilization..."
sleep 10

echo "Ingress controller repaired."