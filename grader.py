#!/usr/bin/env python3

import re
import subprocess
import time
from typing import Dict, Optional
from apex_arena._types import GradingResult

NS     = "ingress-system"
DEPLOY = "ingress-controller"
SVC    = "ingress-controller-svc"

MIN_CACHE_MB    = 3.0    # ssl_session_cache shared zone lower bound (MB)
MAX_CACHE_MB    = 7.0    # ssl_session_cache shared zone upper bound (MB) — must fit within 300Mi with headroom for worker RSS
MAX_TIMEOUT_S      = 1800   # ssl_session_timeout ConfigMap upper bound — 30min allows multiple eviction cycles within 6-hour OOM window
MAX_LIVE_TIMEOUT_S = 1200   # ssl_session_timeout live upper bound — 20min ensures sessions expire well before next OOM cycle
MAX_BUFFER_BYTES   = 8192   # ssl_buffer_size per-connection allocation upper bound — 8k for 300Mi instance class


# ── Shell helper ───────────────────────────────────────────────────────────────

def run_cmd(cmd: str, timeout: int = 30) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(cmd, returncode=-1, stdout="", stderr="command timed out")
    except Exception as exc:
        return subprocess.CompletedProcess(cmd, returncode=-1, stdout="", stderr=str(exc))


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


def _parse_cache_mb(val: str) -> float:
    m = re.search(r'shared:\w+:(\d+)(k|m|g)', val, re.IGNORECASE)
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
    """ssl_session_cache must be a shared zone (any zone name) within the accepted size range."""
    m = re.search(r'ssl_session_cache\s+(shared:\w+:\S+)\s*;', text or "", re.IGNORECASE)
    if not m:
        return False
    mb = _parse_cache_mb(m.group(1))
    return MIN_CACHE_MB <= mb <= MAX_CACHE_MB


def _timeout_ok(text: str, max_s: int = None) -> bool:
    """ssl_session_timeout must be present and within an acceptable range."""
    if max_s is None:
        max_s = MAX_TIMEOUT_S
    m = re.search(r'ssl_session_timeout\s+(\S+)\s*;', text or "")
    if not m:
        return False
    try:
        s = _parse_nginx_time(m.group(1))
        return 0 < s <= max_s
    except (ValueError, TypeError):
        return False


def _not_builtin(text: str) -> bool:
    """ssl_session_cache must not use the unbounded 'builtin' type."""
    return not re.search(r'ssl_session_cache\s+builtin\s*;', text or "")


def _buffer_size_ok(text: str) -> bool:
    """ssl_buffer_size must be present and within the per-connection allocation limit for this instance class."""
    m = re.search(r'ssl_buffer_size\s+(\d+)(k|m|)?\s*;', text or "", re.IGNORECASE)
    if not m:
        return False
    n, unit = int(m.group(1)), (m.group(2) or "").lower()
    if unit == 'k':
        bytes_val = n * 1024
    elif unit == 'm':
        bytes_val = n * 1024 * 1024
    else:
        bytes_val = n
    return 0 < bytes_val <= MAX_BUFFER_BYTES


def _protocols_secure(text: str) -> bool:
    """ssl_protocols must not include deprecated protocols (SSLv3, standalone TLSv1)."""
    m = re.search(r'ssl_protocols\s+([^;]+);', text or "")
    if not m:
        return True  # Not set = nginx defaults (TLSv1.2 TLSv1.3) = OK
    protocols = m.group(1).lower()
    return 'sslv3' not in protocols and not re.search(r'\btlsv1\b(?!\.)', protocols)


def _postmortem_exists() -> bool:
    """Agent must create /workdir/postmortem.md with at least 10 lines."""
    res = run_cmd("test -f /workdir/postmortem.md && wc -l /workdir/postmortem.md")
    if res.returncode != 0:
        return False
    parts = res.stdout.strip().split()
    lines = int(parts[0]) if parts else 0
    return lines >= 10


# ── Cluster helpers ────────────────────────────────────────────────────────────

def get_configmap_conf() -> str:
    """Fetch the nginx.conf content from the ingress-nginx-config ConfigMap."""
    res = run_cmd(
        f"kubectl get configmap ingress-nginx-config -n {NS} "
        "-o jsonpath='{.data.nginx\\.conf}'"
    )
    return res.stdout.strip() if res.returncode == 0 else ""


def get_running_pod() -> Optional[str]:
    """Return the name of a running ingress-controller pod, or None."""
    res = run_cmd(
        f"kubectl get pods -n {NS} -l app=ingress-controller "
        "--field-selector=status.phase=Running "
        "-o jsonpath='{.items[0].metadata.name}'"
    )
    pod = res.stdout.strip()
    return pod if pod else None


def get_cluster_ip() -> Optional[str]:
    """Return the ClusterIP of the ingress-controller service, or None."""
    res = run_cmd(
        f"kubectl get svc {SVC} -n {NS} -o jsonpath='{{.spec.clusterIP}}'"
    )
    ip = res.stdout.strip()
    return ip if ip else None


def wait_for_pod(attempts: int = 12, delay: int = 10) -> Optional[str]:
    """Wait up to attempts×delay seconds for a running nginx pod."""
    pod = get_running_pod()
    if pod:
        return pod
    for _ in range(attempts):
        time.sleep(delay)
        pod = get_running_pod()
        if pod:
            return pod
    return None


# ── Anti-cheat ─────────────────────────────────────────────────────────────────

_CHEAT_SIGNALS = [
    # Internal grader constant names
    (r'\bMIN_CACHE_MB\b',                                          "grader internal constant MIN_CACHE_MB"),
    (r'\bMAX_CACHE_MB\b',                                          "grader internal constant MAX_CACHE_MB"),
    (r'\bMAX_TIMEOUT_S\b',                                         "grader internal constant MAX_TIMEOUT_S"),
    (r'\bMAX_LIVE_TIMEOUT_S\b',                                    "grader internal constant MAX_LIVE_TIMEOUT_S"),
    (r'\bMAX_BUFFER_BYTES\b',                                      "grader internal constant MAX_BUFFER_BYTES"),
    (r'\b_cache_ok\b',                                             "grader internal function _cache_ok"),
    (r'\b_timeout_ok\b',                                           "grader internal function _timeout_ok"),
    (r'\b_buffer_size_ok\b',                                       "grader internal function _buffer_size_ok"),
    (r'\b_protocols_secure\b',                                     "grader internal function _protocols_secure"),
    (r'\b_postmortem_exists\b',                                    "grader internal function _postmortem_exists"),
    # Direct file reads
    (r'(?:cat|less|head|tail|more|bat|vim|nano|grep)\s+[^\n]*grader\.py',   "direct read of grader.py"),
    (r'(?:cat|less|head|tail|more|bat|vim|nano|grep)\s+[^\n]*solution\.sh', "direct read of solution.sh"),
    (r'ls\s+/grader\b',                                            "directory listing of /grader"),
    # Python open() bypass
    (r'python[23]?\s[^\n]*open\s*\([^\n]*grader',                  "python file read of grader.py"),
    (r'python[23]?\s[^\n]*open\s*\([^\n]*solution',                "python file read of solution.sh"),
    # cp / base64 bypass
    (r'\bcp\b\s+[^\n]*grader\.py',                                 "cp of grader.py"),
    (r'\bcp\b\s+[^\n]*solution\.sh',                               "cp of solution.sh"),
    (r'\bbase64\b\s+[^\n]*grader',                                 "base64 read of grader.py"),
    (r'\bbase64\b\s+[^\n]*solution',                               "base64 read of solution.sh"),
    # Solution variable names (only exist inside solution.sh)
    (r'\bSSL_CACHE\s*=',                                           "solution.sh variable SSL_CACHE"),
    (r'\bSSL_TIMEOUT\s*=',                                         "solution.sh variable SSL_TIMEOUT"),
    (r'\bSSL_BUFFER\s*=',                                          "solution.sh variable SSL_BUFFER"),
]


def _detect_cheating(transcript: str) -> tuple:
    """Return (cheated, reason) if the agent accessed restricted files."""
    if not transcript:
        return False, ""
    for pattern, label in _CHEAT_SIGNALS:
        if re.search(pattern, transcript, re.IGNORECASE):
            return True, label
    return False, ""


# ── Grade ──────────────────────────────────────────────────────────────────────

def grade(transcript: str = None) -> GradingResult:
    """
    Outcome-based grader for Ingress Controller TLS session memory leak (issue #488).

    Milestones verified:
      1. cache_corrected       — TLS session cache is a bounded shared zone in the ConfigMap
      2. timeout_corrected     — TLS session lifetime is reduced to an acceptable value in the ConfigMap
      3. protocols_secure      — ssl_protocols does not include deprecated protocols (ConfigMap + live)
      4. live_cache_reloaded   — Corrected cache type is active in the running nginx worker
      5. live_timeout_reloaded — Corrected session timeout is active in the running nginx worker
      6. https_operational     — Ingress serves HTTPS traffic reliably after all fixes
      7. postmortem_complete   — Agent created /workdir/postmortem.md (≥10 lines)

    Returns weighted score (7 × 1/7). All subscores are binary (0.0 or 1.0).
    """
    try:
        # ── Integrity check ────────────────────────────────────────────────────
        cheated, reason = _detect_cheating(transcript)
        if cheated:
            return GradingResult(
                score=0.0,
                feedback=(
                    f"Integrity violation — agent accessed restricted file content "
                    f"({reason}); all scores invalidated"
                ),
            )

        # ── Milestone 1: cache_corrected ───────────────────────────────────────
        # The TLS session cache must be replaced with a properly bounded shared zone
        # in the ConfigMap AND the per-connection buffer allocation must be reduced
        # to fit within the 300Mi instance memory budget.
        # Also verifies existing config structure was not destroyed.
        cache_is_shared  = False
        cache_bounded    = False
        builtin_removed  = False
        keepalive_intact = False
        buffer_ok        = False
        cache_corrected  = False

        try:
            for attempt in range(5):
                cfg = get_configmap_conf()
                cache_is_shared  = bool(re.search(
                    r'ssl_session_cache\s+shared:\w+:', cfg or "", re.IGNORECASE))
                cache_bounded    = _cache_ok(cfg)
                builtin_removed  = _not_builtin(cfg)
                keepalive_intact = bool(re.search(r'keepalive_timeout\s+\d+', cfg or ""))
                buffer_ok        = _buffer_size_ok(cfg)
                cache_corrected  = (
                    cache_is_shared and cache_bounded and
                    builtin_removed and keepalive_intact and buffer_ok
                )
                if cache_corrected:
                    break
                if attempt < 4:
                    time.sleep(5)
        except Exception:
            cache_corrected = False

        # ── Milestone 2: timeout_corrected ────────────────────────────────────
        # TLS session lifetime must be reduced to a value that prevents unbounded
        # session accumulation under sustained HTTPS traffic.
        timeout_present   = False
        timeout_bounded   = False
        timeout_corrected = False

        try:
            for attempt in range(5):
                cfg = get_configmap_conf()
                timeout_present   = bool(re.search(
                    r'ssl_session_timeout\s+\S+\s*;', cfg or ""))
                timeout_bounded   = _timeout_ok(cfg)
                timeout_corrected = timeout_present and timeout_bounded
                if timeout_corrected:
                    break
                if attempt < 4:
                    time.sleep(5)
        except Exception:
            timeout_corrected = False

        # ── Milestone 3: protocols_secure ─────────────────────────────────────
        # The nginx config includes deprecated ssl_protocols (TLSv1) which exposes
        # clients to downgrade attacks. The agent must remove deprecated protocols
        # from both the ConfigMap and the running nginx worker (via rollout restart).
        proto_cm_secure   = False
        proto_live_secure = False
        protocols_secure  = False

        try:
            for attempt in range(5):
                cfg = get_configmap_conf()
                proto_cm_secure = _protocols_secure(cfg)
                if proto_cm_secure:
                    break
                if attempt < 4:
                    time.sleep(5)
            pod = wait_for_pod()
            if pod:
                for attempt in range(3):
                    res_T = run_cmd(
                        f"kubectl exec -n {NS} {pod} -- nginx -T 2>/dev/null", timeout=15)
                    live_p = res_T.stdout or ""
                    proto_live_secure = _protocols_secure(live_p)
                    if proto_live_secure:
                        break
                    if attempt < 2:
                        time.sleep(10)
            protocols_secure = proto_cm_secure and proto_live_secure
        except Exception:
            protocols_secure = False

        # ── Milestone 4: live_cache_reloaded ──────────────────────────────────
        # ConfigMap changes do not auto-propagate to a running nginx process when
        # the volume uses subPath. A rollout restart is required.
        # Verifies via nginx -T that the running worker has a corrected shared
        # cache zone loaded and passes syntax validation.
        # Note: does NOT check buffer size — that is M1's concern only.
        live_cache_shared   = False
        live_cache_bounded  = False
        live_not_builtin    = False
        live_syntax_ok      = False
        live_buffer_ok      = False
        live_cache_reloaded = False

        try:
            pod = wait_for_pod()
            if pod:
                for attempt in range(3):
                    res_T = run_cmd(
                        f"kubectl exec -n {NS} {pod} -- nginx -T 2>/dev/null", timeout=15)
                    res_t = run_cmd(
                        f"kubectl exec -n {NS} {pod} -- nginx -t", timeout=10)
                    live = res_T.stdout or ""
                    live_cache_shared  = bool(re.search(
                        r'ssl_session_cache\s+shared:\w+:', live, re.IGNORECASE))
                    live_cache_bounded = _cache_ok(live)
                    live_not_builtin   = _not_builtin(live)
                    live_syntax_ok     = "syntax is ok" in (
                        res_t.stdout + res_t.stderr).lower()
                    live_buffer_ok     = _buffer_size_ok(live)
                    live_cache_reloaded = (
                        live_cache_shared and live_cache_bounded and
                        live_not_builtin  and live_syntax_ok
                    )
                    if live_cache_reloaded:
                        break
                    if attempt < 2:
                        time.sleep(10)
        except Exception:
            live_cache_reloaded = False

        # ── Milestone 5: live_timeout_reloaded ────────────────────────────────
        # Verifies via nginx -T that the running worker has the corrected session
        # timeout value loaded after rollout restart.
        # Uses MAX_LIVE_TIMEOUT_S (1800s / 30m) — stricter than M2's ConfigMap
        # check — ensuring sessions expire well within the 6-hour OOM window.
        live_timeout_present  = False
        live_timeout_bounded  = False
        live_timeout_reloaded = False

        try:
            pod = wait_for_pod()
            if pod:
                for attempt in range(3):
                    res_T = run_cmd(
                        f"kubectl exec -n {NS} {pod} -- nginx -T 2>/dev/null", timeout=15)
                    live = res_T.stdout or ""
                    live_timeout_present  = bool(re.search(
                        r'ssl_session_timeout\s+\S+\s*;', live))
                    live_timeout_bounded  = _timeout_ok(live, max_s=MAX_LIVE_TIMEOUT_S)
                    live_timeout_reloaded = live_timeout_present and live_timeout_bounded
                    if live_timeout_reloaded:
                        break
                    if attempt < 2:
                        time.sleep(10)
        except Exception:
            live_timeout_reloaded = False

        # ── Milestone 6: https_operational ────────────────────────────────────
        # Gate: the live running nginx must no longer use the builtin cache type.
        # Checked against nginx -T output (the actually loaded config), NOT the
        # ConfigMap. This makes M5 independent of cache size / timeout thresholds:
        # any shared zone proves the agent performed a rollout restart so the new
        # config was loaded. Agents who only edit the ConfigMap without restarting
        # still fail (live nginx still shows builtin). Agents who restart with any
        # shared zone pass the gate regardless of which size or timeout they chose.
        # Verifies the ingress controller responds to HTTPS requests with a valid
        # TLS handshake and passes nginx config syntax validation.
        https_gate_ok     = False
        https_responds    = False
        tls_handshake_ok  = False
        nginx_syntax_ok   = False
        https_operational = False

        try:
            pod = wait_for_pod()
            ip  = get_cluster_ip()
            if pod and ip:
                # Gate: live nginx must not use builtin (proves rollout restart happened)
                res_live      = run_cmd(
                    f"kubectl exec -n {NS} {pod} -- nginx -T 2>/dev/null", timeout=15)
                https_gate_ok = _not_builtin(res_live.stdout or "")
                if https_gate_ok:
                    # curl /healthz
                    for _ in range(6):
                        res = run_cmd(f"curl -k -s --max-time 5 https://{ip}/healthz")
                        if "ok" in res.stdout.lower():
                            https_responds = True
                            break
                        time.sleep(5)
                    # TLS handshake
                    res_tls = run_cmd(
                        f"echo Q | openssl s_client -connect {ip}:443 2>&1 | head -10",
                        timeout=15,
                    )
                    combined = (res_tls.stdout + res_tls.stderr).lower()
                    tls_handshake_ok = (
                        "ssl-session" in combined or
                        "cipher"      in combined or
                        "connected"   in combined
                    )
                    # nginx config syntax
                    res_t = run_cmd(
                        f"kubectl exec -n {NS} {pod} -- nginx -t", timeout=10)
                    nginx_syntax_ok = "syntax is ok" in (
                        res_t.stdout + res_t.stderr).lower()
                    https_operational = (
                        https_responds and tls_handshake_ok and nginx_syntax_ok
                    )
        except Exception:
            https_operational = False

        # ── Milestone 7: postmortem_complete ──────────────────────────────────
        # Agent must document their investigation by creating /workdir/postmortem.md
        # with at least 10 lines explaining the root cause and remediation steps.
        postmortem_complete = False

        try:
            postmortem_complete = _postmortem_exists()
        except Exception:
            postmortem_complete = False

        # ── Subscores ──────────────────────────────────────────────────────────
        #
        # 1) cache_corrected:       ConfigMap cache is bounded shared zone + buffer OK
        # 2) timeout_corrected:     ConfigMap timeout is within acceptable range
        # 3) protocols_secure:      ConfigMap + live nginx have no deprecated protocols
        # 4) live_cache_reloaded:   Shared cache zone active in running worker (restart proof)
        # 5) live_timeout_reloaded: Corrected timeout active in running worker
        # 6) https_operational:     Ingress serves HTTPS reliably after all fixes
        # 7) postmortem_complete:   Agent created /workdir/postmortem.md (≥10 lines)

        subscores: Dict[str, float] = {
            "cache_corrected":       1.0 if cache_corrected       else 0.0,
            "timeout_corrected":     1.0 if timeout_corrected     else 0.0,
            "protocols_secure":      1.0 if protocols_secure      else 0.0,
            "live_cache_reloaded":   1.0 if live_cache_reloaded   else 0.0,
            "live_timeout_reloaded": 1.0 if live_timeout_reloaded else 0.0,
            "https_operational":     1.0 if https_operational     else 0.0,
            "postmortem_complete":   1.0 if postmortem_complete   else 0.0,
        }

        weight_val   = 1.0 / len(subscores)
        weights: Dict[str, float] = {k: weight_val for k in subscores}
        final_score  = sum(subscores.values()) / len(subscores)
        passed_count = sum(1 for v in subscores.values() if v >= 1.0)

        feedback_parts = [
            f"Score={final_score:.4f}",
            f"Subscores={subscores}",
            f"MilestonesPassed={passed_count}/{len(subscores)}",
            # Milestone 1 detail
            f"CacheIsSharedZone: {'✓' if cache_is_shared   else '✗'}",
            f"CacheSizeBounded: {'✓' if cache_bounded       else '✗'}",
            f"BuiltinRemoved: {'✓' if builtin_removed       else '✗'}",
            f"KeepaliveIntact: {'✓' if keepalive_intact     else '✗'}",
            f"BufferSizeOk: {'✓' if buffer_ok               else '✗'}",
            # Milestone 2 detail
            f"TimeoutPresent: {'✓' if timeout_present       else '✗'}",
            f"TimeoutBounded: {'✓' if timeout_bounded       else '✗'}",
            # Milestone 3 detail
            f"ProtoCmSecure: {'✓' if proto_cm_secure        else '✗'}",
            f"ProtoLiveSecure: {'✓' if proto_live_secure    else '✗'}",
            # Milestone 4 detail
            f"LiveCacheShared: {'✓' if live_cache_shared    else '✗'}",
            f"LiveCacheBounded: {'✓' if live_cache_bounded  else '✗'}",
            f"LiveNotBuiltin: {'✓' if live_not_builtin      else '✗'}",
            f"LiveSyntaxOk: {'✓' if live_syntax_ok          else '✗'}",
            f"LiveBufferOk: {'✓' if live_buffer_ok          else '✗'}",
            # Milestone 5 detail
            f"LiveTimeoutPresent: {'✓' if live_timeout_present else '✗'}",
            f"LiveTimeoutBounded: {'✓' if live_timeout_bounded else '✗'}",
            # Milestone 6 detail
            f"HttpsGate: {'✓' if https_gate_ok              else '✗'}",
            f"HttpsResponds: {'✓' if https_responds         else '✗'}",
            f"TlsHandshake: {'✓' if tls_handshake_ok        else '✗'}",
            f"NginxSyntax: {'✓' if nginx_syntax_ok          else '✗'}",
            # Milestone 7 detail
            f"PostmortemComplete: {'✓' if postmortem_complete else '✗'}",
        ]

        return GradingResult(
            score=final_score,
            subscores=subscores,
            weights=weights,
            feedback=" | ".join(feedback_parts),
        )

    except Exception as e:
        return GradingResult(score=0.0, feedback=f"Grader error: {str(e)}")
