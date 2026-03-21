#!/usr/bin/env python3
"""Grader: ingress-tls-memory-leak

Verifies that the agent correctly identified and fixed the three broken nginx
TLS session parameters causing periodic OOMKill restarts.

Five milestones × 0.20 weight = 1.0 maximum score.

Grading strategy:
  • tls_params_corrected   — ConfigMap has exact platform-standard values
  • nginx_live_updated     — Live nginx process reflects exact platform values
  • config_structure_intact — Original nginx directives preserved (not rebuilt from scratch)
  • pod_stable             — No new OOMKill restarts during observation window
  • https_operational      — HTTPS endpoint responds with valid TLS
"""

import base64
import datetime
import json
import re
import subprocess
import time
from apex_arena._types import GradingResult

NS     = "ingress-system"
DEPLOY = "ingress-controller"
SVC    = "ingress-controller-svc"

# Authoritative values — loaded from protected snapshot at grading time
EXPECTED_TLS: dict = {}   # populated inside grade() each run

# Broken values planted by setup
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


# ── Load authoritative TLS values ─────────────────────────────────────────────

def _load_expected_tls() -> dict:
    """Read platform-standard TLS values from protected snapshot.
    Primary: /grader/tls_expected.json (chmod 600, agent cannot read).
    Fallback: live platform-nginx-config Secret."""
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
    _, out, _ = run(f"kubectl get secret platform-nginx-config -n {NS} -o json 2>/dev/null")
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


# ── Exact-match TLS helpers ────────────────────────────────────────────────────

def _cache_exact(text: str) -> bool:
    val = EXPECTED_TLS.get("cache", "")
    return bool(val and re.search(rf'ssl_session_cache\s+{re.escape(val)}\s*;', text or ""))


def _timeout_exact(text: str) -> bool:
    val = EXPECTED_TLS.get("timeout", "")
    return bool(val and re.search(rf'ssl_session_timeout\s+{re.escape(val)}\s*;', text or ""))


def _buffer_exact(text: str) -> bool:
    val = EXPECTED_TLS.get("buffer", "")
    return bool(val and re.search(rf'ssl_buffer_size\s+{re.escape(val)}\s*;', text or ""))


def _not_broken(text: str) -> bool:
    return (
        not re.search(rf'ssl_session_cache\s+{re.escape(BROKEN_CACHE)}\s*;',    text or "")
        and not re.search(rf'ssl_session_timeout\s+{re.escape(BROKEN_TIMEOUT)}\s*;', text or "")
        and not re.search(rf'ssl_buffer_size\s+{re.escape(BROKEN_BUFFER)}\s*;',  text or "")
    )


# ── Objective 1: tls_params_corrected ─────────────────────────────────────────
# ConfigMap has exact platform-standard TLS values from platform-nginx-config Secret.

def _obj_tls_params_corrected() -> tuple[float, str]:
    if not EXPECTED_TLS:
        return 0.0, "Could not load platform TLS reference values"
    checks = {}
    for attempt in range(3):
        cfg = _get_configmap_conf()
        checks = {
            "cache_correct":   _cache_exact(cfg),
            "timeout_correct": _timeout_exact(cfg),
            "buffer_correct":  _buffer_exact(cfg),
            "not_broken":      _not_broken(cfg),
            "not_decoy":       not re.search(r'ssl_session_cache\s+shared:SSL:32m\s*;', cfg or ""),
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
# Live nginx process reflects exact platform-standard TLS values.
# ConfigMap update alone is insufficient — nginx must be reloaded (rollout restart).
# Note: nginx.conf uses subPath mount — kubelet does NOT auto-sync on ConfigMap change.
# The agent must perform a rollout restart to get the new pod to mount fresh config.

def _obj_nginx_live_updated() -> tuple[float, str]:
    if not EXPECTED_TLS:
        return 0.0, "Could not load platform TLS reference values"

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
    for attempt in range(3):
        _, live, _ = run(f"kubectl exec -n {NS} {pod} -- nginx -T 2>/dev/null", timeout=15)
        _, out, err = run(f"kubectl exec -n {NS} {pod} -- nginx -t", timeout=10)
        syntax_ok = "syntax is ok" in (out + err).lower()
        checks = {
            "live_cache_correct":   _cache_exact(live),
            "live_timeout_correct": _timeout_exact(live),
            "live_buffer_correct":  _buffer_exact(live),
            "live_not_broken":      _not_broken(live),
            "nginx_syntax_valid":   syntax_ok,
        }
        if all(checks.values()):
            break
        if attempt < 2:
            time.sleep(10)
    n      = sum(checks.values())
    detail = ", ".join(f"{'✓' if ok else '✗'} {k}" for k, ok in checks.items())
    score  = 1.0 if all(checks.values()) else 0.0
    return score, f"{n}/{len(checks)} live nginx checks — {detail}"


# ── Objective 3: config_structure_intact ──────────────────────────────────────
# The original nginx.conf contained directives beyond the broken TLS parameters.
# A correct fix patches only the broken values in-place — it does NOT rebuild the
# entire nginx.conf from scratch, which would lose these original directives.
# Checks that keepalive_timeout and server_tokens (present in setup config) survived.

def _obj_config_structure_intact() -> tuple[float, str]:
    checks = {}
    for attempt in range(3):
        cfg = _get_configmap_conf()
        checks = {
            "keepalive_preserved":      bool(re.search(r'keepalive_timeout\s+\d+\s*;', cfg or "")),
            "server_tokens_preserved":  bool(re.search(r'server_tokens\s+off\s*;', cfg or "")),
            "worker_connections_intact": bool(re.search(r'worker_connections\s+1024\s*;', cfg or "")),
        }
        if all(checks.values()):
            break
        if attempt < 2:
            time.sleep(5)
    n      = sum(checks.values())
    detail = ", ".join(f"{'✓' if ok else '✗'} {k}" for k, ok in checks.items())
    score  = 1.0 if all(checks.values()) else 0.0
    return score, f"{n}/{len(checks)} config structure checks — {detail}"


# ── Objective 4: pod_stable ───────────────────────────────────────────────────

def _obj_pod_stable() -> tuple[float, str]:
    pod = _get_running_pod()
    for _ in range(9):
        if pod:
            break
        time.sleep(10)
        pod = _get_running_pod()

    restart_before = _get_restart_count()
    time.sleep(30)

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


# ── Objective 5: https_operational ────────────────────────────────────────────

def _obj_https_operational() -> tuple[float, str]:
    ip = _get_cluster_ip()
    if not ip:
        return 0.0, "Could not resolve ClusterIP"

    checks = {}

    https_ok = False
    for _ in range(6):
        _, body, _ = run(f"curl -k -s --max-time 5 https://{ip}/healthz")
        if "ok" in body.lower():
            https_ok = True
            break
        time.sleep(5)
    checks["https_responds"] = https_ok

    _, tls_out, tls_err = run(
        f"echo Q | openssl s_client -connect {ip}:443 2>&1 | head -10",
        timeout=15
    )
    combined = (tls_out + tls_err).lower()
    checks["tls_handshake_ok"] = (
        "ssl-session" in combined or
        "cipher"      in combined or
        "connected"   in combined
    )

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
    """Milestone 1: ConfigMap updated with exact platform-standard TLS values
    from the authoritative platform-nginx-config Secret."""
    return _obj_tls_params_corrected()


def _milestone_nginx_live_updated() -> tuple[float, str]:
    """Milestone 2: Live nginx process loaded the platform-standard TLS values.
    Requires rollout restart (subPath mount blocks kubelet auto-sync)."""
    return _obj_nginx_live_updated()


def _milestone_config_structure_intact() -> tuple[float, str]:
    """Milestone 3: Original nginx configuration directives preserved.
    The fix must surgically patch only the broken TLS parameters, not rebuild
    the entire nginx.conf (which would lose keepalive_timeout, server_tokens, etc)."""
    return _obj_config_structure_intact()


def _milestone_pod_stable() -> tuple[float, str]:
    """Milestone 4: Pod running normally with no new OOMKill events."""
    return _obj_pod_stable()


def _milestone_https_operational() -> tuple[float, str]:
    """Milestone 5: HTTPS endpoint responds with valid TLS and correct syntax."""
    return _obj_https_operational()


# ── Grade ──────────────────────────────────────────────────────────────────────
# Five milestones × 0.20 weight = 1.0 maximum score.
# Score distribution: 0.00 / 0.20 / 0.40 / 0.60 / 0.80 / 1.00

OBJECTIVES = [
    ("tls_params_corrected",    _milestone_tls_params_corrected),
    ("nginx_live_updated",      _milestone_nginx_live_updated),
    ("config_structure_intact", _milestone_config_structure_intact),
    ("pod_stable",              _milestone_pod_stable),
    ("https_operational",       _milestone_https_operational),
]


def grade(transcript: str = None) -> GradingResult:
    try:
        EXPECTED_TLS.update(_load_expected_tls())

        subscores: dict = {}
        milestone_feedback = []

        for name, fn in OBJECTIVES:
            score, message = fn()
            subscores[name] = score
            status = "PASS" if score >= 1.0 else "FAIL"
            milestone_feedback.append(f"{status} {name}: {message}")

        weights: dict = {k: 0.20 for k in subscores.keys()}
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
