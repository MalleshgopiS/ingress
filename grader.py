#!/usr/bin/env python3
"""Grader: ingress-tls-memory-leak

Verifies that the agent correctly identified and fixed the three broken nginx
TLS session parameters that were causing periodic OOMKill restarts.
"""

import base64
import datetime
import json
import re
import subprocess
import time
from apex_arena._types import GradingResult

# TLS expected values loaded inside grade() at grading time.
# Values are NOT hardcoded — reading this file does not reveal the fix.
EXPECTED_TLS: dict = {}   # filled inside grade() each time it is called

NS     = "ingress-system"
DEPLOY = "ingress-controller"
SVC    = "ingress-controller-svc"

# ── Broken values planted by setup ────────────────────────────────────────────
# ssl_session_cache builtin = OpenSSL per-worker cache with NO size limit
# (exactly matches issue #488: "No size limit, stale sessions never evicted")
BROKEN_CACHE   = "builtin"
BROKEN_TIMEOUT = "86400"
BROKEN_BUFFER  = "64k"

# ── Decoy values (nginx-ssl-defaults ConfigMap — wrong but plausible) ─────────
DECOY_CACHE   = "shared:SSL:20m"
DECOY_TIMEOUT = "4h"
DECOY_BUFFER  = "16k"


# ── Shell helper ───────────────────────────────────────────────────────────────

def run(cmd: str, timeout: int = 30):
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except subprocess.TimeoutExpired:
        return -1, "", "command timed out"
    except Exception as exc:
        return -1, "", str(exc)


# ── Load authoritative TLS values from cluster at grading time ────────────────

def _load_expected_tls() -> dict:
    """Read the three TLS tuning values.
    Reads from a protected snapshot file written during setup — prevents an agent
    from gaming the check by modifying the tls-session-params Secret after it is read.
    Falls back to reading the live Secret if the snapshot is unavailable."""
    # Primary: protected snapshot in /grader (chmod 700 — agent cannot access)
    try:
        with open("/grader/tls_expected.json") as f:
            data = json.load(f)
            return {
                "cache":   data.get("ssl_session_cache", "").strip(),
                "timeout": data.get("ssl_session_timeout", "").strip(),
                "buffer":  data.get("ssl_buffer_size", "").strip(),
            }
    except Exception:
        pass
    # Fallback: read from live Secret (e.g. when running outside container)
    _, out, _ = run(f"kubectl get secret tls-session-params -n {NS} -o json 2>/dev/null")
    try:
        data = json.loads(out).get("data", {})
        return {
            "cache":   base64.b64decode(data["ssl_session_cache"]).decode().strip(),
            "timeout": base64.b64decode(data["ssl_session_timeout"]).decode().strip(),
            "buffer":  base64.b64decode(data["ssl_buffer_size"]).decode().strip(),
        }
    except Exception:
        return {}   # Secret missing or unreadable — all TLS checks will return False


# ── Cluster helpers ────────────────────────────────────────────────────────────

def _get_configmap_conf() -> str:
    _, out, _ = run(
        f"kubectl get configmap ingress-nginx-config -n {NS} "
        "-o jsonpath='{.data.nginx\\.conf}'"
    )
    return out


def _get_running_pod() -> str:
    _, out, _ = run(
        f"kubectl get pods -n {NS} -l app=ingress-controller "
        "--field-selector=status.phase=Running "
        "-o jsonpath='{.items[0].metadata.name}'"
    )
    return out.strip()


def _get_cluster_ip() -> str:
    _, out, _ = run(
        f"kubectl get svc {SVC} -n {NS} -o jsonpath='{{.spec.clusterIP}}'"
    )
    return out.strip()


def _get_restart_count() -> str:
    _, out, _ = run(
        f"kubectl get pod -n {NS} -l app=ingress-controller "
        "-o jsonpath='{{.items[0].status.containerStatuses[0].restartCount}}'"
    )
    return out.strip() or "0"


# ── TLS value matchers ─────────────────────────────────────────────────────────

def _cache_ok(text: str) -> bool:
    val = EXPECTED_TLS.get("cache")
    if not val:
        return False
    return bool(re.search(rf"ssl_session_cache\s+{re.escape(val)}\s*;", text or ""))


def _timeout_ok(text: str) -> bool:
    val = EXPECTED_TLS.get("timeout")
    if not val:
        return False
    return bool(re.search(rf"ssl_session_timeout\s+{re.escape(val)}\s*;", text or ""))


def _buffer_ok(text: str) -> bool:
    val = EXPECTED_TLS.get("buffer")
    if not val:
        return False
    return bool(re.search(rf"ssl_buffer_size\s+{re.escape(val)}\s*;", text or ""))


# ── Objective 1: tls_params_corrected ─────────────────────────────────────────
# nginx ConfigMap has all three correct TLS values from the authoritative Secret.
# Also checks that neither the original broken values nor the decoy values were used.
# Retries 3× with 5s gaps to tolerate ConfigMap write propagation delays.

def _obj_tls_params_corrected() -> tuple[float, str]:
    checks = {}
    for attempt in range(3):
        cfg = _get_configmap_conf()
        checks = {
            "cache_correct":   _cache_ok(cfg),
            "timeout_correct": _timeout_ok(cfg),
            "buffer_correct":  _buffer_ok(cfg),
            "not_broken":      not any([
                re.search(rf"ssl_session_cache\s+{re.escape(BROKEN_CACHE)}\s*;",   cfg),
                re.search(rf"ssl_session_timeout\s+{re.escape(BROKEN_TIMEOUT)}\s*;", cfg),
                re.search(rf"ssl_buffer_size\s+{re.escape(BROKEN_BUFFER)}\s*;",    cfg),
            ]),
            "not_decoy":       not any([
                re.search(rf"ssl_session_cache\s+{re.escape(DECOY_CACHE)}\s*;",    cfg),
                re.search(rf"ssl_session_timeout\s+{re.escape(DECOY_TIMEOUT)}\s*;", cfg),
            ]),
        }
        if all(checks.values()):
            break
        if attempt < 2:
            time.sleep(5)
    n      = sum(checks.values())
    detail = ", ".join(f"{'✓' if ok else '✗'} {k}" for k, ok in checks.items())
    score  = 1.0 if all(checks.values()) else 0.0
    return score, f"{n}/{len(checks)} ConfigMap TLS param checks — {detail}"


# ── Objective 2: nginx_live_updated ───────────────────────────────────────────
# Live nginx process reflects all three correct TLS values.
# Patching the ConfigMap alone is not sufficient — nginx must be reloaded
# (nginx -s reload / SIGHUP) or the pod must have restarted.
# Retries 6× with 10s gaps (up to 60s) to allow nginx reload to complete.

def _obj_nginx_live_updated() -> tuple[float, str]:
    # Allow time for kubelet to sync the ConfigMap volume and nginx reload to propagate
    pod = _get_running_pod()
    if not pod:
        # Pod may be restarting — wait up to 60s for it to come back
        for _ in range(6):
            time.sleep(10)
            pod = _get_running_pod()
            if pod:
                break
    if not pod:
        return 0.0, "No running nginx pod found — cannot verify live config"

    checks = {}
    for attempt in range(6):
        # Use `nginx -T` to dump the RUNNING in-memory configuration.
        # This is the only reliable way to confirm nginx has actually reloaded —
        # `cat /etc/nginx/nginx.conf` reflects kubelet file-sync but NOT nginx reload.
        _, live, _ = run(f"kubectl exec -n {NS} {pod} -- nginx -T 2>/dev/null", timeout=15)
        _, out, err = run(f"kubectl exec -n {NS} {pod} -- nginx -t", timeout=10)
        syntax_ok = "syntax is ok" in (out + err).lower()
        checks = {
            "live_cache_correct":   _cache_ok(live),
            "live_timeout_correct": _timeout_ok(live),
            "live_buffer_correct":  _buffer_ok(live),
            "live_not_broken":      not any([
                re.search(rf"ssl_session_cache\s+{re.escape(BROKEN_CACHE)}\s*;",    live),
                re.search(rf"ssl_session_timeout\s+{re.escape(BROKEN_TIMEOUT)}\s*;", live),
            ]),
            "nginx_syntax_valid":   syntax_ok,
        }
        if all(checks.values()):
            break
        if attempt < 5:
            time.sleep(10)
    n      = sum(checks.values())
    detail = ", ".join(f"{'✓' if ok else '✗'} {k}" for k, ok in checks.items())
    score  = 1.0 if all(checks.values()) else 0.0
    return score, f"{n}/{len(checks)} live nginx checks — {detail}"


# ── Objective 3: pod_stable ───────────────────────────────────────────────────
# Pod is running and no new OOMKill restarts occurred during the grading window.
# Waits up to 90s for deployment to become ready, then observes for 30s to
# detect any immediate OOMKill triggered by an incorrect fix.

def _obj_pod_stable() -> tuple[float, str]:
    # Pre-wait: allow the deployment to reach readyReplicas=1 before sampling the
    # restart baseline. This prevents false failures when the grader runs while
    # a pod is mid-restart after nginx -s reload or a rollout restart.
    for _ in range(9):
        _, r, _ = run(
            f"kubectl get deploy {DEPLOY} -n {NS} "
            "-o jsonpath='{{.status.readyReplicas}}'"
        )
        if r == "1":
            break
        time.sleep(10)

    restart_before = _get_restart_count()

    # Observe window: detect any immediate OOMKill caused by a bad fix
    time.sleep(30)

    _, ready, _ = run(
        f"kubectl get deploy {DEPLOY} -n {NS} "
        "-o jsonpath='{{.status.readyReplicas}}'"
    )
    restart_after = _get_restart_count()

    checks = {
        "deployment_ready": ready == "1",
        "no_new_restarts":  restart_after == restart_before,
    }

    # Check for recent OOMKill events (last 5 minutes)
    _, events_out, _ = run(
        f"kubectl get events -n {NS} --field-selector=reason=OOMKilling "
        "--sort-by=.metadata.creationTimestamp -o json 2>/dev/null"
    )
    recent_oom = False
    try:
        items = json.loads(events_out).get("items", [])
        now   = datetime.datetime.utcnow()
        for ev in items:
            ts = ev.get("lastTimestamp") or ev.get("metadata", {}).get("creationTimestamp", "")
            if ts:
                try:
                    t = datetime.datetime.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S")
                    if (now - t).total_seconds() < 300:
                        recent_oom = True
                        break
                except Exception:
                    pass
    except Exception:
        pass
    checks["no_recent_oomkill"] = not recent_oom

    n      = sum(checks.values())
    detail = ", ".join(f"{'✓' if ok else '✗'} {k}" for k, ok in checks.items())
    score  = 1.0 if all(checks.values()) else 0.0
    return score, f"{n}/{len(checks)} pod stability checks — {detail}"


# ── Objective 4: https_operational ────────────────────────────────────────────
# HTTPS endpoint responds correctly, TLS handshake succeeds, nginx syntax valid.

def _obj_https_operational() -> tuple[float, str]:
    ip = _get_cluster_ip()
    if not ip:
        return 0.0, "Could not resolve ClusterIP"

    checks = {}

    # HTTPS responds to health probe
    https_ok = False
    for _ in range(6):
        _, body, _ = run(f"curl -k -s --max-time 5 https://{ip}/healthz")
        if "ok" in body.lower():
            https_ok = True
            break
        time.sleep(5)
    checks["https_responds"] = https_ok

    # TLS handshake succeeds (openssl s_client)
    _, tls_out, tls_err = run(
        f"echo Q | openssl s_client -connect {ip}:443 2>&1 | head -10",
        timeout=15
    )
    combined = (tls_out + tls_err).lower()
    checks["tls_handshake_ok"] = (
        "ssl-session" in combined or
        "cipher" in combined or
        "connected" in combined
    )

    # nginx configuration syntax valid
    pod = _get_running_pod()
    if pod:
        _, out, err = run(f"kubectl exec -n {NS} {pod} -- nginx -t", timeout=10)
        checks["nginx_syntax_valid"] = "syntax is ok" in (out + err).lower()
    else:
        checks["nginx_syntax_valid"] = False

    n      = sum(checks.values())
    detail = ", ".join(f"{'✓' if ok else '✗'} {k}" for k, ok in checks.items())
    score  = 1.0 if all(checks.values()) else 0.0
    return score, f"{n}/{len(checks)} HTTPS operational checks — {detail}"


# ── Grouped milestone functions ────────────────────────────────────────────────

def _milestone_tls_params_corrected() -> tuple[float, str]:
    """Milestone 1: All three broken TLS params corrected in nginx ConfigMap
    using values from the authoritative Secret (not the decoy ConfigMap)."""
    return _obj_tls_params_corrected()


def _milestone_nginx_live_updated() -> tuple[float, str]:
    """Milestone 2: Live nginx process has reloaded and reflects the corrected
    TLS parameters — ConfigMap update alone does not satisfy this check."""
    return _obj_nginx_live_updated()


def _milestone_pod_stable() -> tuple[float, str]:
    """Milestone 3: Pod running normally with no new OOMKill events since
    the fix was applied."""
    return _obj_pod_stable()


def _milestone_https_operational() -> tuple[float, str]:
    """Milestone 4: HTTPS endpoint responds correctly with a valid TLS
    handshake and valid nginx configuration syntax."""
    return _obj_https_operational()


# ── Grade ──────────────────────────────────────────────────────────────────────
# Four milestones × 0.25 weight = 1.0 maximum score.
# Score distribution: 0.00 / 0.25 / 0.50 / 0.75 / 1.00

OBJECTIVES = [
    ("tls_params_corrected", _milestone_tls_params_corrected),
    ("nginx_live_updated",   _milestone_nginx_live_updated),
    ("pod_stable",           _milestone_pod_stable),
    ("https_operational",    _milestone_https_operational),
]


def grade(transcript: str = None) -> GradingResult:
    try:
        # Load authoritative TLS values from cluster at grading time (not at import time)
        EXPECTED_TLS.update(_load_expected_tls())

        # ── Run all four milestones ────────────────────────────────────────────
        subscores: dict = {}
        milestone_feedback = []

        for name, fn in OBJECTIVES:
            score, message = fn()
            subscores[name] = score
            status = "PASS" if score >= 1.0 else "FAIL"
            milestone_feedback.append(f"{status} {name}: {message}")

        # Equal weights (required) — built after subscores are filled
        weights: dict = {k: 0.25 for k in subscores.keys()}

        # Final score is weighted sum of subscores
        final_score = 0.0
        for k, v in subscores.items():
            final_score += v * weights[k]

        passed_count = sum(1 for v in subscores.values() if v >= 1.0)

        feedback_parts = [
            f"Score={final_score:.2f}",
            f"Subscores={subscores}",
            f"MilestonesPassed={passed_count}/{len(OBJECTIVES)}",
        ] + milestone_feedback

        return GradingResult(
            score=final_score,
            subscores=subscores,
            weights=weights,
            feedback=" | ".join(feedback_parts),
        )

    except Exception as e:
        return GradingResult(score=0.0, feedback=f"Grader error: {str(e)}")
