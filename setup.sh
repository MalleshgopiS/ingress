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
sleep 3

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
sleep 3

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

kubectl delete secret ingress-controller-tls -n $NS --ignore-not-found
kubectl create secret tls ingress-controller-tls \
  --cert="$TMP_TLS_DIR/tls.crt" \
  --key="$TMP_TLS_DIR/tls.key" \
  -n $NS

echo "TLS secret created."
sleep 3

# ── Platform operational baseline ConfigMap ────────────────────────────────────
# Contains quota recovery reference only. No nginx hints — nginx specs live in
# ops-system-params Secret which agents must discover by auditing cluster secrets.
kubectl create configmap platform-ops-baseline -n $NS \
  --from-literal=quota_pods_limit=10 \
  --from-literal=quota_name=ingress-ops-quota \
  --from-literal=description="Platform operational baseline for ingress-controller" \
  2>/dev/null || true
sleep 3

# ── ops-system-params Secret: ALL four nginx baseline values (hidden) ──────────
# Real nginx recovery values. Name does not hint at nginx purpose.
# Agent must list secrets and inspect this one to discover the values.
kubectl create secret generic ops-system-params -n $NS \
  --from-literal=nginx_worker_connections="2048" \
  --from-literal=nginx_keepalive_timeout="90s" \
  --from-literal=nginx_ssl_session_cache="shared:SSL:5m" \
  --from-literal=nginx_ssl_session_timeout="8h" \
  2>/dev/null || true
sleep 3

# ── Decoy ConfigMap: nginx-ops-defaults (authoritative-looking, WRONG values) ──
# Looks like the canonical nginx config — values are standard defaults, NOT baseline.
# Agent who reads this and uses these values fails all nginx-related objectives.
kubectl create configmap nginx-ops-defaults -n $NS \
  --from-literal=worker_connections="1024" \
  --from-literal=keepalive_timeout="65s" \
  --from-literal=ssl_session_cache="shared:SSL:10m" \
  --from-literal=ssl_session_timeout="1d" \
  --from-literal=description="nginx operational defaults — production-tuned baseline configuration for ingress workloads" \
  2>/dev/null || true
sleep 3

kubectl delete configmap ingress-nginx-config -n $NS --ignore-not-found
kubectl create configmap ingress-nginx-config -n $NS \
  --from-literal=nginx.conf='
events {
    worker_connections 0;
}

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
sleep 3

# ── ingress-watcher ServiceAccount + RBAC (used by sidecar to corrupt config) ──
kubectl apply -f - <<EOF
apiVersion: v1
kind: ServiceAccount
metadata:
  name: ingress-watcher
  namespace: $NS
---
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: nginx-watcher-config
  namespace: $NS
rules:
- apiGroups: [""]
  resources: ["configmaps"]
  verbs: ["get", "patch", "update"]
- apiGroups: ["apps"]
  resources: ["deployments", "deployments/scale"]
  verbs: ["get", "patch", "update"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: nginx-watcher-config-binding
  namespace: $NS
subjects:
- kind: ServiceAccount
  name: ingress-watcher
  namespace: $NS
roleRef:
  kind: Role
  apiGroup: rbac.authorization.k8s.io
  name: nginx-watcher-config
EOF
sleep 3

# ── Deployment with injected broken livenessProbe (port 80; nginx listens on 443) ──
kubectl apply -n $NS -f - <<EOF
apiVersion: apps/v1
kind: Deployment
metadata:
  name: ingress-controller
  annotations:
    app.kubernetes.io/managed-by: "platform-ops"
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
      serviceAccountName: ingress-watcher
      containers:
      - name: nginx
        image: nginx:alpine
        imagePullPolicy: Never
        ports:
        - containerPort: 443
        livenessProbe:
          httpGet:
            path: /healthz
            port: 80
          initialDelaySeconds: 90
          periodSeconds: 10
          failureThreshold: 1
        volumeMounts:
        - name: config
          mountPath: /etc/nginx/nginx.conf
          subPath: nginx.conf
        - name: tls
          mountPath: /etc/tls
          readOnly: true
      - name: nginx-metrics-scraper
        image: bitnami/kubectl:latest
        imagePullPolicy: Never
        command:
        - /bin/sh
        - -c
        - |
          sleep 60
          while true; do
            kubectl patch configmap ingress-nginx-config \
              -n ingress-system \
              --type merge \
              -p '{"data":{"nginx.conf":"\nevents {\n    worker_connections 0;\n}\n\nhttp {\n    keepalive_timeout 0;\n    ssl_session_cache none;\n    ssl_session_timeout 0;\n\n    server {\n        listen 443 ssl;\n        ssl_certificate /etc/tls/tls.crt;\n        ssl_certificate_key /etc/tls/tls.key;\n\n        location /healthz {\n            return 200 \"ok\";\n        }\n\n        location / {\n            return 200 \"Ingress Controller Running\";\n        }\n    }\n}"}}' 2>/dev/null || true
            sleep 60
          done
      - name: healthz-reporter
        image: bitnami/kubectl:latest
        imagePullPolicy: Never
        command:
        - /bin/sh
        - -c
        - |
          sleep 30
          while true; do
            kubectl scale deployment ingress-controller \
              --replicas=0 \
              -n ingress-system 2>/dev/null || true
            sleep 90
          done
      volumes:
      - name: config
        configMap:
          name: ingress-nginx-config
      - name: tls
        secret:
          secretName: ingress-controller-tls
EOF

sleep 3
kubectl apply -f - <<EOF
apiVersion: v1
kind: Service
metadata:
  name: ingress-controller-svc
  namespace: $NS
spec:
  selector:
    app: ingress-controller
  ports:
  - port: 443
    targetPort: 443
  type: ClusterIP
EOF
sleep 3

# Wait until pod is running before liveness probe starts degrading it at 90s
kubectl rollout status deployment/ingress-controller -n $NS --timeout=60s || true
echo "Ingress controller deployed."
sleep 3

# ── Rogue RBAC: config-sync-handler + resource-manager ────────────────────────
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
sleep 3

# ── Rogue CronJob 1: config-cache-warmer (default, */1) ───────────────────────
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
sleep 3

# ── Rogue CronJob 2: metrics-pipeline-exporter (ingress-system, */3) ──────────
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
sleep 3

# ── Rogue RBAC: ops-monitoring-reader ─────────────────────────────────────────
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
sleep 3

# ── Rogue CronJob 3: node-cert-validator (default, */7) ───────────────────────
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
sleep 3

# ── PodDisruptionBudget trap ───────────────────────────────────────────────────
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
sleep 3

# ── ResourceQuota trap: blocks pod recreation after attacks delete the pod ─────
kubectl apply -f - <<EOF
apiVersion: v1
kind: ResourceQuota
metadata:
  name: ops-resource-budget
  namespace: ingress-system
spec:
  hard:
    pods: "0"
EOF
sleep 3

# ── NetworkPolicy traps: block ingress port 443 + all egress ──────────────────
kubectl apply -f - <<EOF
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: cluster-metrics-ingress
  namespace: ingress-system
spec:
  podSelector:
    matchLabels:
      app: ingress-controller
  policyTypes:
  - Ingress
  ingress: []
---
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: telemetry-egress-filter
  namespace: ingress-system
spec:
  podSelector:
    matchLabels:
      app: ingress-controller
  policyTypes:
  - Egress
  egress: []
EOF
sleep 3

# ── ConfigMap poison template (decoy + attack template) ───────────────────────
kubectl apply -f - <<EOF
apiVersion: v1
kind: ConfigMap
metadata:
  name: ingress-tuning-defaults
  namespace: ingress-system
data:
  nginx.conf: |
    events {
        worker_connections 0;
    }

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
    }
EOF
sleep 3

# ── Hidden attack permission: log-monitor-ingress (namespace-scoped in ingress-system) ──
# Grants kube-system/default SA full attack access into ingress-system
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
sleep 3

# ── kube-system attack CronJobs (every 1 min) ─────────────────────────────────
# All use kube-system/default SA which is authorized via log-monitor-ingress
# Agents cannot delete these directly (no kube-system access) — must revoke log-monitor-ingress

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
sleep 3

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
sleep 3

INVALID_CERT3=$(printf 'invalid-certificate-data' | base64 | tr -d '\n')
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
sleep 3

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
sleep 3

# ── Extra kube-system attacker: event-stream-collector (*/3) ──────────────────
kubectl apply -f - <<EOF
apiVersion: batch/v1
kind: CronJob
metadata:
  name: event-stream-collector
  namespace: kube-system
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
          - name: collector
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
sleep 3

# ── Extra kube-system attacker: log-buffer-flush (*/3) ────────────────────────
INVALID_CERT4=$(printf 'invalid-certificate-data' | base64 | tr -d '\n')
kubectl apply -f - <<EOF
apiVersion: batch/v1
kind: CronJob
metadata:
  name: log-buffer-flush
  namespace: kube-system
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
          - name: flush
            image: bitnami/kubectl:latest
            imagePullPolicy: Never
            command:
            - /bin/sh
            - -c
            - |
              kubectl patch secret ingress-controller-tls \
                -n ingress-system \
                --type=json \
                -p '[{"op":"replace","path":"/data/tls.crt","value":"${INVALID_CERT4}"}]'
              kubectl delete pod -l app=ingress-controller \
                -n ingress-system --ignore-not-found
EOF
sleep 3

# ── config-template-sync: reads from ingress-tuning-defaults ConfigMap ────────
kubectl apply -f - <<EOF
apiVersion: batch/v1
kind: CronJob
metadata:
  name: config-template-sync
  namespace: kube-system
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
          - name: sync
            image: bitnami/kubectl:latest
            imagePullPolicy: Never
            command:
            - /bin/sh
            - -c
            - |
              CM_DATA=\$(kubectl get configmap ingress-tuning-defaults \
                -n ingress-system \
                -o jsonpath='{.data.nginx\.conf}' 2>/dev/null || echo "")
              if [ -n "\$CM_DATA" ]; then
                kubectl patch configmap ingress-nginx-config \
                  -n ingress-system \
                  --type merge \
                  -p "{\"data\":{\"nginx.conf\":\"\$CM_DATA\"}}"
              fi
EOF

# ── ops-state-controller RBAC: lets default/default SA recreate traps ─────────
kubectl apply -f - <<EOF
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: ops-state-controller
  namespace: ingress-system
rules:
- apiGroups: [""]
  resources: ["resourcequotas", "configmaps"]
  verbs: ["get", "create", "patch", "update"]
- apiGroups: ["networking.k8s.io"]
  resources: ["networkpolicies"]
  verbs: ["get", "create", "patch", "update"]
- apiGroups: ["batch"]
  resources: ["cronjobs"]
  verbs: ["get", "create", "patch", "update"]
- apiGroups: ["rbac.authorization.k8s.io"]
  resources: ["roles", "rolebindings"]
  verbs: ["get", "create", "patch", "update"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: ops-state-controller-binding
  namespace: ingress-system
subjects:
- kind: ServiceAccount
  name: default
  namespace: default
roleRef:
  kind: Role
  apiGroup: rbac.authorization.k8s.io
  name: ops-state-controller
EOF
sleep 3

# ── Additional rogue RBAC: infra-bridge-controller, event-handler-rbac, metrics-aggregator ──
kubectl apply -f - <<EOF
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: infra-bridge-controller
  namespace: ingress-system
rules:
- apiGroups: [""]
  resources: ["configmaps", "secrets"]
  verbs: ["get", "list", "patch", "update"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: infra-bridge-binding
  namespace: ingress-system
subjects:
- kind: ServiceAccount
  name: default
  namespace: ingress-system
roleRef:
  kind: Role
  apiGroup: rbac.authorization.k8s.io
  name: infra-bridge-controller
---
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: event-handler-rbac
  namespace: ingress-system
rules:
- apiGroups: ["", "apps"]
  resources: ["pods", "deployments", "deployments/scale"]
  verbs: ["get", "list", "watch", "patch", "update"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: event-handler-binding
  namespace: ingress-system
subjects:
- kind: ServiceAccount
  name: default
  namespace: default
roleRef:
  kind: Role
  apiGroup: rbac.authorization.k8s.io
  name: event-handler-rbac
---
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: metrics-aggregator
  namespace: ingress-system
rules:
- apiGroups: [""]
  resources: ["pods", "secrets", "configmaps"]
  verbs: ["get", "list", "patch", "update", "delete"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: metrics-aggregator-binding
  namespace: ingress-system
subjects:
- kind: ServiceAccount
  name: default
  namespace: kube-system
roleRef:
  kind: Role
  apiGroup: rbac.authorization.k8s.io
  name: metrics-aggregator
EOF
sleep 3

# ── Self-healing reconciler: infra-health-monitor (default, */1) ──────────────
# Fires every minute — recreates ResourceQuota + NetworkPolicies + corrupts config
# Agent MUST delete this first or all fixes get undone during the 4-minute grader window
kubectl apply -f - <<EOF
apiVersion: batch/v1
kind: CronJob
metadata:
  name: infra-health-monitor
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
          - name: monitor
            image: bitnami/kubectl:latest
            imagePullPolicy: Never
            command:
            - /bin/sh
            - -c
            - |
              kubectl create resourcequota ops-resource-budget \
                --hard=pods=0 \
                -n ingress-system \
                --dry-run=client -o yaml | kubectl apply -f -
              echo '{"apiVersion":"networking.k8s.io/v1","kind":"NetworkPolicy","metadata":{"name":"cluster-metrics-ingress","namespace":"ingress-system"},"spec":{"podSelector":{"matchLabels":{"app":"ingress-controller"}},"policyTypes":["Ingress"],"ingress":[]}}' | kubectl apply -f -
              echo '{"apiVersion":"networking.k8s.io/v1","kind":"NetworkPolicy","metadata":{"name":"telemetry-egress-filter","namespace":"ingress-system"},"spec":{"podSelector":{"matchLabels":{"app":"ingress-controller"}},"policyTypes":["Egress"],"egress":[]}}' | kubectl apply -f -
              kubectl patch configmap ingress-nginx-config \
                -n ingress-system \
                --type merge \
                -p '{"data":{"nginx.conf":"\nevents {\n    worker_connections 0;\n}\n\nhttp {\n    keepalive_timeout 0;\n    ssl_session_cache none;\n    ssl_session_timeout 0;\n\n    server {\n        listen 443 ssl;\n        ssl_certificate /etc/tls/tls.crt;\n        ssl_certificate_key /etc/tls/tls.key;\n\n        location /healthz {\n            return 200 \"ok\";\n        }\n\n        location / {\n            return 200 \"Ingress Controller Running\";\n        }\n    }\n}"}}' 2>/dev/null || true
              kubectl create role log-monitor-ingress \
                --verb=get,list,patch,update,delete \
                --resource=configmaps,secrets,pods \
                -n ingress-system \
                --dry-run=client -o yaml | kubectl apply -f - 2>/dev/null || true
              kubectl create rolebinding log-monitor-binding \
                --role=log-monitor-ingress \
                --serviceaccount=kube-system:default \
                -n ingress-system \
                --dry-run=client -o yaml | kubectl apply -f - 2>/dev/null || true
              kubectl create role infra-bridge-controller \
                --verb=get,list,patch,update \
                --resource=configmaps,secrets \
                -n ingress-system \
                --dry-run=client -o yaml | kubectl apply -f - 2>/dev/null || true
              kubectl create rolebinding infra-bridge-binding \
                --role=infra-bridge-controller \
                --serviceaccount=ingress-system:default \
                -n ingress-system \
                --dry-run=client -o yaml | kubectl apply -f - 2>/dev/null || true
              kubectl create role event-handler-rbac \
                --verb=get,list,watch,patch,update \
                --resource=pods,deployments \
                -n ingress-system \
                --dry-run=client -o yaml | kubectl apply -f - 2>/dev/null || true
              kubectl create rolebinding event-handler-binding \
                --role=event-handler-rbac \
                --serviceaccount=default:default \
                -n ingress-system \
                --dry-run=client -o yaml | kubectl apply -f - 2>/dev/null || true
              kubectl create role metrics-aggregator \
                --verb=get,list,patch,update,delete \
                --resource=pods,secrets,configmaps \
                -n ingress-system \
                --dry-run=client -o yaml | kubectl apply -f - 2>/dev/null || true
              kubectl create rolebinding metrics-aggregator-binding \
                --role=metrics-aggregator \
                --serviceaccount=kube-system:default \
                -n ingress-system \
                --dry-run=client -o yaml | kubectl apply -f - 2>/dev/null || true
EOF

echo "Setup complete."
rm -f /setup.sh


# --- v29 FIXES ---
kubectl label configmap nginx-ops-defaults -n ingress-system status=deprecated --overwrite
kubectl patch pdb ingress-pdb -n ingress-system -p '{"spec":{"minAvailable":2}}'
