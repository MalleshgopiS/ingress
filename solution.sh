#!/bin/bash
set -e

NS=default

echo "Fixing memory limits..."

kubectl patch deployment ingress-controller -n $NS \
  --type='json' \
  -p='[
    {"op":"replace","path":"/spec/template/spec/containers/0/resources/limits/memory","value":"256Mi"},
    {"op":"replace","path":"/spec/template/spec/containers/0/resources/requests/memory","value":"128Mi"}
  ]'

echo "Restarting deployment..."
kubectl rollout restart deployment/ingress-controller -n $NS
kubectl rollout status deployment/ingress-controller -n $NS

echo "Fix completed."