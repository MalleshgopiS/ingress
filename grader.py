#!/usr/bin/env python3
"""Grader: ingress-tls-memory-leak

Verifies that the agent correctly identified and fixed the three broken nginx
TLS session parameters causing periodic OOMKill restarts.

Five milestones × 0.20 weight = 1.0 maximum score.  Every milestone is
BINARY (0.0 or 1.0) — all sub-checks within a milestone must pass.

Grading strategy:
  • tls_params_corrected    — ConfigMap TLS values are within safe bounds
  • nginx_live_updated      — Live nginx process reflects bounded TLS values
                              (requires rollout restart; subPath blocks auto-sync)
  • config_structure_intact — Original nginx directives preserved; ONLY passes
                              after TLS fix is confirmed in the ConfigMap
  • pod_stable              — No new OOMKill; deployment limits unchanged; ONLY
                              passes after TLS fix is confirmed in the ConfigMap
  • https_operational       — HTTPS endpoint responds with valid TLS; ONLY passes
                              after live nginx is confirmed to have bounded values

TLS value bounds (any values within these ranges pass):
  ssl_session_cache  : shared:SSL:Xm  where X ≤ 10  (eliminates unbounded builtin)
  ssl_session_timeout: ≤  3 600 s (1 h)              (prevents long-lived accumulation;
                                                       nginx best practice is 1 h or less)
  ssl_buffer_size    : ≤ 16 384 B (16 k)             (nginx documented default; ≤ 16 k passes)

Decoy trap analysis:
  tls-session-params  (32m / 8h / 32k)  — cache 32m >10 MB, timeout 8h >1h  → M1 FAILS
  nginx-ssl-defaults  (20m / 4h / 16k)  — cache 20m >10 MB, timeout 4h >1h  → M1 FAILS
  Agents who reason from nginx best practices (≤10MB, ≤1h, ≤16k)             → M1 PASSES

Score distribution:
  0.00 — no fix, or used decoy values (4h/8h timeout, 20m/32m cache all fail)
  0.60 — ConfigMap patched correctly but rollout restart not performed
  1.00 — all three TLS params fixed + rollout restart + structure preserved
"""

import datetime
import json
import re
import subprocess
import time
from apex_arena._types import GradingResult

NS     = "ingress-system"
DEPLOY = "ingress-controller"
SVC    = "ingress-controller-svc"

# Broken values planted by setup — used to detect un-fixed config
BROKEN_CACHE   = "builtin"
BROKEN_TIMEOUT = "86400"
BROKEN_BUFFER  = "64k"

# Maximum acceptable TLS values (range-based bounds, any value within range passes)
MAX_CACHE_MB  = 10.0   # shared SSL zone must be ≤ 10 MB  (decoy nginx-ssl-defaults=20m fails)
MAX_TIMEOUT_S = 3600   # session lifetime must be ≤ 1 h   (decoy nginx-ssl-defaults=4h fails)
MAX_BUFFER_B  = 16384  # per-connection buffer must be ≤ 16 k (nginx documented default)


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
    'builtin' will never match the shared:SSL: pattern so it always fails."""
    m = re.search(r'ssl_session_cache\s+(shared:SSL:\S+)\s*;', text or "", re.IGNORECASE)
    if not m:
        return False
    mb = _parse_cache_mb(m.group(1))
    return 0 < mb <= MAX_CACHE_MB


def _timeout_ok(text: str) -> bool:
    """ssl_session_timeout must be ≤ MAX_TIMEOUT_S (3 600 s = 1 h)."""
    m = re.search(r'ssl_session_timeout\s+(\S+)\s*;', text or "")
    if not m:
        return False
    try:
        s = _parse_nginx_time(m.group(1))
        return 0 < s <= MAX_TIMEOUT_S
    except (ValueError, TypeError):
        return False


def _buffer_ok(text: str) -> bool:
    """ssl_buffer_size must be ≤ MAX_BUFFER_B (16 384 B = 16 k)."""
    m = re.search(r'ssl_buffer_size\s+(\S+)\s*;', text or "")
    if not m:
        return False
    try:
        b = _parse_nginx_size(m.group(1))
        return 0 < b <= MAX_BUFFER_B
    except (ValueError, TypeError):
        return False


def _not_builtin(text: str) -> bool:
    """ssl_session_cache must not be 'builtin' (unbounded per-worker OpenSSL cache)."""
    return not re.search(r'ssl_session_cache\s+builtin\s*;', text or "")


def _all_tls_bounded(text: str) -> bool:
    """All three TLS params are within safe bounds.
    Used as a prerequisite gate for M3, M4, M5 — prevents those milestones
    from passing trivially in the broken environment."""
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


def _get_restart_count() -> str:
    _, out, _ = run(
        f"kubectl get pod -n {NS} -l app=ingress-controller "
        "-o jsonpath='{{.items[0].status.containerStatuses[0].restartCount}}'"
    )
    return out.strip() or "0"


# ── Objective 1: tls_params_corrected ─────────────────────────────────────────
# ConfigMap TLS values must all be within safe operational bounds.
# Any bounded shared-zone cache ≤ 10 MB, timeout ≤ 1 h, buffer ≤ 16 k passes.
# builtin cache, decoy tls-session-params (32m/8h/32k), and decoy
# nginx-ssl-defaults (20m/4h/16k) all fail at least one check.

def _obj_tls_params_corrected() -> tuple[float, str]:
    checks = {}
    for attempt in range(3):
        cfg = _get_configmap_conf()
        checks = {
            "cache_bounded":   _cache_ok(cfg),     # shared:SSL:Xm, X ≤ 10 MB
            "timeout_bounded": _timeout_ok(cfg),   # ≤ 3 600 s (1 h)
            "buffer_bounded":  _buffer_ok(cfg),    # ≤ 16 384 B (16 k)
            "not_builtin":     _not_builtin(cfg),  # builtin is unbounded — must be gone
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
# Live nginx process must reflect bounded TLS values.
# ConfigMap update alone is insufficient — nginx.conf uses a subPath mount so
# the kubelet does NOT auto-sync on ConfigMap change. A rollout restart is
# required so the new pod mounts the updated ConfigMap fresh at start-up.

def _obj_nginx_live_updated() -> tuple[float, str]:
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
            "live_cache_bounded":   _cache_ok(live),     # shared:SSL:Xm, X ≤ 10 MB
            "live_timeout_bounded": _timeout_ok(live),   # ≤ 3 600 s (1 h)
            "live_buffer_bounded":  _buffer_ok(live),    # ≤ 16 384 B (16 k)
            "live_not_builtin":     _not_builtin(live),  # builtin removed from live process
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
#
# PREREQUISITE: all 3 TLS params must be bounded in the ConfigMap first.
# Without the fix, keepalive/server_tokens/worker_connections trivially pass
# because the original broken config already contains them — this gate prevents
# a do-nothing agent from scoring this milestone.

def _obj_config_structure_intact() -> tuple[float, str]:
    cfg = _get_configmap_conf()

    if not _all_tls_bounded(cfg):
        return 0.0, "TLS params not all bounded — structure check skipped (fix ConfigMap first)"

    checks = {}
    for attempt in range(3):
        cfg = _get_configmap_conf()
        checks = {
            "keepalive_preserved":       bool(re.search(r'keepalive_timeout\s+\d+\s*;', cfg or "")),
            "server_tokens_preserved":   bool(re.search(r'server_tokens\s+off\s*;', cfg or "")),
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
# Pod is running normally, no new OOMKill events, and deployment resource
# limits have not been modified (constraint: do not modify pod memory limits).
#
# PREREQUISITE: all 3 TLS params must be bounded in the ConfigMap first.
# Without the fix the pod is trivially stable during the short observation
# window (OOMKills occur every 4-6 hours, not within seconds) — this gate
# prevents a do-nothing agent from scoring this milestone.

def _obj_pod_stable() -> tuple[float, str]:
    cfg = _get_configmap_conf()

    if not _all_tls_bounded(cfg):
        return 0.0, "TLS params not all bounded — stability check skipped (fix ConfigMap first)"

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

    _, mem_out, _ = run(
        f"kubectl get deployment {DEPLOY} -n {NS} "
        "-o jsonpath='{.spec.template.spec.containers[0].resources.limits.memory}'"
    )
    checks["limits_unchanged"] = mem_out.strip() == "300Mi"

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
# HTTPS endpoint responds with valid TLS and correct syntax.
#
# PREREQUISITE: live nginx must have all 3 TLS params bounded.
# Without the rollout restart the live nginx still runs with builtin cache —
# this gate ensures HTTPS operational credit requires the full fix (patch +
# restart), not just the ConfigMap update.

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
        return 0.0, "Live nginx TLS params not all bounded — rollout restart required before HTTPS check"

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

def _milestone_tls_params_corrected() -> tuple[float, str]:
    """Milestone 1: ConfigMap updated with bounded TLS values.
    Shared SSL zone ≤ 10 MB, timeout ≤ 1 h, buffer ≤ 16 k.
    builtin cache, tls-session-params (32m/8h/32k), nginx-ssl-defaults (20m/4h/16k) all fail."""
    return _obj_tls_params_corrected()


def _milestone_nginx_live_updated() -> tuple[float, str]:
    """Milestone 2: Live nginx process loaded bounded TLS values.
    Requires rollout restart — subPath mount blocks kubelet auto-sync."""
    return _obj_nginx_live_updated()


def _milestone_config_structure_intact() -> tuple[float, str]:
    """Milestone 3: Original nginx configuration directives preserved.
    Gated on TLS fix — prevents trivial pass in unmodified broken environment."""
    return _obj_config_structure_intact()


def _milestone_pod_stable() -> tuple[float, str]:
    """Milestone 4: Pod running with no new OOMKill events.
    Gated on TLS fix — prevents trivial pass in unmodified broken environment."""
    return _obj_pod_stable()


def _milestone_https_operational() -> tuple[float, str]:
    """Milestone 5: HTTPS endpoint responds with valid TLS.
    Gated on live nginx having bounded values — requires rollout restart."""
    return _obj_https_operational()


# ── Grade ──────────────────────────────────────────────────────────────────────
# Five milestones × 0.20 weight = 1.0 maximum score.
# Every milestone is BINARY (0.0 or 1.0).
#
# Expected score distribution:
#   0.00 — no fix, or all-wrong decoy values
#   0.60 — ConfigMap patched correctly but no rollout restart
#           (M1 + M3 + M4 pass; M2 + M5 fail — live nginx still broken)
#   1.00 — all params fixed + rollout restart + structure preserved

OBJECTIVES = [
    ("tls_params_corrected",    _milestone_tls_params_corrected),
    ("nginx_live_updated",      _milestone_nginx_live_updated),
    ("config_structure_intact", _milestone_config_structure_intact),
    ("pod_stable",              _milestone_pod_stable),
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
