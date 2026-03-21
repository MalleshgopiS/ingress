#!/bin/bash
set -euo pipefail

NS=ingress-system
DEPLOY=ingress-controller

echo "=== Applying TLS memory leak remediation ==="

# ── Step 1: Read platform-standard TLS values ──────────────────────────────────
# The authoritative source is the platform-nginx-config Secret in ingress-system.
# This Secret is referenced by the deployment annotation:
#   config.nginx.io/tls-standards: platform-nginx-config
# Do NOT use the tls-session-params Secret — it contains legacy/decoy values.

echo "[Step 1] Reading platform TLS standards from platform-nginx-config Secret..."
SSL_CACHE=$(kubectl get secret platform-nginx-config -n $NS \
  -o jsonpath='{.data.ssl_session_cache}' | base64 -d)
SSL_TIMEOUT=$(kubectl get secret platform-nginx-config -n $NS \
  -o jsonpath='{.data.ssl_session_timeout}' | base64 -d)
SSL_BUFFER=$(kubectl get secret platform-nginx-config -n $NS \
  -o jsonpath='{.data.ssl_buffer_size}' | base64 -d)

echo "[Step 1] Platform-standard values:"
echo "    ssl_session_cache   = $SSL_CACHE"
echo "    ssl_session_timeout = $SSL_TIMEOUT"
echo "    ssl_buffer_size     = $SSL_BUFFER"

# ── Step 2: Patch nginx ConfigMap — surgically, not from scratch ───────────────
# Read the existing nginx.conf and replace ONLY the three broken TLS directives.
# This preserves all other original directives (keepalive_timeout, server_tokens, etc).
# IMPORTANT: Do NOT reconstruct the entire nginx.conf — that would lose existing config.

echo "[Step 2] Reading current nginx.conf from ConfigMap..."
CURRENT_CONF=$(kubectl get configmap ingress-nginx-config -n $NS \
  -o jsonpath='{.data.nginx\.conf}')

echo "[Step 2] Patching only the three broken TLS parameters in-place..."
PATCHED_CONF=$(echo "$CURRENT_CONF" \
  | sed "s|ssl_session_cache\s\+[^;]*;|ssl_session_cache   $SSL_CACHE;|" \
  | sed "s|ssl_session_timeout\s\+[^;]*;|ssl_session_timeout $SSL_TIMEOUT;|" \
  | sed "s|ssl_buffer_size\s\+[^;]*;|ssl_buffer_size     $SSL_BUFFER;|")

kubectl create configmap ingress-nginx-config -n $NS \
  --from-literal=nginx.conf="$PATCHED_CONF" \
  --dry-run=client -o yaml | kubectl apply -f -

echo "[Step 2] ConfigMap patched (original structure preserved)."

# ── Step 3: Rollout restart ────────────────────────────────────────────────────
# nginx.conf is mounted with subPath — kubelet does NOT auto-sync on ConfigMap
# change (documented Kubernetes behaviour for subPath mounts). A rollout restart
# is required so the new pod mounts the updated ConfigMap fresh at startup.

echo "[Step 3] Performing rollout restart to apply new TLS configuration..."
kubectl rollout restart deployment/$DEPLOY -n $NS
kubectl rollout status deployment/$DEPLOY -n $NS --timeout=120s
echo "[Step 3] Rollout complete — new pod has fresh ConfigMap volume."

sleep 15

ACTIVE_POD=$(kubectl get pods -n $NS -l app=ingress-controller \
  --field-selector=status.phase=Running \
  -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || echo "")
if [ -n "$ACTIVE_POD" ]; then
    kubectl exec -n $NS "$ACTIVE_POD" -- nginx -t
    echo "[Step 3] nginx configuration syntax OK."
fi

# ── Step 4: Verify ────────────────────────────────────────────────────────────

echo "[Step 4] Verifying HTTPS endpoint..."
sleep 3
IP=$(kubectl get svc ingress-controller-svc -n $NS \
  -o jsonpath='{.spec.clusterIP}' 2>/dev/null || echo "")
if [ -n "$IP" ]; then
    RESP=$(curl -k -s --max-time 5 "https://$IP/healthz" || echo "")
    if echo "$RESP" | grep -qi "ok"; then
        echo "[Step 4] HTTPS healthz check passed."
    else
        echo "[Step 4] Warning: unexpected response: '$RESP'"
    fi
fi

echo ""
echo "=== Remediation complete ==="
echo "    ssl_session_cache   → $SSL_CACHE   (was: builtin)"
echo "    ssl_session_timeout → $SSL_TIMEOUT  (was: 86400)"
echo "    ssl_buffer_size     → $SSL_BUFFER    (was: 64k)"
