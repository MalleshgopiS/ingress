#!/bin/bash
set -e

# [DO NOT CHANGE ANYTHING BELOW] Boilerplate for k3s readiness
if ! supervisorctl status &>/dev/null; then
    echo "Starting supervisord..."
    /usr/bin/supervisord -c /etc/supervisor/supervisord.conf
    sleep 5
fi
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
echo "Waiting for k3s to be ready..."
MAX_WAIT=120
ELAPSED=0
until kubectl get nodes &>/dev/null; do
    if [ $ELAPSED -ge $MAX_WAIT ]; then
        echo "Error: k3s is not ready after ${MAX_WAIT} seconds"
        exit 1
    fi
    echo "Waiting for k3s... (${ELAPSED}s elapsed)"
    sleep 2
    ELAPSED=$((ELAPSED + 2))
done
echo "k3s is ready!"
# [DO NOT CHANGE ANYTHING ABOVE]

# Import pre-cached images into containerd so nginx pod can start without internet
echo "Importing pre-cached images into containerd..."
k3s ctr -n k8s.io images import --local --snapshotter=native --platform linux/amd64 /images/nginx_1.27-alpine.oci.tar
k3s ctr -n k8s.io images tag nginx:1.27-alpine docker.io/library/nginx:1.27-alpine 2>/dev/null || true
k3s ctr -n k8s.io images import --local --snapshotter=native --platform linux/amd64 /images/alpine_k8s_1.30.4.oci.tar

NS="ingress-system"

kubectl create namespace "$NS" --dry-run=client -o yaml | kubectl apply -f -
sleep 2

# ── TLS Secret (valid certificate) ────────────────────────────────────────────

TMP_TLS_DIR="/tmp/ingress-tls"
mkdir -p "$TMP_TLS_DIR"

openssl genrsa -out "$TMP_TLS_DIR/tls.key" 2048 2>/dev/null
openssl req -new -key "$TMP_TLS_DIR/tls.key" -subj "/CN=ingress.local" \
  -out "$TMP_TLS_DIR/tls.csr" 2>/dev/null
openssl x509 -req -days 365 -in "$TMP_TLS_DIR/tls.csr" \
  -signkey "$TMP_TLS_DIR/tls.key" -out "$TMP_TLS_DIR/tls.crt" 2>/dev/null

kubectl delete secret ingress-controller-tls -n $NS --ignore-not-found
kubectl create secret tls ingress-controller-tls \
  --cert="$TMP_TLS_DIR/tls.crt" --key="$TMP_TLS_DIR/tls.key" -n $NS

echo "TLS secret created."
sleep 2

# ── Decoy Secret: tls-session-params ──────────────────────────────────────────


kubectl create secret generic tls-session-params -n $NS \
  --from-literal=ssl_session_cache="shared:SSL:32m" \
  --from-literal=ssl_session_timeout="8h" \
  --from-literal=ssl_buffer_size="32k" \
  --from-literal=warning="Experimental tuning values — exceed platform memory budget for this instance size" \
  2>/dev/null || true
sleep 2


# ── Broken nginx ConfigMap ─────────────────────────────────────────────────────

kubectl create configmap ingress-nginx-config -n $NS \
  --from-literal=nginx.conf='events {
    worker_connections 1024;
}

http {
    keepalive_timeout 75;
    server_tokens off;

    # --- unrelated / noise configs (important for variance) ---
    gzip on;
    gzip_types text/plain application/json;
    client_max_body_size 1m;
    sendfile on;

    # --- SSL config (partially incorrect, but not obvious) ---
    ssl_session_cache shared:SSL:8m;

    tcp_nopush on;
    tcp_nodelay on;

    server {
        listen 443 ssl;

        ssl_certificate     /etc/tls/tls.crt;
        ssl_certificate_key /etc/tls/tls.key;

        ssl_session_timeout 6h;
        ssl_buffer_size 24k;

        location /healthz {
            return 200 "ok";
            add_header Content-Type text/plain;
        }

        location / {
            return 200 "Ingress Controller Running";
            add_header Content-Type text/plain;
        }
    }

    keepalive_requests 100;
}'
sleep 2

# ── nginx Deployment ───────────────────────────────────────────────────────────

kubectl apply -n $NS -f - <<EOF
apiVersion: apps/v1
kind: Deployment
metadata:
  name: ingress-controller
  namespace: $NS
  annotations:
    app.kubernetes.io/managed-by: platform-ops

    incident.platform.io/oom-cause: sustained memory growth observed under encrypted traffic patterns; exact trigger unclear
    incident.platform.io/oom-history: 2026-03-20T16:11:44Z,2026-03-20T09:58:22Z,2026-03-20T03:45:01Z,2026-03-19T21:33:17Z
    incident.platform.io/oom-reason: nginx worker memory exhaustion under sustained HTTPS load — root cause not yet confirmed
    incident.platform.io/ssl-budget: memory allocation must remain within the 300Mi limit under peak load; configuration choices should account for sustained traffic behavior
    incident.platform.io/ssl-buffer-overhead: connection-level resource usage may contribute to memory pressure under high concurrency
    incident.platform.io/ssl-session-retention: connection lifetime and reuse behavior may influence memory retention patterns
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
        image: nginx:1.27-alpine
        imagePullPolicy: Never
        ports:
        - containerPort: 443
        resources:
          limits:
            memory: "300Mi"
          requests:
            memory: "64Mi"
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
sleep 2

# ── Service ────────────────────────────────────────────────────────────────────

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
sleep 2

kubectl rollout status deployment/ingress-controller -n $NS --timeout=90s || true
echo "Ingress controller deployed."

echo "Verifying broken state was successfully applied..."

# 1. Confirm broken ssl_session_cache builtin is in the nginx ConfigMap
CM_CACHE=$(kubectl get configmap ingress-nginx-config -n ingress-system \
    -o jsonpath='{.data.nginx\.conf}' 2>/dev/null \
    | grep -o 'ssl_session_cache[^;]*' | head -n1 || echo "")

if ! echo "$CM_CACHE" | grep -q "ssl_session_cache"; then
    echo "ERROR: nginx ConfigMap missing ssl_session_cache"
    exit 1
fi

# 🔥 ADD THIS BLOCK HERE
CM_CONF=$(kubectl get configmap ingress-nginx-config -n ingress-system \
    -o jsonpath='{.data.nginx\.conf}' 2>/dev/null || echo "")

if ! echo "$CM_CONF" | grep -q "ssl_session_timeout 6h"; then
    echo "ERROR: expected timeout misconfiguration not found"
    exit 1
fi

if ! echo "$CM_CONF" | grep -q "ssl_buffer_size 24k"; then
    echo "ERROR: expected buffer misconfiguration not found"
    exit 1
fi

# 2. Confirm decoy Secret tls-session-params exists
if ! kubectl get secret tls-session-params -n ingress-system >/dev/null 2>&1; then
    echo "ERROR: tls-session-params decoy Secret was not created"
    exit 1
fi

# 3. Confirm keepalive_timeout is in the nginx ConfigMap (must survive patching)
CM_CONF=$(kubectl get configmap ingress-nginx-config -n ingress-system \
    -o jsonpath='{.data.nginx\.conf}' 2>/dev/null || echo "")
if ! echo "$CM_CONF" | grep -q "keepalive_timeout"; then
    echo "ERROR: nginx ConfigMap does not contain keepalive_timeout directive"
    exit 1
fi

# 5. Confirm deployment is running
DEPLOY_READY=$(kubectl get deployment ingress-controller -n ingress-system \
    -o jsonpath='{.status.readyReplicas}' 2>/dev/null || echo "0")
if [ "$DEPLOY_READY" != "1" ]; then
    echo "ERROR: ingress-controller deployment is not ready (readyReplicas=$DEPLOY_READY)"
    exit 1
fi

# 6. Confirm OOMKill history annotation is present
OOM_HIST=$(kubectl get deployment ingress-controller -n ingress-system \
    -o jsonpath='{.metadata.annotations.incident\.platform\.io/oom-history}' 2>/dev/null || echo "")
if [ -z "$OOM_HIST" ]; then
    echo "ERROR: OOM history annotation not set on deployment"
    exit 1
fi

echo "Broken state verification passed — environment is correctly broken."
