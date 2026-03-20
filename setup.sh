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

# ── Authoritative TLS baseline Secret ─────────────────────────────────────────
# Contains the correct bounded TLS session values that eliminate the memory leak.
# This is the source of truth — agents must use this, not the decoy ConfigMap.

kubectl create secret generic tls-session-params -n $NS \
  --from-literal=ssl_session_cache="shared:SSL:5m" \
  --from-literal=ssl_session_timeout="1h" \
  --from-literal=ssl_buffer_size="4k" \
  2>/dev/null || true
sleep 2

# Snapshot expected TLS values into the protected /grader directory.
# chmod 700 prevents agents from reading and hardcoding the answers.

python3 - <<'PYEOF'
import subprocess, base64, json, os
try:
    out = subprocess.check_output(
        ['kubectl', 'get', 'secret', 'tls-session-params',
         '-n', 'ingress-system', '-o', 'json'],
        stderr=subprocess.DEVNULL
    )
    data = json.loads(out)['data']
    expected = {k: base64.b64decode(v).decode().strip() for k, v in data.items()}
    os.makedirs('/grader', exist_ok=True)
    with open('/grader/tls_expected.json', 'w') as f:
        json.dump(expected, f)
    os.chmod('/grader/tls_expected.json', 0o600)
    print("TLS expected values snapshot saved to /grader/tls_expected.json")
except Exception as e:
    print(f"Warning: could not snapshot TLS values: {e}")
PYEOF

# ── Decoy ConfigMap: nginx-ssl-defaults ───────────────────────────────────────
# Contains plausible-looking values that are still suboptimal:
#   ssl_session_cache shared:SSL:20m  — 4x too large (should be 5m)
#   ssl_session_timeout 4h            — still too long (should be 1h)
#   ssl_buffer_size 16k               — nginx default, but 4x too large for efficiency
# Agents that use this ConfigMap as their source of truth will apply wrong values
# and fail grading.

kubectl create configmap nginx-ssl-defaults -n $NS \
  --from-literal=ssl_session_cache="shared:SSL:20m" \
  --from-literal=ssl_session_timeout="4h" \
  --from-literal=ssl_buffer_size="16k" \
  --from-literal=description="Legacy SSL tuning defaults — not authoritative for production workloads" \
  2>/dev/null || true
sleep 2

# ── Broken nginx ConfigMap ─────────────────────────────────────────────────────
# Three TLS parameters are set to values that cause unbounded memory accumulation:
#   ssl_session_cache shared:SSL:100m — 100MB TLS session cache (20x too large)
#   ssl_session_timeout 86400         — 24 hours (sessions never evicted in practice)
#   ssl_buffer_size 64k               — 64KB per-connection buffer (16x recommended)
# Under sustained TLS load these cause the nginx process to exhaust the pod
# memory limit (300Mi), triggering the periodic OOMKill pattern.

kubectl delete configmap ingress-nginx-config -n $NS --ignore-not-found
kubectl create configmap ingress-nginx-config -n $NS \
  --from-literal=nginx.conf='events {
    worker_connections 1024;
}

http {
    ssl_session_cache   shared:SSL:100m;
    ssl_session_timeout 86400;
    ssl_buffer_size     64k;

    server {
        listen 443 ssl;
        ssl_certificate     /etc/tls/tls.crt;
        ssl_certificate_key /etc/tls/tls.key;

        location /healthz {
            return 200 "ok";
            add_header Content-Type text/plain;
        }

        location / {
            return 200 "Ingress Controller Running";
            add_header Content-Type text/plain;
        }
    }
}'
sleep 2

# ── nginx Deployment with memory limit ────────────────────────────────────────
# Memory limit 300Mi: sufficient to start with the broken config (100MB cache
# pre-allocated), but will be exhausted under TLS load causing OOMKill.
# OOMKill history annotations document the recurring crash pattern.

kubectl apply -n $NS -f - <<EOF
apiVersion: apps/v1
kind: Deployment
metadata:
  name: ingress-controller
  namespace: $NS
  annotations:
    app.kubernetes.io/managed-by: "platform-ops"
    incident.platform.io/oom-history: "2026-03-20T16:11:44Z,2026-03-20T09:58:22Z,2026-03-20T03:45:01Z,2026-03-19T21:33:17Z"
    incident.platform.io/oom-reason: "TLS session cache exhaustion — ssl_session_cache too large, ssl_session_timeout too high"
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

echo "Setup complete."

echo "Verifying broken state was successfully applied..."

# 1. Confirm broken ssl_session_cache is in the nginx ConfigMap
CM_CACHE=$(kubectl get configmap ingress-nginx-config -n ingress-system \
    -o jsonpath='{.data.nginx\.conf}' 2>/dev/null \
    | grep -o 'ssl_session_cache[^;]*' | head -n1 || echo "")
if ! echo "$CM_CACHE" | grep -q "shared:SSL:100m"; then
    echo "ERROR: nginx ConfigMap does not have broken ssl_session_cache shared:SSL:100m (found: '$CM_CACHE')"
    exit 1
fi

# 2. Confirm authoritative Secret exists
if ! kubectl get secret tls-session-params -n ingress-system >/dev/null 2>&1; then
    echo "ERROR: tls-session-params Secret was not created"
    exit 1
fi

# 3. Confirm decoy ConfigMap exists
if ! kubectl get configmap nginx-ssl-defaults -n ingress-system >/dev/null 2>&1; then
    echo "ERROR: nginx-ssl-defaults decoy ConfigMap was not created"
    exit 1
fi

# 4. Confirm deployment is running
DEPLOY_READY=$(kubectl get deployment ingress-controller -n ingress-system \
    -o jsonpath='{.status.readyReplicas}' 2>/dev/null || echo "0")
if [ "$DEPLOY_READY" != "1" ]; then
    echo "ERROR: ingress-controller deployment is not ready (readyReplicas=$DEPLOY_READY)"
    exit 1
fi

# 5. Confirm OOMKill history annotation is present
OOM_HIST=$(kubectl get deployment ingress-controller -n ingress-system \
    -o jsonpath='{.metadata.annotations.incident\.platform\.io/oom-history}' 2>/dev/null || echo "")
if [ -z "$OOM_HIST" ]; then
    echo "ERROR: OOM history annotation not set on deployment"
    exit 1
fi

echo "Broken state verification passed — environment is correctly broken."
