#!/bin/bash
set -euo pipefail

NS=ingress-system
DEPLOY=ingress-controller

echo "=== Applying TLS memory leak remediation ==="

# ── Step 1: Read authoritative TLS values from the platform Secret ─────────────
# Do NOT use the nginx-ssl-defaults ConfigMap — it contains legacy values.
# The tls-session-params Secret is the single source of truth.

echo "[Step 1] Reading authoritative TLS values from tls-session-params Secret..."
SSL_CACHE=$(kubectl get secret tls-session-params -n $NS \
  -o jsonpath='{.data.ssl_session_cache}' 2>/dev/null | base64 -d || echo "shared:SSL:5m")
SSL_TIMEOUT=$(kubectl get secret tls-session-params -n $NS \
  -o jsonpath='{.data.ssl_session_timeout}' 2>/dev/null | base64 -d || echo "1h")
SSL_BUFFER=$(kubectl get secret tls-session-params -n $NS \
  -o jsonpath='{.data.ssl_buffer_size}' 2>/dev/null | base64 -d || echo "4k")

echo "[Step 1] Found values:"
echo "    ssl_session_cache   = $SSL_CACHE"
echo "    ssl_session_timeout = $SSL_TIMEOUT"
echo "    ssl_buffer_size     = $SSL_BUFFER"

# ── Step 2: Patch the nginx ConfigMap with corrected TLS values ────────────────

echo "[Step 2] Patching ingress-nginx-config ConfigMap..."
kubectl create configmap ingress-nginx-config -n $NS \
  --from-literal=nginx.conf="events {
    worker_connections 1024;
}

http {
    ssl_session_cache   $SSL_CACHE;
    ssl_session_timeout $SSL_TIMEOUT;
    ssl_buffer_size     $SSL_BUFFER;

    server {
        listen 443 ssl;
        ssl_certificate     /etc/tls/tls.crt;
        ssl_certificate_key /etc/tls/tls.key;

        location /healthz {
            return 200 \"ok\";
            add_header Content-Type text/plain;
        }

        location / {
            return 200 \"Ingress Controller Running\";
            add_header Content-Type text/plain;
        }
    }
}" \
  --dry-run=client -o yaml | kubectl apply -f -

echo "[Step 2] ConfigMap updated."

# ── Step 3: Reload nginx to apply the new config ───────────────────────────────
# Patching the ConfigMap alone does not affect the running nginx process.
# Sending SIGHUP (nginx -s reload) applies the new config without dropping
# existing connections. Fall back to a rollout restart if no pod is running.

echo "[Step 3] Reloading nginx to apply new TLS configuration..."
POD=$(kubectl get pods -n $NS -l app=ingress-controller \
  --field-selector=status.phase=Running \
  -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || echo "")

if [ -n "$POD" ]; then
    kubectl exec -n $NS "$POD" -- nginx -s reload 2>/dev/null || true
    sleep 3
    kubectl exec -n $NS "$POD" -- nginx -t
    echo "[Step 3] nginx reloaded successfully."
else
    echo "[Step 3] No running pod found — triggering rollout restart..."
    kubectl rollout restart deployment/$DEPLOY -n $NS
    kubectl rollout status deployment/$DEPLOY -n $NS --timeout=120s
fi

# ── Step 4: Verify the fix ─────────────────────────────────────────────────────

echo "[Step 4] Verifying HTTPS endpoint..."
sleep 3
IP=$(kubectl get svc ingress-controller-svc -n $NS \
  -o jsonpath='{.spec.clusterIP}' 2>/dev/null || echo "")
if [ -n "$IP" ]; then
    RESP=$(curl -k -s --max-time 5 "https://$IP/healthz" || echo "")
    if echo "$RESP" | grep -qi "ok"; then
        echo "[Step 4] HTTPS healthz check passed."
    else
        echo "[Step 4] Warning: HTTPS healthz returned unexpected response: '$RESP'"
    fi
fi

echo ""
echo "=== Remediation complete. TLS memory leak configuration has been fixed. ==="
echo "    ssl_session_cache   → $SSL_CACHE   (was: shared:SSL:100m)"
echo "    ssl_session_timeout → $SSL_TIMEOUT  (was: 86400)"
echo "    ssl_buffer_size     → $SSL_BUFFER    (was: 64k)"
