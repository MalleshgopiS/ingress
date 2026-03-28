#!/usr/bin/env python3

import re
import subprocess
import time
from typing import Dict, Optional
from apex_arena._types import GradingResult

NS     = "ingress-system"
DEPLOY = "ingress-controller"
SVC    = "ingress-controller-svc"

BROKEN_BUFFER_BYTES = 65536  # ssl_buffer_size broken value (64k); any reduction is valid
MAX_TIMEOUT_S       = 3600   # ssl_session_timeout upper bound — 1h maximum
MAX_LIVE_TIMEOUT_S  = 3600   # ssl_session_timeout live upper bound — 1h


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


# ── TLS checks ────────────────────────────────────────────────────────────────

def _cache_is_shared(text: str) -> bool:
    """ssl_session_cache must be a shared zone (any size is acceptable)."""
    return bool(re.search(r'ssl_session_cache\s+shared:\w+:\S+\s*;', text or "", re.IGNORECASE))


def _not_builtin(text: str) -> bool:
    """ssl_session_cache must not use the unbounded 'builtin' type."""
    return not re.search(r'ssl_session_cache\s+builtin\s*;', text or "")


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


def _buffer_reduced(text: str) -> bool:
    """ssl_buffer_size must be present and less than the broken 64k value.
    Any value below 64k is acceptable — rejects only the unmodified broken config."""
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
    return 0 < bytes_val < BROKEN_BUFFER_BYTES


def _protocols_secure(text: str) -> bool:
    """ssl_protocols must not include deprecated protocols (SSLv3, standalone TLSv1)."""
    m = re.search(r'ssl_protocols\s+([^;]+);', text or "")
    if not m:
        return True  # Not set = nginx defaults (TLSv1.2 TLSv1.3) = OK
    protocols = m.group(1).lower()
    return 'sslv3' not in protocols and not re.search(r'\btlsv1\b(?!\.)', protocols)


def _watchdog_stopped() -> bool:
    """The ingress-config-watchdog CronJob must be stopped or rendered harmless.
    Accepts three valid remediation approaches:
      1. CronJob deleted (kubectl get returns non-zero)
      2. CronJob suspended (spec.suspend == true)
      3. Source ConfigMap (ingress-config-broken) patched so the watchdog no longer
         re-applies a broken config — agent fixed the drift source rather than the
         mechanism, which is equally valid."""
    res = run_cmd(
        f"kubectl get cronjob ingress-config-watchdog -n {NS} "
        f"-o jsonpath='{{.spec.suspend}}' 2>/dev/null"
    )
    if res.returncode != 0:
        # CronJob does not exist — agent deleted it ✓
        return True
    if res.stdout.strip().lower() == "true":
        # CronJob suspended ✓
        return True
    # CronJob still running — check if agent patched the source ConfigMap
    # (ingress-config-broken). If that no longer contains 'builtin', the watchdog
    # is harmless because it now restores the correct config.
    res_src = run_cmd(
        f"kubectl get configmap ingress-config-broken -n {NS} "
        f"-o jsonpath='{{.data.nginx\\.conf}}' 2>/dev/null"
    )
    if res_src.returncode == 0 and res_src.stdout.strip():
        if not re.search(r'ssl_session_cache\s+builtin', res_src.stdout, re.IGNORECASE):
            return True  # Source no longer broken — watchdog effectively neutralised ✓
    return False


def _alert_selector_correct() -> bool:
    """The ingress-alert-rules ConfigMap must have the correct container selector.
    The broken config uses container='nginx-controller'; correct is container='nginx'."""
    res = run_cmd(
        f"kubectl get configmap ingress-alert-rules -n {NS} "
        f"-o jsonpath='{{.data.alert\\.yaml}}' 2>/dev/null"
    )
    if res.returncode != 0 or not res.stdout.strip():
        return False
    content = res.stdout
    # Must NOT still have the wrong selector
    if re.search(r'container\s*=\s*["\']nginx-controller["\']', content):
        return False
    # Must have the correct selector
    return bool(re.search(r'container\s*=\s*["\']nginx["\']', content))


def _postmortem_exists() -> bool:
    """Agent must create /workdir/postmortem.md with ≥10 lines and relevant technical content."""
    res = run_cmd("test -f /workdir/postmortem.md && wc -l /workdir/postmortem.md")
    if res.returncode != 0:
        return False
    parts = res.stdout.strip().split()
    lines = int(parts[0]) if parts else 0
    if lines < 10:
        return False
    # Must have at least 5 lines with relevant technical terms — not gameable with keyword soup
    res2 = run_cmd(
        "grep -icE 'ssl|tls|cache|nginx|session|memory|buffer|timeout|protocol|alert' "
        "/workdir/postmortem.md"
    )
    try:
        relevant = int(res2.stdout.strip())
    except (ValueError, TypeError):
        return False
    return relevant >= 5


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
    (r'\bBROKEN_BUFFER_BYTES\b',                                   "grader internal constant BROKEN_BUFFER_BYTES"),
    (r'\bMAX_TIMEOUT_S\b',                                         "grader internal constant MAX_TIMEOUT_S"),
    (r'\bMAX_LIVE_TIMEOUT_S\b',                                    "grader internal constant MAX_LIVE_TIMEOUT_S"),
    (r'\b_cache_is_shared\b',                                      "grader internal function _cache_is_shared"),
    (r'\b_buffer_reduced\b',                                       "grader internal function _buffer_reduced"),
    (r'\b_timeout_ok\b',                                           "grader internal function _timeout_ok"),
    (r'\b_protocols_secure\b',                                     "grader internal function _protocols_secure"),
    (r'\b_alert_selector_correct\b',                               "grader internal function _alert_selector_correct"),
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

    Subscores (5 × 0.2 weight each):
      1. drift_stopped       — watchdog CronJob neutralized (deleted, suspended, or source patched)
      2. config_fixed        — ConfigMap: shared cache (any size), buffer < 64k, no TLSv1,
                               timeout ≤ 1h; gated on drift_stopped
      3. https_serving       — Service functionally verified: live nginx has new config after
                               rollout restart AND actual HTTPS request returns correct response
                               AND TLS handshake succeeds
      4. alert_configured    — ingress-alert-rules ConfigMap has correct container="nginx"
                               selector (was "nginx-controller")
      5. postmortem_complete — /workdir/postmortem.md exists with ≥10 lines of relevant content

    Returns weighted score (5 × 0.2). All subscores are binary (0.0 or 1.0).
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

        # ── Subscore 1: drift_stopped ──────────────────────────────────────────
        # Computed FIRST — config_fixed (S2) is gated on this to prevent race
        # conditions with the watchdog CronJob (runs every 3 min; grader retry
        # window is only 25s). If the watchdog is still running, ConfigMap state
        # is unreliable and config_fixed correctly fails.
        drift_stopped = False
        try:
            drift_stopped = _watchdog_stopped()
        except Exception:
            drift_stopped = False

        # ── Subscore 2: config_fixed ───────────────────────────────────────────
        # ConfigMap must have all four TLS issues resolved:
        #   - ssl_session_cache changed from builtin to shared: (any size)
        #   - ssl_buffer_size reduced below the broken 64k (any lower value valid)
        #   - ssl_protocols no longer includes deprecated TLSv1/SSLv3
        #   - ssl_session_timeout reduced to ≤ 1h
        # Gated on drift_stopped: watchdog reverts ConfigMap every 3 min,
        # shorter than the grader's 25s retry window. Without the gate, a
        # mid-grading watchdog run would non-deterministically flip this score.
        cfg_cache_shared    = False
        cfg_buffer_reduced  = False
        cfg_proto_secure    = False
        cfg_timeout_ok      = False
        config_fixed        = False

        try:
            if drift_stopped:
                for attempt in range(5):
                    cfg = get_configmap_conf()
                    cfg_cache_shared   = _cache_is_shared(cfg)
                    cfg_buffer_reduced = _buffer_reduced(cfg)
                    cfg_proto_secure   = _protocols_secure(cfg)
                    cfg_timeout_ok     = _timeout_ok(cfg)
                    config_fixed = (
                        cfg_cache_shared and cfg_buffer_reduced and
                        cfg_proto_secure and cfg_timeout_ok
                    )
                    if config_fixed:
                        break
                    if attempt < 4:
                        time.sleep(5)
        except Exception:
            config_fixed = False

        # ── Subscore 3: https_serving ─────────────────────────────────────────
        # The primary objective is to restore reliable HTTPS service. This subscore
        # verifies the service is actually working end-to-end — not just that the
        # ConfigMap has the right values. Requires:
        #   1. Live nginx process has the new config loaded (shared cache, not builtin)
        #      — proves the agent performed a rollout restart (subPath volumes do not
        #      auto-reload; ConfigMap changes only take effect after pod restart)
        #   2. nginx config syntax is valid in the running pod
        #   3. Actual HTTPS request to /healthz returns a valid response
        #   4. TLS handshake completes successfully (correct protocol negotiation)
        # NOT gated on drift_stopped: the live pod state is independently verifiable.
        # An agent who fixes the config and restarts but forgets the watchdog still
        # gets partial credit here, since HTTPS is functional at grading time.
        live_cache_shared  = False
        live_not_builtin   = False
        live_syntax_ok     = False
        https_responds     = False
        tls_handshake_ok   = False
        https_serving      = False

        try:
            pod = wait_for_pod()
            ip  = get_cluster_ip()
            if pod and ip:
                # Step 1 & 2: verify live nginx loaded the new config
                for attempt in range(3):
                    res_T = run_cmd(
                        f"kubectl exec -n {NS} {pod} -- nginx -T 2>/dev/null", timeout=15)
                    res_t = run_cmd(
                        f"kubectl exec -n {NS} {pod} -- nginx -t", timeout=10)
                    live = res_T.stdout or ""
                    live_cache_shared = _cache_is_shared(live)
                    live_not_builtin  = _not_builtin(live)
                    live_syntax_ok    = "syntax is ok" in (
                        res_t.stdout + res_t.stderr).lower()
                    if live_cache_shared and live_not_builtin and live_syntax_ok:
                        break
                    if attempt < 2:
                        time.sleep(10)

                # Step 3: actual HTTPS request — proves the service is reachable
                # and serving correct content, not just that the config looks right
                if live_cache_shared and live_not_builtin:
                    for _ in range(6):
                        res_curl = run_cmd(
                            f"curl -k -s --max-time 5 https://{ip}/healthz")
                        if "ok" in res_curl.stdout.lower():
                            https_responds = True
                            break
                        time.sleep(5)

                    # Step 4: TLS handshake — verifies protocol negotiation works
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

                https_serving = (
                    live_cache_shared and live_not_builtin and live_syntax_ok and
                    https_responds    and tls_handshake_ok
                )
        except Exception:
            https_serving = False

        # ── Subscore 4: alert_configured ──────────────────────────────────────
        # The ingress-alert-rules ConfigMap was deployed with an incorrect
        # container label selector (container="nginx-controller") that prevents
        # the restart alert from ever firing. The agent must discover the broken
        # selector and correct it to container="nginx" to match the actual pod.
        alert_configured = False
        try:
            alert_configured = _alert_selector_correct()
        except Exception:
            alert_configured = False

        # ── Subscore 5: postmortem_complete ───────────────────────────────────
        # Agent must document their investigation by creating /workdir/postmortem.md
        # with at least 10 lines explaining the root cause and remediation steps.
        postmortem_complete = False
        try:
            postmortem_complete = _postmortem_exists()
        except Exception:
            postmortem_complete = False

        # ── Subscores ──────────────────────────────────────────────────────────
        #
        # Five equal-weight subscores — each tests a distinct, independently
        # discoverable and fixable issue in the cluster:
        #
        # 1) drift_stopped:      watchdog CronJob neutralized
        # 2) config_fixed:       all four ConfigMap TLS issues resolved (gated)
        # 3) https_serving:      end-to-end HTTPS functional test (live config + curl + TLS)
        # 4) alert_configured:   Prometheus alert selector corrected
        # 5) postmortem_complete: documentation quality

        subscores: Dict[str, float] = {
            "drift_stopped":       1.0 if drift_stopped       else 0.0,
            "config_fixed":        1.0 if config_fixed         else 0.0,
            "https_serving":       1.0 if https_serving        else 0.0,
            "alert_configured":    1.0 if alert_configured     else 0.0,
            "postmortem_complete": 1.0 if postmortem_complete  else 0.0,
        }

        weight_val   = 1.0 / len(subscores)
        weights: Dict[str, float] = {k: weight_val for k in subscores}
        final_score  = sum(subscores.values()) / len(subscores)
        passed_count = sum(1 for v in subscores.values() if v >= 1.0)

        feedback_parts = [
            f"Score={final_score:.4f}",
            f"Subscores={subscores}",
            f"MilestonesPassed={passed_count}/{len(subscores)}",
            # drift_stopped
            f"DriftStopped: {'✓' if drift_stopped          else '✗'}",
            # config_fixed components
            f"CfgCacheShared: {'✓' if cfg_cache_shared     else '✗'}",
            f"CfgBufferReduced: {'✓' if cfg_buffer_reduced  else '✗'}",
            f"CfgProtoSecure: {'✓' if cfg_proto_secure      else '✗'}",
            f"CfgTimeoutOk: {'✓' if cfg_timeout_ok          else '✗'}",
            # https_serving components
            f"LiveCacheShared: {'✓' if live_cache_shared    else '✗'}",
            f"LiveNotBuiltin: {'✓' if live_not_builtin      else '✗'}",
            f"LiveSyntaxOk: {'✓' if live_syntax_ok          else '✗'}",
            f"HttpsResponds: {'✓' if https_responds         else '✗'}",
            f"TlsHandshake: {'✓' if tls_handshake_ok        else '✗'}",
            # alert_configured
            f"AlertConfigured: {'✓' if alert_configured     else '✗'}",
            # postmortem
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
