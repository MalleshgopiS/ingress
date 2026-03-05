#!/usr/bin/env bash
set -e

NS="default"

echo "Creating TLS certificate..."

openssl req -x509 -nodes -days 365 \
-newkey rsa:2048 \
-keyout /tmp/tls.key \
-out /tmp/tls.crt \
-subj "/CN=ingress.local"

kubectl create secret tls ingress-tls \
--cert=/tmp/tls.crt \
--key=/tmp/tls.key \
-n $NS || true


echo "Creating broken nginx config..."

kubectl apply -f - <<EOF
apiVersion: v1
kind: ConfigMap
metadata:
  name: ingress-nginx-config
  namespace: $NS
data:
  nginx.conf: |
    events {
      worker_connections 1;
    }

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
    }
EOF


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
        ports:
        - containerPort: 443
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

kubectl apply -f - <<EOF
apiVersion: v1
kind: Service
metadata:
  name: ingress-controller
  namespace: $NS
spec:
  selector:
    app: ingress-controller
  ports:
  - port: 443
    targetPort: 443
EOF


echo "Waiting for deployment..."

kubectl rollout status deployment/ingress-controller -n $NS --timeout=240s

kubectl get deployment ingress-controller -n $NS \
-o jsonpath='{.metadata.uid}' > /grader/original_uid

chmod 400 /grader/original_uid

echo "Setup completed successfully."