#!/usr/bin/env python3
"""Grader: ingress-tls-memory-leak

Verifies that the agent correctly identified and fixed the three broken nginx
TLS session parameters that were causing periodic OOMKill restarts.

Two-tier grading strategy:
  • ConfigMap (tls_params_corrected) — range-based: any valid bounded value passes.
    This rewards agents who correctly identify and fix the broken parameters
    regardless of which specific source they used.
  • Live nginx (nginx_live_updated) — exact-match: values must match the
    platform-authoritative tls-session-params Secret. This rewards agents who
    discovered and used the correct platform standard, not just any bounded value.
"""

import base64
import datetime
import json
import re
import subprocess
import time
from apex_arena._types import GradingResult

# Authoritative TLS values — loaded from protected snapshot at grading time.
# Used only for the live nginx check (nginx_live_updated milestone).
EXPECTED_TLS: dict = {}   # populated inside grade() each run

NS     = "ingress-system"
DEPLOY = "ingress-controller"
SVC    = "ingress-controller-svc"

# ── Broken values planted by setup ────────────────────────────────────────────
# ssl_session_cache builtin = OpenSSL per-worker cache with NO size limit
# ssl_session_timeout 86400 = raw seconds (24 h) — sessions never evicted
# ssl_buffer_size 64k       = 4× typical TLS record size, wastes memory per conn
BROKEN_CACHE   = "builtin"
BROKEN_TIMEOUT = "86400"
BROKEN_BUFFER  = "64k"


# ── Shell helper ───────────────────────────────────────────────────────────────

def run(cmd: str, timeout: int = 30):
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except subprocess.TimeoutExpired:
        return -1, "", "command timed out"
    except Exception as exc:
        return -1, "", str(exc)


# ── Load authoritative TLS values (for live nginx exact-match check) ──────────

def _load_expected_tls() -> dict:
    """Read the three platform-standard TLS values at grading time.
    Primary source: protected snapshot written by setup (chmod 700 — agent cannot read).
    Fallback: live tls-session-params Secret (readable by agent, but grader snapshot
    prevents gaming by Secret modification)."""
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
    _, out, _ = run(f"kubectl get secret tls-session-params -n {NS} -o json 2>/dev/null")
    try:
        data = json.loads(out).get("data", {})
        return {
            "cache":   base64.b64decode(data["ssl_session_cache"]).decode().strip(),
            "timeout": base64.b64decode(data["ssl_session_timeout"]).decode().strip(),
            "buffer":  base64.b64decode(data["ssl_buffer_size"]).decode().strip(),
        }
    except Exception:
        return {}


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


# ── TLS value validators (range-based — any valid bounded value passes) ────────

def _cache_bounded(text: str) -> bool:
    """ssl_session_cache must use a named shared:SSL: zone, not per-worker builtin."""
    return bool(re.search(r'ssl_session_cache\s+shared:SSL:\d+\w*\s*;', text or "", re.I))


def _timeout_bounded(text: str) -> bool:
    """ssl_session_timeout must carry a time-unit suffix (s/m/h/d), not raw 86400."""
    m = re.search(r'ssl_session_timeout\s+(\S+)\s*;', text or "")
    if not m:
        return False
    val = m.group(1)
    return val != BROKEN_TIMEOUT and bool(re.match(r'^\d+[smhd]$', val, re.I))


def _buffer_bounded(text: str) -> bool:
    """ssl_buffer_size must be a k-unit value that is not the oversized 64k."""
    m = re.search(r'ssl_buffer_size\s+(\S+)\s*;', text or "")
    if not m:
        return False
    val = m.group(1)
    return bool(re.match(r'^\d+k$', val, re.I)) and val.lower() != BROKEN_BUFFER


def _not_broken(text: str) -> bool:
    """None of the three original broken values remain in the config."""
    return (
        not re.search(rf'ssl_session_cache\s+{re.escape(BROKEN_CACHE)}\s*;',    text or "")
        and not re.search(rf'ssl_session_timeout\s+{re.escape(BROKEN_TIMEOUT)}\s*;', text or "")
        and not re.search(rf'ssl_buffer_size\s+{re.escape(BROKEN_BUFFER)}\s*;',  text or "")
    )


def _all_params_present(text: str) -> bool:
    """All three TLS directives are present in the config."""
    return all([
        re.search(r'ssl_session_cache\s+\S',   text or ""),
        re.search(r'ssl_session_timeout\s+\S',  text or ""),
        re.search(r'ssl_buffer_size\s+\S',      text or ""),
    ])


# ── Objective 1: tls_params_corrected ─────────────────────────────────────────
# nginx ConfigMap has all three TLS parameters corrected to valid bounded values.
# Any properly bounded value is accepted — no specific reference value required.
# Retries 3× with 5s gaps to tolerate ConfigMap write propagation delays.

def _obj_tls_params_corrected() -> tuple[float, str]:
    checks = {}
    for attempt in range(3):
        cfg = _get_configmap_conf()
        checks = {
            "cache_bounded":   _cache_bounded(cfg),
            "timeout_bounded": _timeout_bounded(cfg),
            "buffer_bounded":  _buffer_bounded(cfg),
            "not_broken":      _not_broken(cfg),
            "all_params_set":  _all_params_present(cfg),
        }
        if all(checks.values()):
            break
        if attempt < 2:
            time.sleep(5)
    n      = sum(checks.values())
    detail = ", ".join(f"{'✓' if ok else '✗'} {k}" for k, ok in checks.items())
    score  = 1.0 if all(checks.values()) else 0.0
    return score, f"{n}/{len(checks)} ConfigMap TLS param checks — {detail}"


# ── Exact-match helpers (live nginx only — uses platform Secret values) ────────

def _live_cache_exact(text: str) -> bool:
    val = EXPECTED_TLS.get("cache", "")
    return bool(val and re.search(rf'ssl_session_cache\s+{re.escape(val)}\s*;', text or ""))


def _live_timeout_exact(text: str) -> bool:
    val = EXPECTED_TLS.get("timeout", "")
    return bool(val and re.search(rf'ssl_session_timeout\s+{re.escape(val)}\s*;', text or ""))


def _live_buffer_exact(text: str) -> bool:
    val = EXPECTED_TLS.get("buffer", "")
    return bool(val and re.search(rf'ssl_buffer_size\s+{re.escape(val)}\s*;', text or ""))


# ── Objective 2: nginx_live_updated ───────────────────────────────────────────
# Live nginx process reflects the platform-standard TLS values from the
# tls-session-params Secret — not just any bounded value.
# ConfigMap update alone is not sufficient — nginx must be reloaded or the
# pod restarted so the running process picks up the corrected configuration.
# Retries 6× with 10s gaps (up to 60s) to allow nginx reload to complete.

def _obj_nginx_live_updated() -> tuple[float, str]:
    if not EXPECTED_TLS:
        return 0.0, "Could not load platform TLS reference values — check tls-session-params Secret"

    pod = _get_running_pod()
    if not pod:
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
            "live_cache_correct":   _live_cache_exact(live),
            "live_timeout_correct": _live_timeout_exact(live),
            "live_buffer_correct":  _live_buffer_exact(live),
            "live_not_broken":      _not_broken(live),
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
# Waits up to 90s for a Running pod, then observes for 30s to detect any
# immediate OOMKill triggered by an incorrect fix.
#
# NOTE: Uses pod-level phase (status.phase=Running) rather than deployment-level
# readyReplicas. readyReplicas is updated by the deployment controller
# asynchronously and can lag significantly in loaded k3s environments; pod phase
# is set by the kubelet on the node and is always current.

def _obj_pod_stable() -> tuple[float, str]:
    # Pre-wait: allow a Running pod to appear before sampling the restart baseline.
    pod = _get_running_pod()
    for _ in range(9):
        if pod:
            break
        time.sleep(10)
        pod = _get_running_pod()

    restart_before = _get_restart_count()

    # Observe window: detect any immediate OOMKill caused by a bad fix
    time.sleep(30)

    # Confirm a Running pod still exists after the observation window.
    # Retry up to 6× with 5s gaps to handle the brief period when an OOMKilled
    # pod is being rescheduled (which should NOT pass this check).
    pod_after = ""
    for _ in range(6):
        pod_after = _get_running_pod()
        if pod_after:
            break
        time.sleep(5)

    restart_after = _get_restart_count()

    checks = {
        "deployment_ready": bool(pod_after),
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
    """Milestone 1: All three broken TLS params corrected to valid bounded values
    in the nginx ConfigMap. Any properly bounded value is accepted."""
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
        # Load platform-standard TLS values at grading time (used by nginx_live_updated)
        EXPECTED_TLS.update(_load_expected_tls())

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
        final_score = sum(v * weights[k] for k, v in subscores.items())

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
