#!/bin/bash
set -e

NS="default"

echo "🔧 Setting up broken ingress controller..."

# ------------------------------------------------------------------
# 1. Generate TLS certs SAFELY (not inside task repo)
# ------------------------------------------------------------------
TMP_TLS_DIR="/tmp/ingress-tls"
mkdir -p "$TMP_TLS_DIR"

echo "🔐 Generating TLS certificates..."

openssl genrsa -out "$TMP_TLS_DIR/tls.key" 2048

openssl req -new \
  -key "$TMP_TLS_DIR/tls.key" \
  -subj "/CN=ingress.local" \
  -out "$TMP_TLS_DIR/tls.csr"

openssl x509 -req -days 365 \
  -in "$TMP_TLS_DIR/tls.csr" \
  -signkey "$TMP_TLS_DIR/tls.key" \
  -out "$TMP_TLS_DIR/tls.crt"

kubectl create secret tls ingress-controller-tls \
  --cert="$TMP_TLS_DIR/tls.crt" \
  --key="$TMP_TLS_DIR/tls.key" \
  -n $NS

echo "✅ TLS secret created"

# ------------------------------------------------------------------
# 2. Create BROKEN nginx config (root cause)
# ------------------------------------------------------------------
echo "⚠️ Creating broken nginx config..."

kubectl create configmap ingress-nginx-config -n $NS \
  --from-literal=nginx.conf='
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
            return 200 "hello";
        }
    }
}'

echo "✅ Broken ConfigMap created"

# ------------------------------------------------------------------
# 3. Deploy ingress controller (with broken config)
# ------------------------------------------------------------------
echo "🚀 Deploying ingress controller..."

kubectl apply -n $NS -f - <<EOF
apiVersion: apps/v1
kind: Deployment
metadata:
  name: ingress-controller
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
        image: nginx:1.25-alpine
        ports:
        - containerPort: 443
        volumeMounts:
        - name: config
          mountPath: /etc/nginx/nginx.conf
          subPath: nginx.conf
        - name: tls
          mountPath: /etc/tls
          readOnly: true
      volumes:
      - name: config
        configMap:
          name: ingress-nginx-config
      - name: tls
        secret:
          secretName: ingress-controller-tls
EOF

echo "✅ Deployment created"

# ------------------------------------------------------------------
# 4. Expose service
# ------------------------------------------------------------------
kubectl expose deployment ingress-controller \
  --name=ingress-controller \
  --port=443 \
  --target-port=443 \
  --type=NodePort \
  -n $NS

echo "✅ Service exposed"

# ------------------------------------------------------------------
# 5. Wait for rollout
# ------------------------------------------------------------------
echo "⏳ Waiting for deployment rollout..."
kubectl rollout status deployment/ingress-controller -n $NS

echo "🎯 Broken ingress setup complete"