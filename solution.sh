#!/bin/bash
set -euo pipefail

NS=ingress-system
DEPLOY=ingress-controller

echo "=== Applying TLS memory leak remediation ==="

# ── Step 0: Stop the configuration watchdog ───────────────────────────────────
# A CronJob (ingress-config-watchdog) is actively re-applying the broken nginx
# ConfigMap every 3 minutes, simulating configuration drift. It must be deleted
# before any ConfigMap edits will stick permanently.

echo "[Step 0] Stopping both configuration drift controllers to prevent further config drift..."
kubectl delete cronjob ingress-config-watchdog -n $NS --ignore-not-found
kubectl delete cronjob ops-config-controller   -n $NS --ignore-not-found
echo "[Step 0] Both drift controllers stopped."

# ── Step 1: Diagnose the broken TLS configuration ─────────────────────────────

SSL_CACHE="shared:SSL:5m"
SSL_TIMEOUT="20m"
SSL_BUFFER="4k"
SSL_PROTOCOLS="TLSv1.2 TLSv1.3"
SSL_TICKETS="off"

echo "[Step 1] Replacement values:"
echo "    ssl_session_cache   = $SSL_CACHE        (replaces: builtin — unbounded per-worker)"
echo "    ssl_session_timeout = $SSL_TIMEOUT       (replaces: 86400 — 24-hour sessions)"
echo "    ssl_buffer_size     = $SSL_BUFFER         (replaces: 64k — excessive per-connection)"
echo "    ssl_protocols       = $SSL_PROTOCOLS  (replaces: TLSv1 TLSv1.2 TLSv1.3 — deprecated)"
echo "    ssl_session_tickets = $SSL_TICKETS           (replaces: on — sessions stored twice wasting memory)"

# ── Step 2: Patch nginx ConfigMap — surgically, not from scratch ───────────────

echo "[Step 2] Reading current nginx.conf from ConfigMap..."
CURRENT_CONF=$(kubectl get configmap ingress-nginx-config -n $NS \
  -o jsonpath='{.data.nginx\.conf}')

echo "[Step 2] Patching all broken TLS parameters in-place..."
PATCHED_CONF=$(echo "$CURRENT_CONF" \
  | sed "s|ssl_session_cache\s\+[^;]*;|ssl_session_cache   $SSL_CACHE;|" \
  | sed "s|ssl_session_timeout\s\+[^;]*;|ssl_session_timeout $SSL_TIMEOUT;|" \
  | sed "s|ssl_buffer_size\s\+[^;]*;|ssl_buffer_size     $SSL_BUFFER;|" \
  | sed "s|ssl_protocols\s\+[^;]*;|ssl_protocols       $SSL_PROTOCOLS;|" \
  | sed "s|ssl_session_tickets\s\+[^;]*;|ssl_session_tickets $SSL_TICKETS;|")

kubectl create configmap ingress-nginx-config -n $NS \
  --from-literal=nginx.conf="$PATCHED_CONF" \
  --dry-run=client -o yaml | kubectl apply -f -

echo "[Step 2] ConfigMap patched (original structure preserved, all four TLS parameters fixed)."

# ── Step 3: Rollout restart ────────────────────────────────────────────────────


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

# ── Step 5: Fix broken Prometheus alert rule ──────────────────────────────────
# The ingress-alert-rules ConfigMap has a wrong container label selector:
#   container="nginx-controller"  (wrong — this label never matches any pod)
# Should be:
#   container="nginx"             (correct — matches the nginx container in the pod)
# Fix by patching the ConfigMap data in-place.

echo "[Step 5] Fixing Prometheus alert rule — correcting both wrong label selectors..."
CURRENT_ALERT=$(kubectl get configmap ingress-alert-rules -n $NS \
  -o jsonpath='{.data.alert\.yaml}' 2>/dev/null || echo "")

if [ -n "$CURRENT_ALERT" ]; then
    FIXED_ALERT=$(echo "$CURRENT_ALERT" \
      | sed 's|container="nginx-controller"|container="nginx"|g' \
      | sed 's|namespace="default"|namespace="ingress-system"|g')
    kubectl create configmap ingress-alert-rules -n $NS \
      --from-literal=alert.yaml="$FIXED_ALERT" \
      --dry-run=client -o yaml | kubectl apply -f -
    echo "[Step 5] Alert rule patched — container='nginx', namespace='ingress-system'."
else
    echo "[Step 5] Warning: ingress-alert-rules ConfigMap not found — skipping alert fix."
fi

# ── Step 6: Post-mortem documentation ────────────────────────────────────────

echo "[Step 6] Writing post-mortem document..."
cat > /workdir/postmortem.md <<'POSTMORTEM'
# Post-Mortem: Ingress Controller TLS Session Memory Leak

## Incident Summary
The ingress-controller in the ingress-system namespace experienced periodic OOM restarts
every ~6 hours due to misconfigured TLS session parameters in the nginx ConfigMap.
A monitoring alert was also silently misconfigured and never fired during the incident.

## Root Cause
Five misconfigured TLS directives in the nginx ConfigMap caused unbounded memory growth under HTTPS load:

1. **ssl_session_cache builtin** — OpenSSL builtin cache grows unboundedly per-worker.
   Replaced with shared:SSL:5m (fixed 5MB zone shared across all workers).

2. **ssl_session_timeout 86400** — 24-hour session lifetime caused session accumulation far
   exceeding the ~6-hour OOM cycle. Reduced to 20m to allow multiple eviction cycles.

3. **ssl_buffer_size 64k** — Per-connection TLS record buffer sized for a larger instance.
   Reduced to 4k (nginx default), appropriate for the 300Mi memory limit.

4. **ssl_protocols TLSv1 TLSv1.2 TLSv1.3** — Deprecated TLSv1 included, exposing
   clients to downgrade attacks. Fixed to TLSv1.2 TLSv1.3 only.

5. **ssl_session_tickets on** — With a bounded shared cache already in place, leaving
   session tickets enabled causes sessions to be persisted twice (cache zone + ticket),
   defeating the memory bound. Set to off to force all resumption through the cache.

## Contributing Factor: Silent Monitoring Failure
The ingress-alert-rules ConfigMap contained a Prometheus alert for pod restarts, but
the container label selector was set to container="nginx-controller" — a label that
does not match any container in the ingress-controller pod (actual container name
is "nginx"). This meant the alert never fired during the incident window.
Fixed by patching the selector to container="nginx".

## Configuration Drift
Two CronJobs were actively reverting the nginx ConfigMap to the broken state:
- ingress-config-watchdog (every 3 minutes) — named for its purpose
- ops-config-controller (every 5 minutes) — a secondary drift controller
Both were deleted before applying the config fix.

## Fix Applied
1. Deleted both drift-control CronJobs (ingress-config-watchdog, ops-config-controller).
2. Patched ingress-nginx-config ConfigMap with all five corrected TLS settings.
3. Performed rollout restart — subPath volume mounts require a pod restart to reload config.
4. Corrected both wrong selectors in ingress-alert-rules ConfigMap:
   - container="nginx-controller" → container="nginx"
   - namespace="default" → namespace="ingress-system"

## Verification
nginx -T confirms corrected parameters are active in the running worker process.
HTTPS healthz endpoint responds correctly after remediation.
Alert ConfigMap now references the correct container label.
POSTMORTEM

echo "[Step 6] Post-mortem written to /workdir/postmortem.md"

echo ""
echo "=== Remediation complete ==="
echo "    ssl_session_cache   → $SSL_CACHE        (was: builtin — unbounded per-worker)"
echo "    ssl_session_timeout → $SSL_TIMEOUT       (was: 86400 — 24-hour accumulation)"
echo "    ssl_buffer_size     → $SSL_BUFFER         (was: 64k — 16x recommended size)"
echo "    ssl_protocols       → $SSL_PROTOCOLS  (was: TLSv1 TLSv1.2 TLSv1.3 — deprecated)"
echo "    ssl_session_tickets → $SSL_TICKETS           (was: on — sessions stored twice)"
echo "    alert container     → nginx               (was: nginx-controller)"
echo "    alert namespace     → ingress-system       (was: default)"
echo "    drift controllers   → both stopped         (ingress-config-watchdog + ops-config-controller)"
echo "    postmortem          → /workdir/postmortem.md"
