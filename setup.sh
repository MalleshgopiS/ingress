#!/usr/bin/env bash
set -e

NS="default"

echo "Creating TLS certificate..."
openssl req -x509 -nodes -days 365 \
  -newkey rsa:2048 \
  -keyout tls.key \
  -out tls.crt \
  -subj "/CN=ingress.local"

kubectl create secret tls ingress-tls \
  --cert=tls.crt --key=tls.key -n $NS

echo "Creating broken nginx config..."
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
      server {
        listen 443 ssl;
        ssl_certificate /etc/tls/tls.crt;
        ssl_certificate_key /etc/tls/tls.key;

        keepalive_timeout 0;

        location / {
          return 200 "Ingress Controller Running";
        }
      }
    }
EOF

echo "Creating deployment..."
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
          resources:
            limits:
              memory: "128Mi"
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
cat <<EOF | kubectl apply -f -
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

echo "Waiting for deployment rollout..."
kubectl rollout status deployment ingress-controller -n $NS --timeout=180s

echo "Setup completed successfully."