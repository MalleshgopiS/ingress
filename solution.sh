#!/usr/bin/env bash
set -e

NS="bleater"

echo "Fixing nginx keepalive timeout..."

kubectl get configmap ingress-nginx-config -n $NS -o yaml \
| sed -E 's/keepalive_timeout[[:space:]]+0;/keepalive_timeout 65;/' \
| kubectl apply -f -

echo "Restarting deployment..."

kubectl rollout restart deployment ingress-controller -n $NS

kubectl rollout status deployment ingress-controller -n $NS --timeout=180s

echo "Fix complete."