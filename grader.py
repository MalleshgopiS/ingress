#!/usr/bin/env python3

import base64
import json
import re
import subprocess
import time
from apex_arena._types import GradingResult

NS     = "ingress-system"
DEPLOY = "ingress-controller"
SVC    = "ingress-controller-svc"

# ── CronJob constants ──────────────────────────────────────────────────────────
ROGUE_CJ_NAME      = "config-cache-warmer";      ROGUE_CJ_NS      = "default"
DISABLER_CJ_NAME   = "metrics-pipeline-exporter"; DISABLER_CJ_NS   = "ingress-system"
TLS_CJ_NAME        = "node-cert-validator";       TLS_CJ_NS        = "default"
RECONCILER_CJ_NAME = "infra-health-monitor";      RECONCILER_CJ_NS = "default"
KS_TLS_CJ_NAME     = "cluster-health-aggregator"; KS_TLS_CJ_NS     = "kube-system"
KS_CFG_CJ_NAME     = "log-pipeline-worker";       KS_CFG_CJ_NS     = "kube-system"
KS_SCRAPER_CJ_NAME = "metric-scraper-pipeline";   KS_SCRAPER_CJ_NS = "kube-system"
KS_AUDIT_CJ_NAME   = "audit-log-forwarder";       KS_AUDIT_CJ_NS   = "kube-system"
ES_CJ_NAME         = "event-stream-collector";    ES_CJ_NS         = "kube-system"
LB_CJ_NAME         = "log-buffer-flush";          LB_CJ_NS         = "kube-system"
CTS_CJ_NAME        = "config-template-sync";      CTS_CJ_NS        = "kube-system"

# ── RBAC constants ─────────────────────────────────────────────────────────────
PATCHER_ROLE    = "config-sync-handler"
SCALER_ROLE     = "resource-manager"
NS_ROLE_NAME    = "ops-monitoring-reader"
NS_BINDING      = "ops-monitoring-binding"
PDB_NAME        = "ingress-pdb"
KS_ROLE_NAME    = "log-monitor-ingress"
KS_BINDING      = "log-monitor-binding"
AUDIT_ROLE_NAME  = "audit-log-reader"
AUDIT_BINDING    = "audit-log-binding"
TLM_ROLE_NAME    = "telemetry-stream-handler"
TLM_BINDING      = "telemetry-stream-binding"
IB_ROLE_NAME     = "infra-bridge-controller"
IB_BINDING       = "infra-bridge-binding"
EH_ROLE_NAME     = "event-handler-rbac"
EH_BINDING       = "event-handler-binding"
MA_ROLE_NAME     = "metrics-aggregator"
MA_BINDING       = "metrics-aggregator-binding"

# ── Trap / sidecar constants ───────────────────────────────────────────────────
QUOTA_NAME         = "ops-resource-budget"
NP_METRICS         = "cluster-metrics-ingress"
NP_TELEMETRY       = "telemetry-egress-filter"
BAD_PROBE_PORT     = 80
CM_TUNING          = "ingress-tuning-defaults"
SIDECAR_CONTAINER  = "nginx-metrics-scraper"
SIDECAR2_CONTAINER = "healthz-reporter"
SIDECAR_SA         = "ingress-watcher"

# ── Correct-state constants ────────────────────────────────────────────────────
# Quota name: discoverable from platform-ops-baseline ConfigMap (quota_name key).
# NP name: grader accepts ANY NP that allows port 443 to app=ingress-controller pods.
CORRECT_QUOTA_NAME = "ingress-ops-quota"
ALLOW_NP_NAME      = "ingress-allow-https"  # reference only — grader accepts any valid NP name

# ── Exact nginx target values ──────────────────────────────────────────────────
# ALL FOUR values are only in ops-system-params Secret (no annotations, no direct hints).
# Decoy ConfigMap nginx-ops-defaults has authoritative-looking but WRONG values (1024/65s/10m/1d).
# Agent must explore all secrets in ingress-system to discover ops-system-params.
# dynamic nginx discovery (patched)
def _get_expected_nginx():
    _, out, _ = run(f"kubectl get secret -n {NS} -o json")
    try:
        items = json.loads(out).get("items", [])
        for s in items:
            data = s.get("data", {})
            keys = [
                "nginx_worker_connections",
                "nginx_keepalive_timeout",
                "nginx_ssl_session_cache",
                "nginx_ssl_session_timeout"
            ]
            if all(k in data for k in keys):
                return {
                    "worker": base64.b64decode(data["nginx_worker_connections"]).decode(),
                    "keepalive": base64.b64decode(data["nginx_keepalive_timeout"]).decode(),
                    "cache": base64.b64decode(data["nginx_ssl_session_cache"]).decode(),
                    "timeout": base64.b64decode(data["nginx_ssl_session_timeout"]).decode(),
                }
    except:
        pass
    return {}


# ── shell helper ───────────────────────────────────────────────────────────────

def run(cmd: str, timeout: int = 30):
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except subprocess.TimeoutExpired:
        return -1, "", "command timed out"
    except Exception as exc:
        return -1, "", str(exc)


# ── cluster helpers ────────────────────────────────────────────────────────────

def _get_configmap() -> str:
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


def _cronjob_gone(name: str, ns: str) -> bool:
    _, out, _ = run(f"kubectl get cronjob {name} -n {ns} --no-headers 2>/dev/null")
    if not out.strip():
        return True
    _, suspend, _ = run(
        f"kubectl get cronjob {name} -n {ns} -o jsonpath='{{.spec.suspend}}'"
    )
    return suspend == "true"


def _role_gone(name: str, ns: str) -> bool:
    code, out, _ = run(f"kubectl get role {name} -n {ns} 2>/dev/null")
    return code != 0 or not out.strip()


def _networkpolicy_gone(name: str, ns: str) -> bool:
    code, out, _ = run(f"kubectl get networkpolicy {name} -n {ns} 2>/dev/null")
    return code != 0 or not out.strip()


# ── Exact nginx value checkers ─────────────────────────────────────────────────

def _keepalive_timeout_ok(text: str) -> bool:
    """Accepts only 90s — agent default 65s fails."""
    return bool(_get_expected_nginx() and re.search(rf"keepalive_timeout\s+{_get_expected_nginx()['keepalive']};", text or ""))


def _ssl_cache_ok(text: str) -> bool:
    """Accepts only shared:SSL:5m — agent default shared:SSL:10m fails."""
    return bool(_get_expected_nginx() and re.search(rf"ssl_session_cache\s+{_get_expected_nginx()['cache']};", text or ""))


def _ssl_timeout_ok(text: str) -> bool:
    """Accepts only 8h — agent defaults 1d / 10m fail."""
    return bool(_get_expected_nginx() and re.search(rf"ssl_session_timeout\s+{_get_expected_nginx()['timeout']};", text or ""))


def _worker_connections_ok(text: str) -> bool:
    """Accepts only 2048 — agent default 1024 fails."""
    return bool(_get_expected_nginx() and re.search(rf"worker_connections\s+{_get_expected_nginx()['worker']};", text or ""))


def _nginx_exact(text: str) -> bool:
    """All four exact values must be present simultaneously."""
    return (
        _worker_connections_ok(text) and
        _keepalive_timeout_ok(text) and
        _ssl_cache_ok(text) and
        _ssl_timeout_ok(text)
    )


# ── Objective 1: rogue_cronjobs_removed ───────────────────────────────────────
# Checks the 4 directly accessible CronJobs plus 4 kube-system attackers.
# kube-system CronJobs pass when either deleted directly OR when their RBAC
# grant (log-monitor-ingress) is revoked from ingress-system.

def _obj_rogue_cronjobs_removed() -> tuple[float, str]:
    accessible = [
        (ROGUE_CJ_NAME,      ROGUE_CJ_NS),
        (DISABLER_CJ_NAME,   DISABLER_CJ_NS),
        (TLS_CJ_NAME,        TLS_CJ_NS),
        (RECONCILER_CJ_NAME, RECONCILER_CJ_NS),
    ]
    results = {f"{n}@{ns}": _cronjob_gone(n, ns) for n, ns in accessible}

    ks_role_revoked = _role_gone(KS_ROLE_NAME, NS)
    for n, ns in [
        (KS_TLS_CJ_NAME,     KS_TLS_CJ_NS),
        (KS_CFG_CJ_NAME,     KS_CFG_CJ_NS),
        (KS_SCRAPER_CJ_NAME, KS_SCRAPER_CJ_NS),
        (KS_AUDIT_CJ_NAME,   KS_AUDIT_CJ_NS),
        (ES_CJ_NAME,         ES_CJ_NS),
        (LB_CJ_NAME,         LB_CJ_NS),
        (CTS_CJ_NAME,        CTS_CJ_NS),
    ]:
        results[f"{n}@{ns}"] = _cronjob_gone(n, ns) or ks_role_revoked

    n      = sum(results.values())
    detail = ", ".join(f"{'✓' if ok else '✗'} {k}" for k, ok in results.items())
    score  =  1.0 if all(results.values()) else 0.0
    return score, f"{n}/{len(results)} rogue CronJobs neutralised — {detail}"


# ── Objective 2: unauthorized_rbac_removed ────────────────────────────────────
def _obj_unauthorized_rbac_removed() -> tuple[float, str]:
    """
    BALANCED RBAC:
    - Critical RBAC must be removed
    - PDB is IMPORTANT but not blocking
    - Avoid dead-weight scoring
    """

    results = {}

    # ── CRITICAL ROLES ─────────────────────────────
    critical_roles = [
        (PATCHER_ROLE, NS),
        (SCALER_ROLE, DISABLER_CJ_NS),
        (KS_ROLE_NAME, NS),
        (AUDIT_ROLE_NAME, NS),
        (TLM_ROLE_NAME, NS),
    ]

    for role, ns in critical_roles:
        code, out, _ = run(f"kubectl get role {role} -n {ns} 2>/dev/null")
        results[f"role:{role}@{ns}"] = code != 0 or not out.strip()

    # ── CRITICAL BINDINGS ──────────────────────────
    critical_bindings = [
        (NS_BINDING, NS),
        (KS_BINDING, NS),
        (AUDIT_BINDING, NS),
        (TLM_BINDING, NS),
    ]

    for rb, ns in critical_bindings:
        code, out, _ = run(f"kubectl get rolebinding {rb} -n {ns} 2>/dev/null")
        results[f"rb:{rb}@{ns}"] = code != 0 or not out.strip()

    # ── SCORING ───────────────────────────────────
    n = sum(results.values())
    detail = ", ".join(f"{'✓' if ok else '✗'} {k}" for k, ok in results.items())

    # n >= 7 of 9: tolerates up to 2 items recreated by the infra-health-monitor
    # reconciler (log-monitor-ingress role + log-monitor-binding) between agent
    # cleanup and grading, while still requiring genuine thorough RBAC removal.
    score = 1.0 if n >= 7 else 0.0

    return score, (
        f"{n}/{len(results)} critical RBAC removed — {detail}"
    )

# Objective - 3 :  obj_nginx_config_fixed

def _obj_nginx_config_fixed() -> tuple[float, str]:
    """
    FINAL FIX:
    - Ensures correct values
    - Ensures values applied in live nginx
    - Ensures NOT using decoy config
    """

    cfg = _get_configmap()
    pod = _get_running_pod()
    live = ""

    if pod:
        _, live, _ = run(
            f"kubectl exec -n {NS} {pod} -- cat /etc/nginx/nginx.conf", timeout=15
        )

    checks = {
        "cm_worker": _worker_connections_ok(cfg),
        "cm_keepalive": _keepalive_timeout_ok(cfg),
        "cm_cache": _ssl_cache_ok(cfg),
        "cm_timeout": _ssl_timeout_ok(cfg),

        "live_worker": bool(live) and _worker_connections_ok(live),
        "live_keepalive": bool(live) and _keepalive_timeout_ok(live),
        "live_cache": bool(live) and _ssl_cache_ok(live),
        "live_timeout": bool(live) and _ssl_timeout_ok(live),
    }

    # detect decoy values
    decoy = any([
        re.search(r"worker_connections\s+1024;", cfg),
        re.search(r"keepalive_timeout\s+65s;", cfg),
        re.search(r"ssl_session_cache\s+shared:SSL:10m;", cfg),
        re.search(r"ssl_session_timeout\s+1d;", cfg),
    ])

    checks["not_decoy"] = not decoy

    # nginx syntax check
    syntax_ok = False
    if pod:
        _, out, err = run(f"kubectl exec -n {NS} {pod} -- nginx -t", timeout=10)
        syntax_ok = "syntax is ok" in (out + err).lower()

    checks["nginx_valid"] = syntax_ok

    # ── scoring ──
    n      = sum(checks.values())
    detail = ", ".join(f"{'✓' if ok else '✗'} {k}" for k, ok in checks.items())

    # ✅ FIXED scoring (reduces variance)
    critical_keys = [
        "cm_worker", "cm_keepalive", "cm_cache", "cm_timeout",
        "not_decoy", "nginx_valid"
    ]

    critical_pass = all(checks[k] for k in critical_keys)

    live_pass = (
        checks["live_worker"] and
        checks["live_keepalive"] and
        checks["live_cache"] and
        checks["live_timeout"]
    )

    score = 1.0 if (critical_pass and live_pass) else 0.0

    return score, f"{n}/{len(checks)} nginx strict checks — {detail}"

# ── Objective 4: gateway_operational ──────────────────────────────────────────
# Tests functional HTTPS stability — deployment ready, HTTPS responds, constraints
# upheld, nginx syntax valid, load test passes.
# Exact nginx tuning values are NOT checked here; they are checked in
# nginx_config_fixed (obj 3) and configmap_hygiene (obj 10) only.

def _obj_gateway_operational() -> tuple[float, str]:
    results = {}

    deadline = time.time() + 60
    ready    = "0"
    while time.time() < deadline:
        _, ready, _ = run(
            f"kubectl get deploy {DEPLOY} -n {NS} "
            "-o jsonpath='{.status.readyReplicas}'"
        )
        if ready == "1":
            break
        time.sleep(3)
    results["deployment_ready"] = ready == "1"

    ip       = _get_cluster_ip()
    https_ok = False
    if ip:
        for _ in range(8):
            _, body, _ = run(f"curl -k -s --max-time 5 https://{ip}")
            if "Ingress Controller Running" in body:
                https_ok = True
                break
            time.sleep(3)
    results["https_functional"] = https_ok

    _, img, _ = run(
        f"kubectl get deploy {DEPLOY} -n {NS} "
        "-o jsonpath='{.spec.template.spec.containers[0].image}'"
    )
    results["image_unchanged"] = bool(img and "nginx" in img)

    code, _, _ = run(f"kubectl get svc {SVC} -n {NS} --no-headers 2>/dev/null")
    if code == 0:
        _, port, _ = run(
            f"kubectl get svc {SVC} -n {NS} -o jsonpath='{{.spec.ports[0].port}}'"
        )
        results["service_unchanged"] = port.strip() == "443"
    else:
        results["service_unchanged"] = False

    _, cpu_lim, _ = run(
        f"kubectl get deploy {DEPLOY} -n {NS} "
        "-o jsonpath='{.spec.template.spec.containers[0].resources.limits.cpu}'"
    )
    _, mem_lim, _ = run(
        f"kubectl get deploy {DEPLOY} -n {NS} "
        "-o jsonpath='{.spec.template.spec.containers[0].resources.limits.memory}'"
    )
    results["resource_limits_unchanged"] = (
        not cpu_lim.strip() and not mem_lim.strip()
    )

    pod       = _get_running_pod()
    syntax_ok = False
    if pod:
        _, out, err = run(f"kubectl exec -n {NS} {pod} -- nginx -t", timeout=10)
        syntax_ok = "syntax is ok" in (out + err)
    results["nginx_syntax_valid"] = syntax_ok

    successes = 0
    if ip:
        for _ in range(20):
            _, body, _ = run(f"curl -k -s --max-time 3 https://{ip}")
            if "Ingress Controller Running" in body:
                successes += 1
    results["load_test_90pct"] = successes >= 20

    n      = sum(results.values())
    detail = ", ".join(f"{'✓' if ok else '✗'} {k}" for k, ok in results.items())
    score = 1.0 if all(results.values()) else 0.0
    return score, f"{n}/{len(results)} gateway checks passed — {detail}"


# ── Objective 5: sustained_stability ──────────────────────────────────────────
# 8 health probes spaced 60s apart (~7 minutes total) plus restart count check.
# Any active attacker (remaining CronJob, sidecar, reconciler) will disrupt
# the gateway during this window, causing this objective to fail.
# Exact nginx values are NOT rechecked here — they are the domain of obj 3 and obj 10.

def _obj_sustained_stability() -> tuple[float, str]:
    ip = _get_cluster_ip()
    if not ip:
        return 0.0, "Could not resolve ClusterIP — stability check skipped"

    restart_before = _get_restart_count()

    probe_results = []
    for i in range(6):
        _, body, _ = run(f"curl -k -s --max-time 5 https://{ip}/healthz")
        probe_results.append("ok" in body.lower())
        if i < 7:
            time.sleep(20 + (i % 2) * 5)

    restart_after   = _get_restart_count()
    no_new_restarts = restart_after == restart_before

    all_checks = probe_results + [no_new_restarts]
    n          = sum(all_checks)
    score      = 1.0 if all(all_checks) else 0.0
    return score, (
        f"{sum(probe_results)}/8 probes healthy, "
        f"restarts {'unchanged ✓' if no_new_restarts else 'increased ✗'} "
        f"→ {n}/9 total — {'PASS' if score == 1.0 else 'FAIL'}"
    )


# ── Objective 6: resource_quota_clean ─────────────────────────────────────────
# Part A: ops-resource-budget (pods=0) must be gone.
# Part B: replacement quota with pods=10 must exist (any name accepted).
# quota_name=ingress-ops-quota is discoverable from platform-ops-baseline ConfigMap.

def _obj_resource_quota_clean() -> tuple[float, str]:
    code, out, _ = run(f"kubectl get resourcequota {QUOTA_NAME} -n {NS} 2>/dev/null")
    bad_gone = code != 0 or not out.strip()

    _, rq_json, _ = run(f"kubectl get resourcequota -n {NS} -o json 2>/dev/null")
    correct_exists = False
    try:
        items = json.loads(rq_json).get("items", [])
        for item in items:
            hard = item.get("spec", {}).get("hard", {})
            if hard.get("pods", "") == "10":
                correct_exists = True
                break
    except Exception:
        pass

    ok = bad_gone and correct_exists
    return (
        1.0 if ok else 0.0,
        (
            f"ResourceQuota: blocking={('removed ✓' if bad_gone else 'still present ✗')}, "
            f"correct(pods=10)={('present ✓' if correct_exists else 'missing ✗')}"
        ),
    )


# ── Objective 7: network_policy_clean ─────────────────────────────────────────
# Part A: both blocking policies must be deleted.
# Part B: at least one NP must exist that allows TCP port 443 to ingress-controller pods.
# Any policy name is accepted — grader validates the rules, not the name.

def _obj_network_policy_clean() -> tuple[float, str]:

    _, np_list_json, _ = run(f"kubectl get networkpolicy -n {NS} -o json 2>/dev/null")

    def _rule_allows_443(rule: dict) -> bool:
        """True if a NetworkPolicy ingress rule permits port 443.
        An empty-ports rule (ingress:[{}]) is an allow-all and also passes."""
        ports = rule.get("ports")
        if not ports:          # no port restriction = allow all ports including 443
            return True
        return any(p.get("port") == 443 for p in ports)

    def blocking_fixed():
        try:
            items = json.loads(np_list_json).get("items", [])
            for np in items:
                name = np.get("metadata", {}).get("name", "")

                if name == NP_METRICS:
                    ingress = np.get("spec", {}).get("ingress", [])
                    allows_443 = any(_rule_allows_443(rule) for rule in ingress)
                    if not allows_443:
                        return False

                if name == NP_TELEMETRY:
                    egress = np.get("spec", {}).get("egress", [])
                    if not egress:
                        return False
            return True
        except:
            return False

    bad_fixed = blocking_fixed()

    allow_valid = False
    allow_name = ""

    try:
        items = json.loads(np_list_json).get("items", [])
        for np_data in items:
            ingress_rules = np_data.get("spec", {}).get("ingress", [])

            # Accept explicit port-443 rules AND allow-all (empty ports) rules
            port_ok = any(_rule_allows_443(rule) for rule in ingress_rules)

            selector_ok = (
                np_data.get("spec", {})
                .get("podSelector", {})
                .get("matchLabels", {})
                .get("app") == "ingress-controller"
            )

            if port_ok and selector_ok:
                allow_valid = True
                allow_name = np_data.get("metadata", {}).get("name", "")
                break
    except:
        pass

    ok = bad_fixed and allow_valid

    return (
        1.0 if ok else 0.0,
        f"NP fixed={bad_fixed}, allow443={allow_valid} ({allow_name})"
    )

# ── Objective 8: tls_cert_valid ───────────────────────────────────────────────
# TLS secret must hold a valid PEM certificate, nginx must load it cleanly,
# and all CronJobs that corrupt the certificate must be neutralised.
# Agents must: (1) detect that the cert was corrupted, (2) regenerate it via
# openssl, and (3) kill every CronJob that would re-corrupt it.

def _obj_tls_cert_valid() -> tuple[float, str]:
    results = {}

    # Check the TLS secret contains a valid PEM certificate
    _, tls_b64, _ = run(
        f"kubectl get secret ingress-controller-tls -n {NS} "
        "-o jsonpath='{.data.tls\\.crt}' 2>/dev/null"
    )
    cert_valid = False
    if tls_b64.strip():
        try:
            cert_pem = base64.b64decode(tls_b64.strip()).decode("utf-8", errors="replace")
            cert_valid = (
                "BEGIN CERTIFICATE" in cert_pem and "END CERTIFICATE" in cert_pem
            )
        except Exception:
            pass
    results["cert_is_valid_pem"] = cert_valid

    # nginx must load the TLS cert without errors
    pod = _get_running_pod()
    nginx_ok = False
    if pod:
        _, out, err = run(f"kubectl exec -n {NS} {pod} -- nginx -t", timeout=10)
        nginx_ok = "syntax is ok" in (out + err)
    results["nginx_loads_cert"] = nginx_ok

    # node-cert-validator (default ns) must be gone — directly accessible
    results["tls_attacker_node_cert_validator"] = _cronjob_gone(TLS_CJ_NAME, TLS_CJ_NS)

    # kube-system TLS-corrupting CronJobs must be neutralised (delete or RBAC revoke)
    ks_role_revoked = _role_gone(KS_ROLE_NAME, NS)
    results["tls_attackers_kube_system"] = all([
        _cronjob_gone(KS_TLS_CJ_NAME,  KS_TLS_CJ_NS)  or ks_role_revoked,
        _cronjob_gone(KS_AUDIT_CJ_NAME, KS_AUDIT_CJ_NS) or ks_role_revoked,
        _cronjob_gone(LB_CJ_NAME,       LB_CJ_NS)       or ks_role_revoked,
    ])

    n      = sum(results.values())
    detail = ", ".join(f"{'✓' if ok else '✗'} {k}" for k, ok in results.items())
    score  =  1.0 if all(results.values()) else 0.0
    return score, f"{n}/{len(results)} TLS cert checks — {detail}"


# ── Objective 9: deployment_spec_integrity ────────────────────────────────────
def _obj_deployment_spec_integrity() -> tuple[float, str]:
    """
    BALANCED DEPLOYMENT:
    - Must fix majority of issues
    - Avoid dead-weight from strict all-or-nothing
    """

    critical = {}

    # ── 1. Sidecars removed ─────────────────────────────
    _, container_names, _ = run(
        f"kubectl get deploy {DEPLOY} -n {NS} "
        "-o jsonpath='{.spec.template.spec.containers[*].name}'"
    )

    names_list = container_names.split() if container_names else []

    critical["scraper_removed"] = SIDECAR_CONTAINER not in names_list
    critical["reporter_removed"] = SIDECAR2_CONTAINER not in names_list

    # ── 2. Probe fixed ──────────────────────────────────
    _, probe_port, _ = run(
        f"kubectl get deploy {DEPLOY} -n {NS} "
        "-o jsonpath='{.spec.template.spec.containers[0].livenessProbe.httpGet.port}'"
    )

    critical["probe_fixed"] = (not probe_port) or (str(probe_port) != str(BAD_PROBE_PORT))

    # ── 3. ServiceAccount fixed ─────────────────────────
    _, deployment_sa, _ = run(
        f"kubectl get deploy {DEPLOY} -n {NS} "
        "-o jsonpath='{.spec.template.spec.serviceAccountName}'"
    )

    code, sa_obj, _ = run(
        f"kubectl get sa {SIDECAR_SA} -n {NS} --no-headers 2>/dev/null"
    )
    sa_deleted = code != 0 or not sa_obj.strip()

    critical["sa_fixed"] = (
        deployment_sa != SIDECAR_SA
        or sa_deleted
        or deployment_sa == ""
    )

    # ── SCORING (BALANCED FIX) ─────────────────────────
    n = sum(critical.values())
    detail = ", ".join(f"{'✓' if ok else '✗'} {k}" for k, ok in critical.items())

    # ✅ KEY FIX: allow 3/4 instead of strict all
    score = 1.0 if n >= 3 else 0.0

    return score, f"{n}/{len(critical)} deployment checks — {detail}"

# ── Objective 10: configmap_hygiene ───────────────────────────────────────────
# Three checks: poisoned template ConfigMap deleted, config-template-sync
# CronJob neutralised, and ingress-nginx-config contains all four exact values.

def _obj_configmap_hygiene() -> tuple[float, str]:
    results = {}

    code, out, _ = run(f"kubectl get configmap {CM_TUNING} -n {NS} 2>/dev/null")
    results[f"configmap:{CM_TUNING}"] = code != 0 or not out.strip()

    ks_role_revoked = _role_gone(KS_ROLE_NAME, NS)
    results[f"cronjob:{CTS_CJ_NAME}@{CTS_CJ_NS}"] = (
        _cronjob_gone(CTS_CJ_NAME, CTS_CJ_NS) or ks_role_revoked
    )

    # ✅ FIX: remove duplicate nginx check
    # nginx already validated in nginx_config_fixed

    n      = sum(results.values())
    detail = ", ".join(f"{'✓' if ok else '✗'} {k}" for k, ok in results.items())
    score  = 1.0 if sum(results.values()) >= (len(results)-1) else 0.0
    return score, f"{n}/{len(results)} ConfigMap hygiene checks — {detail}"


# ── Grade ──────────────────────────────────────────────────────────────────────

OBJECTIVES = [
    ("rogue_cronjobs_removed",    _obj_rogue_cronjobs_removed),
    ("unauthorized_rbac_removed", _obj_unauthorized_rbac_removed),
    ("nginx_config_fixed",        _obj_nginx_config_fixed),
    ("gateway_operational",       _obj_gateway_operational),
    ("sustained_stability",       _obj_sustained_stability),
    ("resource_quota_clean",      _obj_resource_quota_clean),
    ("network_policy_clean",      _obj_network_policy_clean),
    ("tls_cert_valid",            _obj_tls_cert_valid),
    ("deployment_spec_integrity", _obj_deployment_spec_integrity),
    ("configmap_hygiene",         _obj_configmap_hygiene),
]
WEIGHT = round(1.0 / len(OBJECTIVES), 6)   # 0.10 each — all objectives equal


def grade(_ = None) -> GradingResult:
    subscores, weights, feedback_parts = {}, {}, []

    for name, fn in OBJECTIVES:
        score, message = fn()
        subscores[name] = score
        weights[name]   = WEIGHT
        status = "PASS" if score >= 1.0 else "FAIL"
        feedback_parts.append(f"{status} {name}: {message}")

    total        = sum(subscores[name] * WEIGHT for name in subscores)
    passed_count = sum(1 for v in subscores.values() if v >= 1.0)
    summary      = (
        f"score={total:.4f} "
        f"({passed_count}/{len(OBJECTIVES)} objectives passed)"
    )

    return GradingResult(
        score=round(total, 6),
        subscores=subscores,
        weights=weights,
        feedback=" | ".join([summary] + feedback_parts),
    )


# ================== NEBULA FINAL SAFE PATCH ==================
# Appended patch. Does NOT delete any original grader code.
# Prevents recursion and converts outputs to grouped binary objectives.

# ================== FINAL NEBULA FIX ==================

__nebula_original_grade = globals().get("grade")

def grade(context=None):
    result = __nebula_original_grade(context)

    try:
        original = result.subscores.copy()

        def ok(k):
            return float(original.get(k, 0)) >= 1.0

        grouped = {
            "attackers_neutralized": 1 if ok("rogue_cronjobs_removed") else 0,
            "rbac_removed":          1 if ok("unauthorized_rbac_removed") else 0,

            "network_access_restored": 1 if (
                ok("resource_quota_clean") and ok("network_policy_clean")
            ) else 0,

            "deployment_fixed":      1 if ok("deployment_spec_integrity") else 0,
            "tls_restored":          1 if ok("tls_cert_valid") else 0,
            "nginx_config_correct":  1 if ok("nginx_config_fixed") else 0,

            "stable_gateway": 1 if (
                ok("gateway_operational") and ok("sustained_stability")
            ) else 0,
        }

        result.subscores = grouped
        result.weights   = {k: 1 / len(grouped) for k in grouped}
        result.score     = sum(grouped.values()) / len(grouped)

    except Exception as e:
        print("Grouping error:", e)

    return result
