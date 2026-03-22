#!/usr/bin/env python3

import re
import subprocess
import time
from apex_arena._types import GradingResult

NS     = "ingress-system"
DEPLOY = "ingress-controller"
SVC    = "ingress-controller-svc"


MAX_CACHE_MB  = 10.0  
MAX_TIMEOUT_S = 3600   
MAX_BUFFER_B  = 16384  


# ── Shell helper ───────────────────────────────────────────────────────────────

def run(cmd: str, timeout: int = 30):
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except subprocess.TimeoutExpired:
        return -1, "", "command timed out"
    except Exception as exc:
        return -1, "", str(exc)


# ── nginx value parsers ────────────────────────────────────────────────────────

def _parse_nginx_time(s: str) -> int:
    
    s = s.strip().lower()
    if s.endswith('h'):
        return int(s[:-1]) * 3600
    if s.endswith('m'):
        return int(s[:-1]) * 60
    if s.endswith('s'):
        return int(s[:-1])
    return int(s)


def _parse_nginx_size(s: str) -> int:

    s = s.strip().lower()
    if s.endswith('k'):
        return int(s[:-1]) * 1024
    if s.endswith('m'):
        return int(s[:-1]) * 1024 * 1024
    if s.endswith('g'):
        return int(s[:-1]) * 1024 * 1024 * 1024
    return int(s)


def _parse_cache_mb(val: str) -> float:
    
    m = re.search(r'shared:SSL:(\d+)(k|m|g)', val, re.IGNORECASE)
    if not m:
        return -1.0
    n, unit = int(m.group(1)), m.group(2).lower()
    if unit == 'k':
        return n / 1024.0
    if unit == 'g':
        return n * 1024.0
    return float(n)


# ── Range-bounded TLS checks ───────────────────────────────────────────────────

def _cache_ok(text: str) -> bool:
    
    m = re.search(r'ssl_session_cache\s+(shared:SSL:\S+)\s*;', text or "", re.IGNORECASE)
    if not m:
        return False
    mb = _parse_cache_mb(m.group(1))
    return 0 < mb <= MAX_CACHE_MB


def _timeout_ok(text: str) -> bool:
    
    m = re.search(r'ssl_session_timeout\s+(\S+)\s*;', text or "")
    if not m:
        return False
    try:
        s = _parse_nginx_time(m.group(1))
        return 0 < s <= MAX_TIMEOUT_S
    except (ValueError, TypeError):
        return False


def _buffer_ok(text: str) -> bool:
    """ssl_buffer_size must be ≤ MAX_BUFFER_B (16384 B = 16 k)."""
    m = re.search(r'ssl_buffer_size\s+(\S+)\s*;', text or "")
    if not m:
        return False
    try:
        b = _parse_nginx_size(m.group(1))
        return 0 < b <= MAX_BUFFER_B
    except (ValueError, TypeError):
        return False


def _not_builtin(text: str) -> bool:
    """ssl_session_cache must not be 'builtin'."""
    return not re.search(r'ssl_session_cache\s+builtin\s*;', text or "")


def _all_tls_bounded(text: str) -> bool:
    """All three TLS params within safe bounds — gate for M5."""
    return _cache_ok(text) and _timeout_ok(text) and _buffer_ok(text)


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


# ── Objective 1: cache_corrected ──────────────────────────────────────────────


def _obj_cache_corrected() -> tuple[float, str]:
    
    checks = {}
    for attempt in range(3):
        cfg = _get_configmap_conf()
        checks = {
            "cache_is_shared_zone": bool(re.search(
                r'ssl_session_cache\s+shared:SSL:', cfg or "", re.IGNORECASE)),
            "cache_size_bounded":   _cache_ok(cfg),
            "builtin_removed":      _not_builtin(cfg),
        }
        if all(checks.values()):
            break
        if attempt < 2:
            time.sleep(5)
    n      = sum(checks.values())
    detail = ", ".join(f"{'✓' if ok else '✗'} {k}" for k, ok in checks.items())
    score  = 1.0 if all(checks.values()) else 0.0
    return score, f"{n}/{len(checks)} cache checks — {detail}"


# ── Objective 2: timeout_corrected ────────────────────────────────────────────


def _obj_timeout_corrected() -> tuple[float, str]:
    
    checks = {}
    for attempt in range(3):
        cfg = _get_configmap_conf()
        checks = {
            "timeout_present": bool(re.search(
                r'ssl_session_timeout\s+\S+\s*;', cfg or "")),
            "timeout_bounded": _timeout_ok(cfg),
        }
        if all(checks.values()):
            break
        if attempt < 2:
            time.sleep(5)
    n      = sum(checks.values())
    detail = ", ".join(f"{'✓' if ok else '✗'} {k}" for k, ok in checks.items())
    score  = 1.0 if all(checks.values()) else 0.0
    return score, f"{n}/{len(checks)} timeout checks — {detail}"


# ── Objective 3: live_cache_reloaded ──────────────────────────────────────────


def _obj_live_cache_reloaded() -> tuple[float, str]:
    
    pod = _get_running_pod()
    if not pod:
        for _ in range(6):
            time.sleep(10)
            pod = _get_running_pod()
            if pod:
                break
    if not pod:
        return 0.0, "No running nginx pod found — cannot verify live cache"

    checks = {}
    for attempt in range(3):
        _, live, _ = run(f"kubectl exec -n {NS} {pod} -- nginx -T 2>/dev/null", timeout=15)
        _, out, err = run(f"kubectl exec -n {NS} {pod} -- nginx -t", timeout=10)
        checks = {
            "live_cache_shared":  bool(re.search(
                r'ssl_session_cache\s+shared:SSL:', live or "", re.IGNORECASE)),
            "live_cache_bounded": _cache_ok(live),
            "live_not_builtin":   _not_builtin(live),
            "nginx_syntax_valid": "syntax is ok" in (out + err).lower(),
        }
        if all(checks.values()):
            break
        if attempt < 2:
            time.sleep(10)
    n      = sum(checks.values())
    detail = ", ".join(f"{'✓' if ok else '✗'} {k}" for k, ok in checks.items())
    score  = 1.0 if all(checks.values()) else 0.0
    return score, f"{n}/{len(checks)} live cache checks — {detail}"


# ── Objective 4: live_timeout_reloaded ────────────────────────────────────────


def _obj_live_timeout_reloaded() -> tuple[float, str]:
    
    pod = _get_running_pod()
    if not pod:
        for _ in range(6):
            time.sleep(10)
            pod = _get_running_pod()
            if pod:
                break
    if not pod:
        return 0.0, "No running nginx pod found — cannot verify live timeout"

    checks = {}
    for attempt in range(3):
        _, live, _ = run(f"kubectl exec -n {NS} {pod} -- nginx -T 2>/dev/null", timeout=15)
        _, out, err = run(f"kubectl exec -n {NS} {pod} -- nginx -t", timeout=10)
        checks = {
            "live_timeout_present": bool(re.search(
                r'ssl_session_timeout\s+\S+\s*;', live or "")),
            "live_timeout_bounded": _timeout_ok(live),
            "nginx_syntax_valid":   "syntax is ok" in (out + err).lower(),
        }
        if all(checks.values()):
            break
        if attempt < 2:
            time.sleep(10)
    n      = sum(checks.values())
    detail = ", ".join(f"{'✓' if ok else '✗'} {k}" for k, ok in checks.items())
    score  = 1.0 if all(checks.values()) else 0.0
    return score, f"{n}/{len(checks)} live timeout checks — {detail}"


# ── Objective 5: https_operational ────────────────────────────────────────────


def _obj_https_operational() -> tuple[float, str]:
    
    pod = _get_running_pod()
    if not pod:
        for _ in range(6):
            time.sleep(10)
            pod = _get_running_pod()
            if pod:
                break
    if not pod:
        return 0.0, "No running nginx pod found — cannot verify HTTPS"

    _, live, _ = run(f"kubectl exec -n {NS} {pod} -- nginx -T 2>/dev/null", timeout=15)
    if not _all_tls_bounded(live):
        cache_ok   = _cache_ok(live)
        timeout_ok = _timeout_ok(live)
        return 0.0, (
            f"Live nginx TLS parameters not within safe bounds — "
            f"cache {'within budget ✓' if cache_ok else 'exceeds budget ✗'}, "
            f"timeout {'within budget ✓' if timeout_ok else 'exceeds budget ✗'} — "
            f"review platform limits and ensure deployment is restarted"
        )

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

    _, out, err = run(f"kubectl exec -n {NS} {pod} -- nginx -t", timeout=10)
    checks["nginx_syntax_valid"] = "syntax is ok" in (out + err).lower()

    n      = sum(checks.values())
    detail = ", ".join(f"{'✓' if ok else '✗'} {k}" for k, ok in checks.items())
    score  = 1.0 if all(checks.values()) else 0.0
    return score, f"{n}/{len(checks)} HTTPS operational checks — {detail}"


# ── Grouped milestone functions ────────────────────────────────────────────────

def _milestone_cache_corrected() -> tuple[float, str]:

    return _obj_cache_corrected()


def _milestone_timeout_corrected() -> tuple[float, str]:

    return _obj_timeout_corrected()


def _milestone_live_cache_reloaded() -> tuple[float, str]:
    
    return _obj_live_cache_reloaded()


def _milestone_live_timeout_reloaded() -> tuple[float, str]:
    
    return _obj_live_timeout_reloaded()


def _milestone_https_operational() -> tuple[float, str]:

    return _obj_https_operational()


# ── Grade ──────────────────────────────────────────────────────────────────────

OBJECTIVES = [
    ("cache_corrected",         _milestone_cache_corrected),
    ("timeout_corrected",       _milestone_timeout_corrected),
    ("live_cache_reloaded",     _milestone_live_cache_reloaded),
    ("live_timeout_reloaded",   _milestone_live_timeout_reloaded),
    ("https_operational",       _milestone_https_operational),
]


def grade(transcript: str = None) -> GradingResult:
    try:
        subscores: dict = {}
        milestone_feedback = []

        for name, fn in OBJECTIVES:
            score, message = fn()
            subscores[name] = score
            status = "PASS" if score >= 1.0 else "FAIL"
            milestone_feedback.append(f"{status} {name}: {message}")

        weights: dict = {k: 0.20 for k in subscores.keys()}
        final_score   = sum(v * weights[k] for k, v in subscores.items())
        passed_count  = sum(1 for v in subscores.values() if v >= 1.0)

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
