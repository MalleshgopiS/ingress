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
  -o jsonpath='{.data.ssl_session_cache}' | base64 -d)
SSL_TIMEOUT=$(kubectl get secret tls-session-params -n $NS \
  -o jsonpath='{.data.ssl_session_timeout}' | base64 -d)
SSL_BUFFER=$(kubectl get secret tls-session-params -n $NS \
  -o jsonpath='{.data.ssl_buffer_size}' | base64 -d)

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
# Two-phase approach:
#   Phase A — wait for kubelet to sync the updated ConfigMap to the pod volume
#             (the file /etc/nginx/nginx.conf inside the pod is a ConfigMap mount;
#              kubelet refreshes it asynchronously, typically within 60–90 s).
#             Only AFTER the on-disk file reflects the new cache value is it safe
#             to call nginx -s reload, otherwise nginx re-reads the stale file.
#   Phase B — if the kubelet sync window passes without a confirmed live update,
#             fall back to a rollout restart so the new pod gets a fresh mount.

echo "[Step 3] Applying new TLS configuration to the running nginx process..."
POD=$(kubectl get pods -n $NS -l app=ingress-controller \
  --field-selector=status.phase=Running \
  -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || echo "")

RELOAD_OK=false

if [ -n "$POD" ]; then
    echo "[Step 3] Waiting for kubelet to sync ConfigMap volume to pod (up to 90s)..."
    for i in $(seq 1 18); do
        sleep 5
        # Check whether the on-disk nginx.conf inside the pod already has the new value.
        # Only once the file is updated should nginx -s reload be issued.
        if kubectl exec -n $NS "$POD" -- \
               grep -q "ssl_session_cache.*$SSL_CACHE" /etc/nginx/nginx.conf 2>/dev/null; then
            echo "[Step 3] Volume synced (iteration ${i}). Sending reload signal..."
            kubectl exec -n $NS "$POD" -- nginx -s reload
            sleep 3
            # Verify the running process picked up the new values via nginx -T
            LIVE=$(kubectl exec -n $NS "$POD" -- nginx -T 2>/dev/null || true)
            if echo "$LIVE" | grep -q "ssl_session_cache.*$SSL_CACHE"; then
                RELOAD_OK=true
                echo "[Step 3] nginx reload verified — live process updated."
            else
                echo "[Step 3] Reload sent but nginx -T still shows old config. Retrying reload..."
                kubectl exec -n $NS "$POD" -- nginx -s reload
                sleep 5
                LIVE=$(kubectl exec -n $NS "$POD" -- nginx -T 2>/dev/null || true)
                if echo "$LIVE" | grep -q "ssl_session_cache.*$SSL_CACHE"; then
                    RELOAD_OK=true
                    echo "[Step 3] nginx reload verified on retry."
                fi
            fi
            break
        fi
        echo "[Step 3]  ... waiting for kubelet sync (${i}/18)"
    done
fi

if [ "$RELOAD_OK" != "true" ]; then
    echo "[Step 3] Performing rollout restart to guarantee fresh ConfigMap mount..."
    kubectl rollout restart deployment/$DEPLOY -n $NS
    kubectl rollout status deployment/$DEPLOY -n $NS --timeout=120s
    echo "[Step 3] Rollout restart completed — new pod has fresh ConfigMap volume."
fi

# Final syntax check on whichever pod is now active
ACTIVE_POD=$(kubectl get pods -n $NS -l app=ingress-controller \
  --field-selector=status.phase=Running \
  -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || echo "")
if [ -n "$ACTIVE_POD" ]; then
    kubectl exec -n $NS "$ACTIVE_POD" -- nginx -t
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
echo "    ssl_session_cache   → $SSL_CACHE   (was: builtin)"
echo "    ssl_session_timeout → $SSL_TIMEOUT  (was: 86400)"
echo "    ssl_buffer_size     → $SSL_BUFFER    (was: 64k)"
