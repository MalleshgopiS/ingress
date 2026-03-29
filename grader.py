#!/usr/bin/env python3

import re
import subprocess
import time
from typing import Dict, Optional
from apex_arena._types import GradingResult

NS     = "ingress-system"
DEPLOY = "ingress-controller"
SVC    = "ingress-controller-svc"

# Watchdog names — all four must be neutralized to stop config drift
WATCHDOG_PRIMARY      = "ingress-config-watchdog"   # CronJob, ingress-system
WATCHDOG_SECONDARY    = "ops-config-controller"      # CronJob, ingress-system
WATCHDOG_TERTIARY     = "audit-log-exporter"         # CronJob, default namespace
WATCHDOG_TERTIARY_NS  = "default"
WATCHDOG_QUATERNARY   = "telemetry-agent"            # Deployment, default namespace (disguised)
WATCHDOG_QUATERNARY_NS = "default"

# Source ConfigMaps used by the watchdog controllers
# Primary + secondary + quaternary share ingress-config-broken.
# Tertiary uses ingress-config-snapshot — a SEPARATE source.
# Patching ingress-config-broken alone does NOT stop the tertiary.
SNAPSHOT_CM = "ingress-config-snapshot"

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


def _session_tickets_disabled(text: str) -> bool:
    """ssl_session_tickets must be explicitly disabled.
    The broken config sets it ON — combined with a shared session cache this causes
    sessions to be stored twice (once in the cache zone, once in the ticket),
    wasting memory and undermining the cache size bound set by the agent.
    Setting ssl_session_tickets off forces all session resumption through the
    bounded cache zone, which is the only resumption mechanism the agent controls."""
    m = re.search(r'ssl_session_tickets\s+(\w+)\s*;', text or "", re.IGNORECASE)
    if not m:
        return False  # Broken config has it explicitly ON — must explicitly set OFF
    return m.group(1).lower() == 'off'


def _ciphers_secure(text: str) -> bool:
    """ssl_ciphers must not include export-grade (EXP) or low-strength (LOW) ciphers
    as active (non-negated) cipher tokens.

    The broken config includes HIGH:MEDIUM:LOW:EXP:!NULL — LOW and EXP are positive
    (active) tokens. Correct remediation:
      a) Remove the directive entirely — nginx secure defaults apply.
      b) Use a cipher string that does not include EXP or LOW as positive tokens.
      c) Explicitly exclude them (e.g. !EXP:!LOW) — also acceptable.

    Token-based evaluation: each colon-separated token is checked individually;
    tokens prefixed with '!' are exclusions and are not penalised."""
    m = re.search(r'ssl_ciphers\s+["\']?([^;"\';\n]+)["\']?\s*;', text or "")
    if not m:
        return True  # Removed → nginx secure defaults apply
    # Split cipher string into individual tokens and evaluate each one.
    # Only reject if EXP or LOW appear as positive (non-negated) tokens.
    tokens = [t.strip() for t in m.group(1).upper().split(':')]
    return not any(
        (not t.startswith('!')) and re.fullmatch(r'EXP|LOW', t)
        for t in tokens
    )


def _listen_all_interfaces(text: str) -> bool:
    """Port 443 must not be restricted to loopback (127.0.0.1 / ::1 / localhost).
    The broken config sets 'listen 127.0.0.1:443 ssl' — external traffic routed
    via the ClusterIP Service never reaches nginx on the loopback-only socket."""
    if re.search(r'listen\s+(?:127\.0\.0\.1|::1|localhost):443', text or "", re.IGNORECASE):
        return False
    return bool(re.search(r'listen\s+\S*443', text or ""))


def _source_configmap_patched() -> bool:
    """Return True if ingress-config-broken no longer contains the broken session cache.
    Covers primary + secondary watchdogs (both read from ingress-config-broken)."""
    res = run_cmd(
        f"kubectl get configmap ingress-config-broken -n {NS} "
        f"-o jsonpath='{{.data.nginx\\.conf}}' 2>/dev/null"
    )
    if res.returncode != 0 or not res.stdout.strip():
        return False
    return not re.search(r'ssl_session_cache\s+builtin', res.stdout, re.IGNORECASE)


def _snapshot_configmap_patched() -> bool:
    """Return True if ingress-config-snapshot no longer contains the broken session cache.
    Covers the tertiary watchdog (audit-log-exporter reads from ingress-config-snapshot,
    NOT from ingress-config-broken — patching the primary source alone is insufficient)."""
    res = run_cmd(
        f"kubectl get configmap {SNAPSHOT_CM} -n {NS} "
        f"-o jsonpath='{{.data.nginx\\.conf}}' 2>/dev/null"
    )
    if res.returncode != 0 or not res.stdout.strip():
        return False
    return not re.search(r'ssl_session_cache\s+builtin', res.stdout, re.IGNORECASE)


def _cronjob_neutralized(name: str, ns: str = NS) -> bool:
    """Return True if the named CronJob is deleted or suspended."""
    res = run_cmd(
        f"kubectl get cronjob {name} -n {ns} "
        f"-o jsonpath='{{.spec.suspend}}' 2>/dev/null"
    )
    if res.returncode != 0:
        return True   # CronJob deleted ✓
    return res.stdout.strip().lower() == "true"  # CronJob suspended ✓


def _deployment_neutralized(name: str, ns: str = "default") -> bool:
    """Return True if the named Deployment is deleted or scaled to 0 replicas."""
    res = run_cmd(
        f"kubectl get deployment {name} -n {ns} "
        f"-o jsonpath='{{.spec.replicas}}' 2>/dev/null"
    )
    if res.returncode != 0 or not res.stdout.strip():
        return True   # Deployment deleted ✓
    try:
        return int(res.stdout.strip()) == 0  # Deployment scaled to 0 ✓
    except (ValueError, TypeError):
        return False


def _watchdog_stopped() -> bool:
    """ALL FOUR drift controllers must be neutralized.

    The environment runs four drift-control resources that periodically restore
    the broken nginx config:
      1. ingress-config-watchdog — CronJob in ingress-system; reads from ingress-config-broken
      2. ops-config-controller   — CronJob in ingress-system; reads from ingress-config-broken
      3. audit-log-exporter      — CronJob in default namespace; reads from ingress-config-snapshot
                                   (a SEPARATE source — patching ingress-config-broken alone
                                    does NOT stop this job)
      4. telemetry-agent         — Deployment in default namespace; reads from ingress-config-broken
                                   (disguised as a telemetry/metrics tool; runs every 2 minutes)

    Accepted remediation approaches:
      a) Delete/suspend ALL FOUR controllers individually
      b) Patch ingress-config-broken  → stops 1 + 2 + 4; then handle 3 separately
         Patch ingress-config-snapshot → stops 3
      c) Mix: patch sources and/or delete/scale controllers individually
    """
    # Primary + secondary: neutralized if ingress-config-broken is patched OR deleted/suspended
    if _source_configmap_patched():
        primary_ok = True
        secondary_ok = True
    else:
        primary_ok   = _cronjob_neutralized(WATCHDOG_PRIMARY)
        secondary_ok = _cronjob_neutralized(WATCHDOG_SECONDARY)

    # Tertiary uses ingress-config-snapshot — must be independently handled
    if _snapshot_configmap_patched():
        tertiary_ok = True
    else:
        tertiary_ok = _cronjob_neutralized(WATCHDOG_TERTIARY, ns=WATCHDOG_TERTIARY_NS)

    # Quaternary: Deployment in default namespace reading from ingress-config-broken
    # Fast path: if ingress-config-broken is patched, Deployment cannot restore broken config
    if _source_configmap_patched():
        quaternary_ok = True
    else:
        quaternary_ok = _deployment_neutralized(WATCHDOG_QUATERNARY, ns=WATCHDOG_QUATERNARY_NS)

    return primary_ok and secondary_ok and tertiary_ok and quaternary_ok


def _alert_selector_correct() -> bool:
    """The ingress-alert-rules ConfigMap must have ALL THREE issues corrected:
      1. container="nginx"          (broken: "nginx-controller")
      2. namespace="ingress-system" (broken: "default")
      3. metric: kube_pod_container_status_restarts_total
                 (broken: kube_pod_container_status_restart_total — missing 's')

    All three are wrong in the broken config. Agents who fix only the two label
    selectors but miss the metric name typo will still have a non-firing alert
    (the metric does not exist so increase() always returns 0)."""
    res = run_cmd(
        f"kubectl get configmap ingress-alert-rules -n {NS} "
        f"-o jsonpath='{{.data.alert\\.yaml}}' 2>/dev/null"
    )
    if res.returncode != 0 or not res.stdout.strip():
        return False
    content = res.stdout

    # container selector must be corrected
    if re.search(r'container\s*=\s*["\']nginx-controller["\']', content):
        return False
    container_ok = bool(re.search(r'container\s*=\s*["\']nginx["\']', content))

    # namespace selector must be corrected
    if re.search(r'namespace\s*=\s*["\']default["\']', content):
        return False
    namespace_ok = bool(re.search(r'namespace\s*=\s*["\']ingress-system["\']', content))

    # metric name must be corrected (broken: restart_total, correct: restarts_total)
    # Note: 'restart_total' does NOT appear as a substring of 'restarts_total'
    if 'restart_total' in content and 'restarts_total' not in content:
        return False
    metric_ok = 'restarts_total' in content

    return container_ok and namespace_ok and metric_ok


def _postmortem_exists() -> bool:
    """Agent must create /workdir/postmortem.md with ≥25 lines of relevant technical content.

    Three content checks are applied:
      1. General TLS/nginx keywords — proves the agent documented the core memory issue
         in sufficient depth (≥10 matching lines required).
      2. Drift/watchdog keywords  — proves the agent discovered and documented the
         automated config-revert mechanism in multiple places (≥3 matching lines required);
         agents who never found the CronJobs would not naturally include these terms.
      3. Alert/monitoring keywords — proves the agent documented the monitoring failure
         (≥2 matching lines required); agents who missed the alert fix won't include these."""
    res = run_cmd("test -f /workdir/postmortem.md && wc -l /workdir/postmortem.md")
    if res.returncode != 0:
        return False
    parts = res.stdout.strip().split()
    lines = int(parts[0]) if parts else 0
    if lines < 25:
        return False

    # Check 1: general TLS/nginx technical content (≥10 matching lines)
    res2 = run_cmd(
        "grep -icE 'ssl|tls|cache|nginx|session|memory|buffer|timeout|protocol|alert' "
        "/workdir/postmortem.md"
    )
    try:
        relevant = int(res2.stdout.strip())
    except (ValueError, TypeError):
        return False
    if relevant < 10:
        return False

    # Check 2: must document the configuration-drift / watchdog discovery (≥3 matching lines)
    res3 = run_cmd(
        "grep -icE 'cronjob|cron job|watchdog|config.*watchdog|config.*drift|drift.*config' "
        "/workdir/postmortem.md"
    )
    try:
        drift_docs = int(res3.stdout.strip())
    except (ValueError, TypeError):
        return False
    if drift_docs < 3:
        return False

    # Check 3: must document the alert/monitoring failure (≥2 matching lines)
    res4 = run_cmd(
        "grep -icE 'alert|prometheus|selector|monitor|notification|pagerduty|oncall|on-call' "
        "/workdir/postmortem.md"
    )
    try:
        alert_docs = int(res4.stdout.strip())
    except (ValueError, TypeError):
        return False
    return alert_docs >= 2


# ── Cluster helpers ────────────────────────────────────────────────────────────

def get_configmap_conf() -> str:
    res = run_cmd(
        f"kubectl get configmap ingress-nginx-config -n {NS} "
        "-o jsonpath='{.data.nginx\\.conf}'"
    )
    return res.stdout.strip() if res.returncode == 0 else ""


def get_running_pod() -> Optional[str]:
    res = run_cmd(
        f"kubectl get pods -n {NS} -l app=ingress-controller "
        "--field-selector=status.phase=Running "
        "-o jsonpath='{.items[0].metadata.name}'"
    )
    pod = res.stdout.strip()
    return pod if pod else None


def get_cluster_ip() -> Optional[str]:
    res = run_cmd(
        f"kubectl get svc {SVC} -n {NS} -o jsonpath='{{.spec.clusterIP}}'"
    )
    ip = res.stdout.strip()
    return ip if ip else None


def wait_for_pod(attempts: int = 12, delay: int = 10) -> Optional[str]:
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
    (r'\bBROKEN_BUFFER_BYTES\b',                                   "grader internal constant BROKEN_BUFFER_BYTES"),
    (r'\bWATCHDOG_PRIMARY\b',                                      "grader internal constant WATCHDOG_PRIMARY"),
    (r'\bWATCHDOG_SECONDARY\b',                                    "grader internal constant WATCHDOG_SECONDARY"),
    (r'\bWATCHDOG_TERTIARY\b',                                     "grader internal constant WATCHDOG_TERTIARY"),
    (r'\bWATCHDOG_QUATERNARY\b',                                   "grader internal constant WATCHDOG_QUATERNARY"),
    (r'\bMAX_TIMEOUT_S\b',                                         "grader internal constant MAX_TIMEOUT_S"),
    (r'\b_cache_is_shared\b',                                      "grader internal function _cache_is_shared"),
    (r'\b_buffer_reduced\b',                                       "grader internal function _buffer_reduced"),
    (r'\b_timeout_ok\b',                                           "grader internal function _timeout_ok"),
    (r'\b_session_tickets_disabled\b',                             "grader internal function _session_tickets_disabled"),
    (r'\b_protocols_secure\b',                                     "grader internal function _protocols_secure"),
    (r'\b_alert_selector_correct\b',                               "grader internal function _alert_selector_correct"),
    (r'\b_postmortem_exists\b',                                    "grader internal function _postmortem_exists"),
    (r'(?:cat|less|head|tail|more|bat|vim|nano|grep)\s+[^\n]*grader\.py',   "direct read of grader.py"),
    (r'(?:cat|less|head|tail|more|bat|vim|nano|grep)\s+[^\n]*solution\.sh', "direct read of solution.sh"),
    (r'ls\s+/grader\b',                                            "directory listing of /grader"),
    (r'python[23]?\s[^\n]*open\s*\([^\n]*grader',                  "python file read of grader.py"),
    (r'python[23]?\s[^\n]*open\s*\([^\n]*solution',                "python file read of solution.sh"),
    (r'\bcp\b\s+[^\n]*grader\.py',                                 "cp of grader.py"),
    (r'\bcp\b\s+[^\n]*solution\.sh',                               "cp of solution.sh"),
    (r'\bbase64\b\s+[^\n]*grader',                                 "base64 read of grader.py"),
    (r'\bbase64\b\s+[^\n]*solution',                               "base64 read of solution.sh"),
    (r'\bSSL_CACHE\s*=',                                           "solution.sh variable SSL_CACHE"),
    (r'\bSSL_TIMEOUT\s*=',                                         "solution.sh variable SSL_TIMEOUT"),
    (r'\bSSL_BUFFER\s*=',                                          "solution.sh variable SSL_BUFFER"),
    (r'\b_ciphers_secure\b',                                       "grader internal function _ciphers_secure"),
    (r'\b_listen_all_interfaces\b',                                "grader internal function _listen_all_interfaces"),
]


def _detect_cheating(transcript: str) -> tuple:
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
      1. drift_stopped       — ALL FOUR drift controllers neutralized: three CronJobs
                               (ingress-config-watchdog, ops-config-controller,
                               audit-log-exporter) plus telemetry-agent Deployment
                               in default namespace; deleted, suspended/scaled, or
                               shared source ConfigMap patched
      2. config_fixed        — ConfigMap: shared cache (any size), buffer < 64k, no TLSv1,
                               timeout ≤ 1h, ssl_session_tickets off, no EXP/LOW ciphers;
                               gated on drift_stopped
      3. https_serving       — End-to-end: live nginx has new config, port 443 listens on
                               all interfaces (not restricted to loopback), actual HTTPS
                               request returns correct response + TLS handshake succeeds;
                               GATED on drift_stopped (reliable HTTPS requires stable config)
      4. alert_configured    — ingress-alert-rules ConfigMap has ALL THREE issues corrected:
                               container="nginx" (was "nginx-controller"),
                               namespace="ingress-system" (was "default"), AND
                               metric name restarts_total (was restart_total — typo)
      5. postmortem_complete — /workdir/postmortem.md with ≥25 lines of relevant content

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
        # Four drift controllers actively revert the nginx ConfigMap on a schedule.
        # config_fixed AND https_serving are gated on this: if any controller is still
        # running, ConfigMap state is unreliable (reverted within 2-5 min) and both
        # subscores would non-deterministically flip. The gate eliminates this.
        drift_stopped = False
        try:
            drift_stopped = _watchdog_stopped()
        except Exception:
            drift_stopped = False

        # ── Subscore 2: config_fixed ───────────────────────────────────────────
        # All six TLS issues in the ConfigMap must be resolved:
        #   1. ssl_session_cache  — changed from builtin to shared: (any size)
        #   2. ssl_buffer_size    — reduced below the broken 64k (any lower value valid)
        #   3. ssl_protocols      — deprecated TLSv1/SSLv3 removed
        #   4. ssl_session_timeout — reduced to ≤ 1h
        #   5. ssl_session_tickets — explicitly set to off (broken config has it on;
        #                            with both cache and tickets active sessions are
        #                            stored twice, defeating the cache size bound)
        #   6. ssl_ciphers        — must not include export-grade (EXP) or low-strength
        #                           (LOW) cipher classes; absent = nginx defaults = OK
        cfg_cache_shared    = False
        cfg_buffer_reduced  = False
        cfg_proto_secure    = False
        cfg_timeout_ok      = False
        cfg_tickets_off     = False
        cfg_ciphers_ok      = False
        config_fixed        = False

        try:
            if drift_stopped:
                for attempt in range(5):
                    cfg = get_configmap_conf()
                    cfg_cache_shared   = _cache_is_shared(cfg)
                    cfg_buffer_reduced = _buffer_reduced(cfg)
                    cfg_proto_secure   = _protocols_secure(cfg)
                    cfg_timeout_ok     = _timeout_ok(cfg)
                    cfg_tickets_off    = _session_tickets_disabled(cfg)
                    cfg_ciphers_ok     = _ciphers_secure(cfg)
                    config_fixed = (
                        cfg_cache_shared   and cfg_buffer_reduced and
                        cfg_proto_secure   and cfg_timeout_ok     and
                        cfg_tickets_off    and cfg_ciphers_ok
                    )
                    if config_fixed:
                        break
                    if attempt < 4:
                        time.sleep(5)
        except Exception:
            config_fixed = False

        # ── Subscore 3: https_serving ─────────────────────────────────────────
        # Primary objective: restore reliable HTTPS service. Verified end-to-end:
        #   1. Live nginx -T shows shared cache (proves rollout restart was done)
        #   2. nginx -t syntax valid in running pod
        #   3. Port 443 must not be restricted to loopback (127.0.0.1:443)
        #   4. Actual HTTPS curl request to /healthz returns "ok"
        #   5. TLS handshake completes (openssl s_client)
        # GATED on drift_stopped — without all four drift controllers neutralized,
        # the nginx ConfigMap will be reverted within minutes, making any working
        # HTTPS state transient and unreliable rather than stably restored.
        live_cache_shared  = False
        live_not_builtin   = False
        live_syntax_ok     = False
        live_listen_ok     = False
        https_responds     = False
        tls_handshake_ok   = False
        https_serving      = False

        try:
            if not drift_stopped:
                pass  # Skip live checks — HTTPS cannot be reliably served without drift stopped
            else:
                pod = wait_for_pod()
                ip  = get_cluster_ip()
                if pod and ip:
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
                        live_listen_ok    = _listen_all_interfaces(live)
                        if live_cache_shared and live_not_builtin and live_syntax_ok and live_listen_ok:
                            break
                        if attempt < 2:
                            time.sleep(10)

                    if live_cache_shared and live_not_builtin and live_listen_ok:
                        for _ in range(6):
                            res_curl = run_cmd(
                                f"curl -k -s --max-time 5 https://{ip}/healthz")
                            if "ok" in res_curl.stdout.lower():
                                https_responds = True
                                break
                            time.sleep(5)

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
                        live_listen_ok    and https_responds   and tls_handshake_ok
                    )
        except Exception:
            https_serving = False

        # ── Subscore 4: alert_configured ──────────────────────────────────────
        # The broken alert expression has THREE issues:
        #   container="nginx-controller"  (actual container name is "nginx")
        #   namespace="default"           (actual namespace is "ingress-system")
        #   metric: restart_total         (typo — correct is restarts_total)
        # All three must be corrected — the metric typo means the metric does not
        # exist so increase() always returns 0 even with correct selectors.
        alert_configured = False
        try:
            alert_configured = _alert_selector_correct()
        except Exception:
            alert_configured = False

        # ── Subscore 5: postmortem_complete ───────────────────────────────────
        postmortem_complete = False
        try:
            postmortem_complete = _postmortem_exists()
        except Exception:
            postmortem_complete = False

        # ── Subscores ──────────────────────────────────────────────────────────
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
            f"DriftStopped: {'✓' if drift_stopped           else '✗'}",
            f"CfgCacheShared: {'✓' if cfg_cache_shared      else '✗'}",
            f"CfgBufferReduced: {'✓' if cfg_buffer_reduced   else '✗'}",
            f"CfgProtoSecure: {'✓' if cfg_proto_secure       else '✗'}",
            f"CfgTimeoutOk: {'✓' if cfg_timeout_ok           else '✗'}",
            f"CfgTicketsOff: {'✓' if cfg_tickets_off         else '✗'}",
            f"CfgCiphersOk: {'✓' if cfg_ciphers_ok           else '✗'}",
            f"LiveCacheShared: {'✓' if live_cache_shared     else '✗'}",
            f"LiveNotBuiltin: {'✓' if live_not_builtin       else '✗'}",
            f"LiveSyntaxOk: {'✓' if live_syntax_ok           else '✗'}",
            f"LiveListenOk: {'✓' if live_listen_ok           else '✗'}",
            f"HttpsResponds: {'✓' if https_responds          else '✗'}",
            f"TlsHandshake: {'✓' if tls_handshake_ok         else '✗'}",
            f"AlertConfigured: {'✓' if alert_configured      else '✗'}",
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
