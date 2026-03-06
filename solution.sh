#!/usr/bin/env bash
set -euo pipefail

export KUBECONFIG=/etc/rancher/k3s/k3s.yaml

NS="aurora-ingress"
CFG="/tmp/edge-gateway.conf"

kubectl get configmap edge-gateway-config -n "$NS" -o jsonpath='{.data.nginx\.conf}' > "$CFG"

sed -i \
  -e 's/keepalive_timeout 0;/keepalive_timeout 65;/' \
  -e 's/bleater-ui\.aurora-ingress\.svc\.cluster\.local:80/bleater-ui.aurora-ingress.svc.cluster.local:8080/' \
  -e 's/bleater-assets\.aurora-ingress\.svc\.cluster\.local:80/bleater-assets.aurora-ingress.svc.cluster.local:8081/' \
  -e 's/bleater-api\.aurora-ingress\.svc\.cluster\.local:8080\/health/bleater-api.aurora-ingress.svc.cluster.local:9090\/health/' \
  "$CFG"

kubectl create configmap edge-gateway-config \
  -n "$NS" \
  --from-file=nginx.conf="$CFG" \
  --dry-run=client \
  -o yaml | kubectl apply -f -

kubectl rollout restart deployment/edge-gateway -n "$NS"
kubectl rollout status deployment/edge-gateway -n "$NS" --timeout=180s

echo "Edge gateway repaired."