#!/bin/bash
set -e

NS=default

echo "Creating TLS secret..."
openssl req -x509 -nodes -days 365 -newkey rsa:2048 \
  -keyout tls.key -out tls.crt -subj "/CN=example.com"

kubectl create secret tls ingress-tls \
  --cert=tls.crt --key=tls.key -n $NS

echo "Creating leaking nginx config..."
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
      ssl_session_cache shared:SSL:1m;
      ssl_session_timeout 1d;

      server {
        listen 443 ssl;
        ssl_certificate /etc/tls/tls.crt;
        ssl_certificate_key /etc/tls/tls.key;

        location / {
          return 200 "Ingress Controller Running";
        }
      }
    }
EOF

echo "Creating deployment with bad memory limits..."
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
      app: ingress
  template:
    metadata:
      labels:
        app: ingress
    spec:
      containers:
      - name: nginx
        image: nginx:alpine
        resources:
          limits:
            memory: "64Mi"
          requests:
            memory: "32Mi"
        volumeMounts:
        - name: config
          mountPath: /etc/nginx/nginx.conf
          subPath: nginx.conf
        - name: tls
          mountPath: /etc/tls
      volumes:
      - name: config
        configMap:
          name: ingress-nginx-config
      - name: tls
        secret:
          secretName: ingress-tls
EOF

echo "Creating service..."
kubectl expose deployment ingress-controller \
  --port=443 --target-port=443 --name=ingress-controller -n $NS

echo "Waiting for rollout..."
kubectl rollout status deployment/ingress-controller -n $NS

echo "Saving original UID..."
kubectl get deployment ingress-controller -n $NS \
  -o jsonpath='{.metadata.uid}' > /grader/original_uid

echo "Setup complete."