#!/bin/bash
set -euo pipefail

NS=ingress-system
DEPLOY=ingress-controller

echo "=== Applying TLS memory leak remediation ==="

# ── Step 1: Diagnose the broken TLS configuration ─────────────────────────────
# The three nginx TLS parameters in the ConfigMap are misconfigured:
#   ssl_session_cache builtin    — per-worker, no size limit → unbounded heap growth
#   ssl_session_timeout 86400    — 24-hour sessions → stale sessions never evicted
#   ssl_buffer_size 64k          — 64 KB per connection → excessive per-connection cost
#
# Correct bounded values that eliminate the memory leak:
#   ssl_session_cache shared:SSL:5m  — single shared zone capped at 5 MB
#   ssl_session_timeout 1h           — 1-hour session lifetime (3600 s ≤ limit)
#   ssl_buffer_size 4k               — 4 KB per connection (standard recommendation)
#
# NOTE: There is no authoritative Secret containing correct values — these
# values are derived from nginx operational best practices for a 300 Mi pod.
# Do NOT use the tls-session-params Secret — it contains legacy/excessive values
# (shared:SSL:32m, 8h, 32k) that exceed safe bounds and will not fix the OOM.

SSL_CACHE="shared:SSL:5m"
SSL_TIMEOUT="1h"
SSL_BUFFER="4k"

echo "[Step 1] Bounded replacement values:"
echo "    ssl_session_cache   = $SSL_CACHE   (replaces: builtin — unbounded)"
echo "    ssl_session_timeout = $SSL_TIMEOUT  (replaces: 86400 — 24-hour sessions)"
echo "    ssl_buffer_size     = $SSL_BUFFER    (replaces: 64k — excessive per-connection)"

# ── Step 2: Patch nginx ConfigMap — surgically, not from scratch ───────────────
# Read the existing nginx.conf and replace ONLY the three broken TLS directives.
# This preserves all other original directives (keepalive_timeout, server_tokens,
# worker_connections, server block, etc).
# IMPORTANT: Do NOT reconstruct the entire nginx.conf — that loses existing config.

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
echo "    ssl_session_cache   → $SSL_CACHE   (was: builtin — unbounded per-worker)"
echo "    ssl_session_timeout → $SSL_TIMEOUT  (was: 86400 — 24-hour accumulation)"
echo "    ssl_buffer_size     → $SSL_BUFFER    (was: 64k — 16x recommended size)"
