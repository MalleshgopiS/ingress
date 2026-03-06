#!/bin/bash
set -e

NS=default

kubectl create namespace ingress-system || true

openssl req -x509 -nodes -days 365 -newkey rsa:2048 \
  -keyout tls.key -out tls.crt -subj "/CN=ingress.local"

kubectl create secret tls ingress-controller-tls \
  --key tls.key --cert tls.crt -n $NS || true

cat <<EOF | kubectl apply -f -
apiVersion: v1
kind: ConfigMap
metadata:
  name: ingress-nginx-config
  namespace: $NS
data:
  nginx.conf: |
    events {}
    http {
      keepalive_timeout 0;
      server {
        listen 443 ssl;
        ssl_certificate /etc/tls/tls.crt;
        ssl_certificate_key /etc/tls/tls.key;
        location /healthz {
          return 200 "ok";
        }
        location / {
          return 200 "Ingress Controller Running";
        }
      }
    }
EOF

cat <<EOF | kubectl apply -f -
apiVersion: apps/v1
kind: Deployment
metadata:
  name: ingress-controller
  namespace: $NS
spec:
  replicas: 1
  selector:
    matchLabels:
      app: ingress-controller
  template:
    metadata:
      labels:
        app: ingress-controller
    spec:
      containers:
      - name: nginx
        image: nginx:alpine
        ports:
        - containerPort: 443
        volumeMounts:
        - name: tls
          mountPath: /etc/tls
        - name: config
          mountPath: /etc/nginx/nginx.conf
          subPath: nginx.conf
      volumes:
      - name: tls
        secret:
          secretName: ingress-controller-tls
      - name: config
        configMap:
          name: ingress-nginx-config
EOF

kubectl expose deployment ingress-controller \
  --port=443 --target-port=443 --name=ingress-controller -n $NS || true

kubectl rollout status deployment ingress-controller -n $NS