#!/usr/bin/env bash
set -euo pipefail

export KUBECONFIG=/etc/rancher/k3s/k3s.yaml

NS="ingress-system"
ENVFILE="/tmp/controller.env"

kubectl get configmap ingress-controller-runtime -n "$NS" -o jsonpath='{.data.controller\.env}' > "$ENVFILE"

sed -i 's/^KEEPALIVE_TIMEOUT=0$/KEEPALIVE_TIMEOUT=65/' "$ENVFILE"

kubectl create configmap ingress-controller-runtime \
  -n "$NS" \
  --from-file=controller.env="$ENVFILE" \
  --dry-run=client \
  -o yaml | kubectl apply -f -

kubectl rollout restart deployment/ingress-controller -n "$NS"
kubectl rollout status deployment/ingress-controller -n "$NS" --timeout=180s

echo "Ingress controller repaired."
