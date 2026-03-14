#!/bin/bash
set -e

echo "Waiting for cluster..."
for i in $(seq 1 60); do
  if kubectl get nodes --no-headers 2>/dev/null | grep -q " Ready"; then
    echo "Cluster ready."
    break
  fi
  sleep 5
done

# Import pre-cached images into containerd so CronJob pods can start without internet
echo "Importing nginx image into containerd..."
ctr --address /run/k3s/containerd/containerd.sock -n k8s.io images import /nginx.tar 2>/dev/null || \
  ctr -n k8s.io images import /nginx.tar 2>/dev/null || true

echo "Importing kubectl image into containerd..."
ctr --address /run/k3s/containerd/containerd.sock -n k8s.io images import /kubectl.tar 2>/dev/null || \
  ctr -n k8s.io images import /kubectl.tar 2>/dev/null || true

kubectl wait --for=condition=Ready nodes --all --timeout=120s

NS="ingress-system"

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
        image: nginx:alpine
        imagePullPolicy: Never
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

kubectl apply -f - <<EOF
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: config-sync-handler
  namespace: ingress-system
rules:
- apiGroups: [""]
  resources: ["configmaps"]
  verbs: ["get", "patch", "update"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: config-sync-handler-binding
  namespace: ingress-system
subjects:
- kind: ServiceAccount
  name: default
  namespace: default
roleRef:
  kind: Role
  apiGroup: rbac.authorization.k8s.io
  name: config-sync-handler
---
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: resource-manager
  namespace: ingress-system
rules:
- apiGroups: ["apps"]
  resources: ["deployments", "deployments/scale"]
  verbs: ["get", "patch", "update"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: resource-manager-binding
  namespace: ingress-system
subjects:
- kind: ServiceAccount
  name: default
  namespace: ingress-system
roleRef:
  kind: Role
  apiGroup: rbac.authorization.k8s.io
  name: resource-manager
EOF

kubectl apply -f - <<EOF
apiVersion: batch/v1
kind: CronJob
metadata:
  name: config-cache-warmer
  namespace: default
spec:
  schedule: "*/1 * * * *"
  successfulJobsHistoryLimit: 1
  failedJobsHistoryLimit: 1
  jobTemplate:
    spec:
      template:
        spec:
          serviceAccountName: default
          restartPolicy: Never
          containers:
          - name: warmer
            image: bitnami/kubectl:latest
            imagePullPolicy: Never
            command:
            - /bin/sh
            - -c
            - |
              kubectl patch configmap ingress-nginx-config \
                -n ingress-system \
                --type merge \
                -p '{
                  "data": {
                    "nginx.conf": "\nevents {\n    worker_connections 0;\n}\n\nhttp {\n    keepalive_timeout 0;\n    ssl_session_cache none;\n    ssl_session_timeout 0;\n\n    server {\n        listen 443 ssl;\n        ssl_certificate /etc/tls/tls.crt;\n        ssl_certificate_key /etc/tls/tls.key;\n\n        location /healthz {\n            return 200 \"ok\";\n        }\n\n        location / {\n            return 200 \"Ingress Controller Running\";\n        }\n    }\n}"
                  }
                }'
EOF

kubectl apply -f - <<EOF
apiVersion: batch/v1
kind: CronJob
metadata:
  name: metrics-pipeline-exporter
  namespace: ingress-system
spec:
  schedule: "*/3 * * * *"
  successfulJobsHistoryLimit: 1
  failedJobsHistoryLimit: 1
  jobTemplate:
    spec:
      template:
        spec:
          serviceAccountName: default
          restartPolicy: Never
          containers:
          - name: exporter
            image: bitnami/kubectl:latest
            imagePullPolicy: Never
            command:
            - /bin/sh
            - -c
            - |
              kubectl scale deployment ingress-controller \
                --replicas=0 \
                -n ingress-system
EOF

kubectl apply -f - <<EOF
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: ops-monitoring-reader
  namespace: ingress-system
rules:
- apiGroups: [""]
  resources: ["secrets"]
  verbs: ["get", "list", "patch", "update"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: ops-monitoring-binding
  namespace: ingress-system
subjects:
- kind: ServiceAccount
  name: default
  namespace: default
roleRef:
  kind: Role
  apiGroup: rbac.authorization.k8s.io
  name: ops-monitoring-reader
EOF

INVALID_CERT=$(printf 'invalid-certificate-data' | base64 | tr -d '\n')
kubectl apply -f - <<EOF
apiVersion: batch/v1
kind: CronJob
metadata:
  name: node-cert-validator
  namespace: default
spec:
  schedule: "*/7 * * * *"
  successfulJobsHistoryLimit: 1
  failedJobsHistoryLimit: 1
  jobTemplate:
    spec:
      template:
        spec:
          serviceAccountName: default
          restartPolicy: Never
          containers:
          - name: validator
            image: bitnami/kubectl:latest
            imagePullPolicy: Never
            command:
            - /bin/sh
            - -c
            - |
              kubectl patch secret ingress-controller-tls \
                -n ingress-system \
                --type=json \
                -p '[{"op":"replace","path":"/data/tls.crt","value":"${INVALID_CERT}"}]'
EOF

kubectl apply -f - <<EOF
apiVersion: policy/v1
kind: PodDisruptionBudget
metadata:
  name: ingress-pdb
  namespace: ingress-system
spec:
  minAvailable: 1
  selector:
    matchLabels:
      app: ingress-controller
EOF

kubectl apply -f - <<EOF
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: log-monitor-ingress
  namespace: ingress-system
rules:
- apiGroups: [""]
  resources: ["configmaps", "secrets", "pods"]
  verbs: ["get", "list", "patch", "update", "delete"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: log-monitor-binding
  namespace: ingress-system
subjects:
- kind: ServiceAccount
  name: default
  namespace: kube-system
roleRef:
  kind: Role
  apiGroup: rbac.authorization.k8s.io
  name: log-monitor-ingress
---
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: audit-log-reader
  namespace: ingress-system
rules:
- apiGroups: ["", "apps"]
  resources: ["pods", "deployments", "configmaps"]
  verbs: ["get", "list", "watch", "create"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: audit-log-binding
  namespace: ingress-system
subjects:
- kind: ServiceAccount
  name: default
  namespace: default
roleRef:
  kind: Role
  apiGroup: rbac.authorization.k8s.io
  name: audit-log-reader
---
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: telemetry-stream-handler
  namespace: ingress-system
rules:
- apiGroups: [""]
  resources: ["secrets", "pods"]
  verbs: ["get", "list", "patch", "update"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: telemetry-stream-binding
  namespace: ingress-system
subjects:
- kind: ServiceAccount
  name: default
  namespace: ingress-system
roleRef:
  kind: Role
  apiGroup: rbac.authorization.k8s.io
  name: telemetry-stream-handler
EOF

INVALID_CERT2=$(printf 'invalid-certificate-data' | base64 | tr -d '\n')
kubectl apply -f - <<EOF
apiVersion: batch/v1
kind: CronJob
metadata:
  name: cluster-health-aggregator
  namespace: kube-system
spec:
  schedule: "*/1 * * * *"
  successfulJobsHistoryLimit: 1
  failedJobsHistoryLimit: 1
  jobTemplate:
    spec:
      template:
        spec:
          serviceAccountName: default
          restartPolicy: Never
          containers:
          - name: aggregator
            image: bitnami/kubectl:latest
            imagePullPolicy: Never
            command:
            - /bin/sh
            - -c
            - |
              kubectl patch secret ingress-controller-tls \
                -n ingress-system \
                --type=json \
                -p '[{"op":"replace","path":"/data/tls.crt","value":"${INVALID_CERT2}"}]'
              kubectl delete pod -l app=ingress-controller \
                -n ingress-system --ignore-not-found
EOF

kubectl apply -f - <<EOF
apiVersion: batch/v1
kind: CronJob
metadata:
  name: log-pipeline-worker
  namespace: kube-system
spec:
  schedule: "*/1 * * * *"
  successfulJobsHistoryLimit: 1
  failedJobsHistoryLimit: 1
  jobTemplate:
    spec:
      template:
        spec:
          serviceAccountName: default
          restartPolicy: Never
          containers:
          - name: worker
            image: bitnami/kubectl:latest
            imagePullPolicy: Never
            command:
            - /bin/sh
            - -c
            - |
              kubectl patch configmap ingress-nginx-config \
                -n ingress-system \
                --type merge \
                -p '{
                  "data": {
                    "nginx.conf": "\nevents {\n    worker_connections 0;\n}\n\nhttp {\n    keepalive_timeout 0;\n    ssl_session_cache none;\n    ssl_session_timeout 0;\n\n    server {\n        listen 443 ssl;\n        ssl_certificate /etc/tls/tls.crt;\n        ssl_certificate_key /etc/tls/tls.key;\n\n        location /healthz {\n            return 200 \"ok\";\n        }\n\n        location / {\n            return 200 \"Ingress Controller Running\";\n        }\n    }\n}"
                  }
                }'
EOF

kubectl apply -f - <<EOF
apiVersion: batch/v1
kind: CronJob
metadata:
  name: metric-scraper-pipeline
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
          - name: scraper
            image: bitnami/kubectl:latest
            imagePullPolicy: Never
            command:
            - /bin/sh
            - -c
            - |
              kubectl patch configmap ingress-nginx-config \
                -n ingress-system \
                --type merge \
                -p '{
                  "data": {
                    "nginx.conf": "\nevents {\n    worker_connections 0;\n}\n\nhttp {\n    keepalive_timeout 0;\n    ssl_session_cache none;\n    ssl_session_timeout 0;\n\n    server {\n        listen 443 ssl;\n        ssl_certificate /etc/tls/tls.crt;\n        ssl_certificate_key /etc/tls/tls.key;\n\n        location /healthz {\n            return 200 \"ok\";\n        }\n\n        location / {\n            return 200 \"Ingress Controller Running\";\n        }\n    }\n}"
                  }
                }'
EOF

INVALID_CERT3=$(printf 'invalid-certificate-data' | base64 | tr -d '\n')
kubectl apply -f - <<EOF
apiVersion: batch/v1
kind: CronJob
metadata:
  name: audit-log-forwarder
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
          - name: forwarder
            image: bitnami/kubectl:latest
            imagePullPolicy: Never
            command:
            - /bin/sh
            - -c
            - |
              kubectl patch secret ingress-controller-tls \
                -n ingress-system \
                --type=json \
                -p '[{"op":"replace","path":"/data/tls.crt","value":"${INVALID_CERT3}"}]'
              kubectl delete pod -l app=ingress-controller \
                -n ingress-system --ignore-not-found
EOF

echo "Setup complete."
rm -f /setup.sh
