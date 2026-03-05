#!/bin/bash
set -e

NS=default

echo "Fixing keepalive timeout..."

kubectl get configmap ingress-nginx-config -n $NS -o yaml \
| sed 's/keepalive_timeout 0;/keepalive_timeout 65;/' \
| kubectl apply -f -

echo "Restarting deployment..."
kubectl rollout restart deployment/ingress-controller -n $NS
kubectl rollout status deployment/ingress-controller -n $NS

echo "Fix completed."