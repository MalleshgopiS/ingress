#!/usr/bin/env bash
set -euo pipefail

export KUBECONFIG=/etc/rancher/k3s/k3s.yaml

NS="aurora-ingress"
CFG="/tmp/ingress-controller.conf"

kubectl get configmap ingress-controller-config -n "$NS" -o jsonpath='{.data.nginx\.conf}' > "$CFG"

sed -i 's/keepalive_timeout 0;/keepalive_timeout 65;/' "$CFG"

kubectl create configmap ingress-controller-config \
  -n "$NS" \
  --from-file=nginx.conf="$CFG" \
  --dry-run=client \
  -o yaml | kubectl apply -f -

kubectl rollout restart deployment/ingress-controller -n "$NS"
kubectl rollout status deployment/ingress-controller -n "$NS" --timeout=180s

echo "Ingress controller repaired."
