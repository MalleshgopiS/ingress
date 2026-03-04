#!/bin/bash
set -e

kubectl create namespace ingress-system || true

cat <<EOF | kubectl apply -f -
apiVersion: v1
kind: ConfigMap
metadata:
  name: ingress-nginx-config
  namespace: ingress-system
data:
  nginx.conf: |
    events {}

    http {
      keepalive_timeout 0;

      server {
        listen 80;

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
  namespace: ingress-system
  labels:
    app: ingress-controller
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
        resources:
          limits:
            memory: 128Mi
        volumeMounts:
        - name: nginx-config
          mountPath: /etc/nginx/nginx.conf
          subPath: nginx.conf
      volumes:
      - name: nginx-config
        configMap:
          name: ingress-nginx-config
EOF

cat <<EOF | kubectl apply -f -
apiVersion: v1
kind: Service
metadata:
  name: ingress-controller
  namespace: ingress-system
spec:
  selector:
    app: ingress-controller
  ports:
  - port: 80
    targetPort: 80
EOF