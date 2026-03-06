#!/usr/bin/env bash
set -euo pipefail

export KUBECONFIG=/etc/rancher/k3s/k3s.yaml

NS="ingress-system"
CFG="/tmp/nginx.conf"

echo "Fetching nginx config..."
kubectl get configmap ingress-nginx-config -n "$NS" \
  -o jsonpath='{.data.nginx\.conf}' > "$CFG"

echo "Patching keepalive timeout..."
sed -i 's/keepalive_timeout 0;/keepalive_timeout 65;/' "$CFG"

echo "Updating configmap..."
kubectl create configmap ingress-nginx-config \
  -n "$NS" \
  --from-file=nginx.conf="$CFG" \
  --dry-run=client -o yaml | kubectl apply -f -

echo "Restarting deployment..."
kubectl rollout restart deployment/ingress-controller -n "$NS"
kubectl rollout status deployment/ingress-controller -n "$NS" --timeout=180s

echo "Waiting for stabilization..."
sleep 10

echo "Ingress controller repaired."
