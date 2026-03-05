#!/bin/bash
set -e

NS=default

mkdir -p /grader

echo "Creating TLS certificate..."
openssl req -x509 -nodes -days 365 -newkey rsa:2048 \
  -keyout tls.key -out tls.crt \
  -subj "/CN=ingress.local"

kubectl create secret tls ingress-tls \
  --key tls.key --cert tls.crt -n $NS

echo "Creating broken nginx config..."
kubectl create configmap ingress-nginx-config -n $NS \
  --from-literal=nginx.conf='events { worker_connections 512; }
http {
    keepalive_timeout 0;
    server {
        listen 443 ssl;
        ssl_certificate /etc/nginx/tls/tls.crt;
        ssl_certificate_key /etc/nginx/tls/tls.key;
        location / {
            return 200 "Ingress Controller Running";
        }
    }
}'

echo "Creating deployment..."
kubectl apply -f - <<EOF
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
        resources:
          limits:
            memory: "128Mi"
        volumeMounts:
        - name: config
          mountPath: /etc/nginx/nginx.conf
          subPath: nginx.conf
        - name: tls
          mountPath: /etc/nginx/tls
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

echo "Waiting for deployment..."
kubectl rollout status deployment/ingress-controller -n $NS

# Save original UIDs for grader
kubectl get deployment ingress-controller -n $NS \
  -o jsonpath='{.metadata.uid}' > /grader/original_uid

kubectl get pods -n $NS -l app=ingress-controller \
  -o jsonpath='{.items[0].metadata.uid}' > /grader/original_pod_uid

chmod 400 /grader/original_uid
chmod 400 /grader/original_pod_uid

echo "Setup completed successfully."