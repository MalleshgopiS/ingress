#!/usr/bin/env bash
set -e

NS="ingress-system"

echo "Creating namespace..."
kubectl create namespace $NS || true

echo "Granting ubuntu-user access..."

kubectl create role ubuntu-user-admin \
  --verb=get,list,watch,create,update,patch,delete \
  --resource=configmaps,deployments,pods,services \
  -n $NS || true

kubectl create rolebinding ubuntu-user-admin-binding \
  --role=ubuntu-user-admin \
  --user=ubuntu-user \
  -n $NS || true

echo "Creating broken ConfigMap..."

kubectl apply -f - <<EOF
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
        listen 80;

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
        image: nginx:1.25-alpine
        resources:
          limits:
            memory: "128Mi"
        volumeMounts:
        - name: nginx-config
          mountPath: /etc/nginx/nginx.conf
          subPath: nginx.conf
      volumes:
      - name: nginx-config
        configMap:
          name: ingress-nginx-config
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
  - port: 80
    targetPort: 80
EOF

echo "Waiting for pod..."

kubectl rollout status deployment/ingress-controller -n $NS --timeout=180s

echo "Saving original UID..."

kubectl get deployment ingress-controller -n $NS \
-o jsonpath='{.metadata.uid}' > /grader/original_uid

echo "Setup complete."