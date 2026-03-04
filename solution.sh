#!/bin/bash
set -e

kubectl -n ingress-system patch configmap ingress-nginx-config \
--type merge \
-p '{"data":{"nginx.conf":"events {}\n\nhttp {\n  keepalive_timeout 65;\n\n  server {\n    listen 80;\n\n    location / {\n      return 200 \"Ingress Controller Running\";\n    }\n  }\n}\n"}}'

kubectl -n ingress-system rollout restart deployment ingress-controller
kubectl -n ingress-system rollout status deployment ingress-controller