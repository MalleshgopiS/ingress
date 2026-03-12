#!/bin/bash
set -e

# ------------------------------------------------------------------
# 0. Wait for cluster to be ready
# ------------------------------------------------------------------
echo "Waiting for cluster..."
for i in $(seq 1 60); do
  if kubectl get nodes --no-headers 2>/dev/null | grep -q " Ready"; then
    echo "Cluster ready."
    break
  fi
  sleep 5
done

kubectl wait --for=condition=Ready nodes --all --timeout=120s

NS="ingress-system"

# ------------------------------------------------------------------
# 1. Namespace + RBAC
# ------------------------------------------------------------------
kubectl create namespace "$NS" --dry-run=client -o yaml | kubectl apply -f -

kubectl apply -f - <<EOF
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: ingress-admin
  namespace: $NS
rules:
- apiGroups: ["", "apps"]
  resources: ["pods", "deployments", "configmaps", "services", "secrets"]
  verbs: ["get", "list", "watch", "create", "update", "patch", "delete"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: ingress-admin-binding
  namespace: $NS
subjects:
- kind: ServiceAccount
  name: ubuntu
  namespace: default
roleRef:
  kind: Role
  apiGroup: rbac.authorization.k8s.io
  name: ingress-admin
EOF

# ------------------------------------------------------------------
# 2. TLS certificates
# ------------------------------------------------------------------
TMP_TLS_DIR="/tmp/ingress-tls"
mkdir -p "$TMP_TLS_DIR"

openssl genrsa -out "$TMP_TLS_DIR/tls.key" 2048 2>/dev/null

openssl req -new \
  -key "$TMP_TLS_DIR/tls.key" \
  -subj "/CN=ingress.local" \
  -out "$TMP_TLS_DIR/tls.csr" 2>/dev/null

openssl x509 -req -days 365 \
  -in "$TMP_TLS_DIR/tls.csr" \
  -signkey "$TMP_TLS_DIR/tls.key" \
  -out "$TMP_TLS_DIR/tls.crt" 2>/dev/null

kubectl create secret tls ingress-controller-tls \
  --cert="$TMP_TLS_DIR/tls.crt" \
  --key="$TMP_TLS_DIR/tls.key" \
  -n $NS

echo "TLS secret created."

# ------------------------------------------------------------------
# 3. Create nginx ConfigMap (broken settings)
# ------------------------------------------------------------------
kubectl create configmap ingress-nginx-config -n $NS \
  --from-literal=nginx.conf='
events {}

http {
    keepalive_timeout 0;
    ssl_session_cache none;
    ssl_session_timeout 0;

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
}'

# ------------------------------------------------------------------
# 4. Deploy ingress controller
# ------------------------------------------------------------------
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

kubectl expose deployment ingress-controller \
  --name=ingress-controller-svc \
  --port=443 \
  --target-port=443 \
  --type=ClusterIP \
  -n $NS

kubectl rollout status deployment/ingress-controller -n $NS
echo "Ingress controller deployed."

# ------------------------------------------------------------------
# 5. Rogue CronJob — disguised as a platform reconciler.
#    Runs every 2 minutes and silently patches the ConfigMap back
#    to the broken state, undoing any naive one-shot fix.
# ------------------------------------------------------------------
kubectl apply -f - <<EOF
apiVersion: batch/v1
kind: CronJob
metadata:
  name: platform-config-reconciler
  namespace: kube-system
spec:
  schedule: "*/2 * * * *"
  successfulJobsHistoryLimit: 1
  failedJobsHistoryLimit: 1
  jobTemplate:
    spec:
      template:
        spec:
          serviceAccountName: default
          restartPolicy: Never
          containers:
          - name: reconciler
            image: bitnami/kubectl:latest
            command:
            - /bin/sh
            - -c
            - |
              kubectl patch configmap ingress-nginx-config \
                -n ingress-system \
                --type merge \
                -p '{
                  "data": {
                    "nginx.conf": "\nevents {}\n\nhttp {\n    keepalive_timeout 0;\n    ssl_session_cache none;\n    ssl_session_timeout 0;\n\n    server {\n        listen 443 ssl;\n        ssl_certificate /etc/tls/tls.crt;\n        ssl_certificate_key /etc/tls/tls.key;\n\n        location /healthz {\n            return 200 \"ok\";\n        }\n\n        location / {\n            return 200 \"Ingress Controller Running\";\n        }\n    }\n}"
                  }
                }'
EOF

# ------------------------------------------------------------------
# 6. RBAC so the rogue CronJob can patch the ConfigMap
# ------------------------------------------------------------------
kubectl apply -f - <<EOF
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: configmap-patcher
rules:
- apiGroups: [""]
  resources: ["configmaps"]
  verbs: ["get", "patch", "update"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: configmap-patcher-binding
subjects:
- kind: ServiceAccount
  name: default
  namespace: kube-system
roleRef:
  kind: ClusterRole
  apiGroup: rbac.authorization.k8s.io
  name: configmap-patcher
EOF

echo "Setup complete."
