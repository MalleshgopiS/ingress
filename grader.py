#!/usr/bin/env python3

import re
import subprocess
import time
from apex_arena._types import GradingResult

NS     = "ingress-system"
DEPLOY = "ingress-controller"
SVC    = "ingress-controller-svc"

# ── Grading thresholds ─────────────────────────────────────────────────────────
#
# Environment decoys:
#   tls-session-params  Secret    → cache=32m  timeout=8h  buffer=32k  (ALL wrong)
#   nginx-ssl-defaults  ConfigMap → cache=20m  timeout=30m buffer=16k  (MIXED: timeout ok, cache wrong)
#
# Natural agent variance per rollout:
#   Cache   : ~40% independently choose ≤10m  |  ~60% copy 20m/32m from decoys  → FAIL M1+M3
#   Timeout : ~50% read nginx-ssl-defaults→30m |  ~50% read tls-session-params→8h → FAIL M2+M4
#
# This creates 4 independent score patterns:
#   cache=10m, timeout=30m → M1=1 M2=1 M3=1 M4=1 M5=1 → 1.00  (~20% of rollouts)
#   cache=10m, timeout=8h  → M1=1 M2=0 M3=1 M4=0 M5=0 → 0.40  (~20%)
#   cache=20m, timeout=30m → M1=0 M2=1 M3=0 M4=1 M5=0 → 0.40  (~40%)
#   cache=20m, timeout=8h  → M1=0 M2=0 M3=0 M4=0 M5=0 → 0.00  (~20%)
#   → Expected mean ≈ 0.44,  CV ≈ 0.73

MAX_CACHE_MB  = 10.0   # shared SSL zone ≤ 10 MB  (decoys: 20m, 32m — both fail)
MAX_TIMEOUT_S = 3600   # session timeout ≤ 1 h    (decoy tls-session-params: 8h — fails; nginx-ssl-defaults: 30m — passes)
MAX_BUFFER_B  = 16384  # per-connection buffer ≤ 16 k (decoy tls-session-params: 32k — fails; nginx-ssl-defaults: 16k — passes)


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
    """Convert nginx time string to seconds: 1h→3600, 30m→1800, 3600→3600."""
    s = s.strip().lower()
    if s.endswith('h'):
        return int(s[:-1]) * 3600
    if s.endswith('m'):
        return int(s[:-1]) * 60
    if s.endswith('s'):
        return int(s[:-1])
    return int(s)


def _parse_nginx_size(s: str) -> int:
    """Convert nginx size string to bytes: 4k→4096, 16m→16777216."""
    s = s.strip().lower()
    if s.endswith('k'):
        return int(s[:-1]) * 1024
    if s.endswith('m'):
        return int(s[:-1]) * 1024 * 1024
    if s.endswith('g'):
        return int(s[:-1]) * 1024 * 1024 * 1024
    return int(s)


def _parse_cache_mb(val: str) -> float:
    """Extract MB from shared:SSL:5m → 5.0, shared:SSL:1g → 1024.0."""
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
    """ssl_session_cache must be shared:SSL:Xm where X ≤ MAX_CACHE_MB (10).
    'builtin' never matches the shared:SSL: pattern so always fails."""
    m = re.search(r'ssl_session_cache\s+(shared:SSL:\S+)\s*;', text or "", re.IGNORECASE)
    if not m:
        return False
    mb = _parse_cache_mb(m.group(1))
    return 0 < mb <= MAX_CACHE_MB


def _timeout_ok(text: str) -> bool:
    """ssl_session_timeout must be ≤ MAX_TIMEOUT_S (3600 s = 1 h).
    nginx-ssl-defaults decoy (30m=1800s) passes; tls-session-params (8h=28800s) fails."""
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
# ConfigMap check. ~40% pass: agents who choose ≤10m independently.
# ~60% fail: agents who copy nginx-ssl-defaults (20m) or tls-session-params (32m).

def _obj_cache_corrected() -> tuple[float, str]:
    """ConfigMap ssl_session_cache is shared:SSL:Xm where X ≤ 10 MB (not builtin).
    nginx-ssl-defaults: 20m — FAILS (too large).
    tls-session-params: 32m — FAILS (too large).
    Must independently choose ≤10m."""
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
# ConfigMap check. ~50% pass: agents who read nginx-ssl-defaults (30m ≤ 3600s).
# ~50% fail: agents who read tls-session-params (8h = 28800s > 3600s).

def _obj_timeout_corrected() -> tuple[float, str]:
    """ConfigMap ssl_session_timeout ≤ 3600 s (1 h).
    nginx-ssl-defaults: 30m = 1800s — PASSES (≤1h).
    tls-session-params: 8h = 28800s — FAILS (>1h).
    Split: ~50% pass (read configmap decoy), ~50% fail (read secret decoy)."""
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
# Live-process check. ~40% pass: same agents who pass M1 AND did rollout restart.
# Distinct from M1: checks the RUNNING nginx process via `nginx -T`, not the ConfigMap.
# Requires rollout restart to load new subPath-mounted ConfigMap value.

def _obj_live_cache_reloaded() -> tuple[float, str]:
    """Live nginx (nginx -T) shows bounded ssl_session_cache (≤10m, not builtin) and valid syntax.
    Correlated with M1 — passes only when agent chose ≤10m AND performed rollout restart.
    ~40% of rollouts pass."""
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
# Live-process check. ~50% pass: same agents who pass M2 AND did rollout restart.
# Distinct from M2: checks the RUNNING nginx process via `nginx -T`, not the ConfigMap.
# Requires rollout restart to load new subPath-mounted ConfigMap value.

def _obj_live_timeout_reloaded() -> tuple[float, str]:
    """Live nginx (nginx -T) shows bounded ssl_session_timeout (≤1h) and valid syntax.
    Correlated with M2 — passes only when agent chose ≤1h AND performed rollout restart.
    ~50% of rollouts pass (agents who copied nginx-ssl-defaults timeout=30m)."""
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
# Gated: passes only when live nginx has ALL TLS params bounded (cache+timeout+buffer).
# ~20% pass: agents who independently chose ≤10m cache AND got timeout ≤1h.

def _obj_https_operational() -> tuple[float, str]:
    """Live nginx has all bounded TLS values AND HTTPS endpoint is reachable.
    Gate: live nginx must show cache ≤10m AND timeout ≤1h AND buffer ≤16k.
    Requires correct values for BOTH cache and timeout — not achievable by copying one decoy."""
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
            f"Live nginx TLS not fully bounded — "
            f"cache {'≤10m ✓' if cache_ok else '>10m ✗'}, "
            f"timeout {'≤1h ✓' if timeout_ok else '>1h ✗'} — "
            f"must fix both independently and rollout restart"
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
    """Milestone 1 — ConfigMap: ssl_session_cache ≤10m, not builtin.
    ~40% pass. Decoys (20m, 32m) both fail. Agent must choose independently."""
    return _obj_cache_corrected()


def _milestone_timeout_corrected() -> tuple[float, str]:
    """Milestone 2 — ConfigMap: ssl_session_timeout ≤1h.
    ~50% pass. nginx-ssl-defaults (30m) passes; tls-session-params (8h) fails.
    Natural split: agents who read configmap vs those who read secret."""
    return _obj_timeout_corrected()


def _milestone_live_cache_reloaded() -> tuple[float, str]:
    """Milestone 3 — Live nginx: ssl_session_cache ≤10m after rollout restart.
    ~40% pass. Checks RUNNING PROCESS via nginx -T — distinct from M1 (ConfigMap).
    Requires rollout restart (subPath mount blocks kubelet auto-sync)."""
    return _obj_live_cache_reloaded()


def _milestone_live_timeout_reloaded() -> tuple[float, str]:
    """Milestone 4 — Live nginx: ssl_session_timeout ≤1h after rollout restart.
    ~50% pass. Checks RUNNING PROCESS via nginx -T — distinct from M2 (ConfigMap).
    M3 and M4 can independently pass/fail: e.g. cache wrong + timeout right → M3=0, M4=1."""
    return _obj_live_timeout_reloaded()


def _milestone_https_operational() -> tuple[float, str]:
    """Milestone 5 — HTTPS: live nginx has ALL bounded TLS values + endpoint responds.
    ~20% pass. Gate requires BOTH cache ≤10m AND timeout ≤1h in live nginx.
    No single decoy gives both correct values — forces independent reasoning."""
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
