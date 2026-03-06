#!/usr/bin/env bash
set -euo pipefail

export KUBECONFIG=/etc/rancher/k3s/k3s.yaml

NS="ingress-system"
CFG="/tmp/ingress-nginx.conf"

kubectl get configmap ingress-nginx-config -n "$NS" -o jsonpath='{.data.nginx\.conf}' > "$CFG"

sed -i 's/keepalive_timeout 0;/keepalive_timeout 65;/' "$CFG"

kubectl create configmap ingress-nginx-config \
  -n "$NS" \
  --from-file=nginx.conf="$CFG" \
  --from-literal=ssl-session-timeout=0 \
  --dry-run=client \
  -o yaml | kubectl apply -f -

kubectl rollout restart deployment/ingress-controller -n "$NS"
kubectl rollout status deployment/ingress-controller -n "$NS" --timeout=180s

echo "Ingress controller repaired."
