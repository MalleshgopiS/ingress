#!/usr/bin/env bash
set -e

NS="ingress-system"

echo "Fixing TLS keepalive configuration..."

kubectl get configmap ingress-nginx-config -n $NS -o yaml \
| sed 's/keepalive_timeout 0;/keepalive_timeout 65;/' \
| kubectl apply -f -

echo "Restarting ingress deployment..."

kubectl rollout restart deployment ingress-controller -n $NS

kubectl rollout status deployment ingress-controller -n $NS --timeout=180s

echo "Fix applied."