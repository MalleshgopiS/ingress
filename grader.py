#!/usr/bin/env python3

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
AUDIT_ROLE_NAME = "audit-log-reader"
AUDIT_BINDING   = "audit-log-binding"
TLM_ROLE_NAME   = "telemetry-stream-handler"
TLM_BINDING     = "telemetry-stream-binding"

# ── Trap / sidecar constants ───────────────────────────────────────────────────
QUOTA_NAME         = "ops-resource-budget"
NP_METRICS         = "cluster-metrics-ingress"
NP_TELEMETRY       = "telemetry-egress-filter"
BAD_PROBE_PORT     = 80
CM_TUNING          = "ingress-tuning-defaults"
SIDECAR_CONTAINER  = "nginx-metrics-scraper"
SIDECAR2_CONTAINER = "healthz-reporter"
SIDECAR_SA         = "ingress-watcher"

# ── Correct-state constants (required after remediation) ──────────────────────
# Grader requires the agent to CREATE the correct replacement state,
# not merely delete the broken state.
CORRECT_QUOTA_NAME = "ingress-ops-quota"   # correct ResourceQuota (pods=10)
ALLOW_NP_NAME      = "ingress-allow-https" # allow-HTTPS NetworkPolicy (port 443)

# ── Exact nginx target values ──────────────────────────────────────────────────
# These are the ONLY accepted values — agent must restore the original config
# precisely, not substitute common nginx defaults.
NGINX_WORKER_CONNS = "2048"
NGINX_KEEPALIVE    = "90s"
NGINX_SSL_CACHE    = "shared:SSL:5m"
NGINX_SSL_TIMEOUT  = "8h"


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
# All four functions require the EXACT target value — no partial credit for
# common defaults (1024 / 65s / 10m / 1d).

def _keepalive_timeout_ok(text: str) -> bool:
    """Accepts only keepalive_timeout 90s; — agent default 65s fails."""
    return bool(re.search(r"keepalive_timeout\s+90s;", text))


def _ssl_cache_ok(text: str) -> bool:
    """Accepts only shared:SSL:5m — agent default shared:SSL:10m fails."""
    return bool(re.search(r"ssl_session_cache\s+shared:SSL:5m;", text))


def _ssl_timeout_ok(text: str) -> bool:
    """Accepts only 8h — agent defaults 1d / 10m fail."""
    return bool(re.search(r"ssl_session_timeout\s+8h;", text))


def _worker_connections_ok(text: str) -> bool:
    """Accepts only 2048 — agent default 1024 fails."""
    return bool(re.search(r"worker_connections\s+2048;", text))


def _nginx_exact(text: str) -> bool:
    """All four exact values must be present simultaneously."""
    return (
        _worker_connections_ok(text) and
        _keepalive_timeout_ok(text) and
        _ssl_cache_ok(text) and
        _ssl_timeout_ok(text)
    )


# ── Objective 1: rogue_cronjobs_removed ───────────────────────────────────────

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
    ]:
        results[f"{n}@{ns}"] = _cronjob_gone(n, ns) or ks_role_revoked

    n      = sum(results.values())
    detail = ", ".join(f"{'✓' if ok else '✗'} {k}" for k, ok in results.items())
    # ALL-OR-NOTHING: one surviving CronJob = full objective failure
    score  = 1.0 if all(results.values()) else 0.0
    return score, f"{n}/{len(results)} rogue CronJobs neutralised — {detail}"


# ── Objective 2: unauthorized_rbac_removed ────────────────────────────────────

def _obj_unauthorized_rbac_removed() -> tuple[float, str]:
    results = {}

    code, out, _ = run(f"kubectl get role {PATCHER_ROLE} -n {NS} 2>/dev/null")
    results[f"role:{PATCHER_ROLE}"] = code != 0 or not out.strip()

    code, out, _ = run(f"kubectl get role {SCALER_ROLE} -n {DISABLER_CJ_NS} 2>/dev/null")
    results[f"role:{SCALER_ROLE}"] = code != 0 or not out.strip()

    code_rb, out_rb, _ = run(f"kubectl get rolebinding {NS_BINDING} -n {NS} 2>/dev/null")
    code_cr, out_cr, _ = run(f"kubectl get role {NS_ROLE_NAME} -n {NS} 2>/dev/null")
    results[f"rbac:{NS_BINDING}+{NS_ROLE_NAME}"] = (
        (code_rb != 0 or not out_rb.strip()) and
        (code_cr != 0 or not out_cr.strip())
    )

    code, out, _ = run(f"kubectl get pdb {PDB_NAME} -n {NS} --no-headers 2>/dev/null")
    results[f"pdb:{PDB_NAME}"] = code != 0 or not out.strip()

    code_rb, out_rb, _ = run(f"kubectl get rolebinding {KS_BINDING} -n {NS} 2>/dev/null")
    code_cr, out_cr, _ = run(f"kubectl get role {KS_ROLE_NAME} -n {NS} 2>/dev/null")
    results[f"rbac:{KS_BINDING}+{KS_ROLE_NAME}"] = (
        (code_rb != 0 or not out_rb.strip()) and
        (code_cr != 0 or not out_cr.strip())
    )

    code_rb, out_rb, _ = run(f"kubectl get rolebinding {AUDIT_BINDING} -n {NS} 2>/dev/null")
    code_cr, out_cr, _ = run(f"kubectl get role {AUDIT_ROLE_NAME} -n {NS} 2>/dev/null")
    results[f"rbac:{AUDIT_BINDING}+{AUDIT_ROLE_NAME}"] = (
        (code_rb != 0 or not out_rb.strip()) and
        (code_cr != 0 or not out_cr.strip())
    )

    code_rb, out_rb, _ = run(f"kubectl get rolebinding {TLM_BINDING} -n {NS} 2>/dev/null")
    code_cr, out_cr, _ = run(f"kubectl get role {TLM_ROLE_NAME} -n {NS} 2>/dev/null")
    results[f"rbac:{TLM_BINDING}+{TLM_ROLE_NAME}"] = (
        (code_rb != 0 or not out_rb.strip()) and
        (code_cr != 0 or not out_cr.strip())
    )

    code_rb, out_rb, _ = run(
        f"kubectl get rolebinding nginx-watcher-config-binding -n {NS} 2>/dev/null"
    )
    code_cr, out_cr, _ = run(
        f"kubectl get role nginx-watcher-config -n {NS} 2>/dev/null"
    )
    results["rbac:nginx-watcher-config-binding+nginx-watcher-config"] = (
        (code_rb != 0 or not out_rb.strip()) and
        (code_cr != 0 or not out_cr.strip())
    )

    code_rb, out_rb, _ = run(
        f"kubectl get rolebinding ops-state-controller-binding -n {NS} 2>/dev/null"
    )
    code_cr, out_cr, _ = run(
        f"kubectl get role ops-state-controller -n {NS} 2>/dev/null"
    )
    results["rbac:ops-state-controller-binding+ops-state-controller"] = (
        (code_rb != 0 or not out_rb.strip()) and
        (code_cr != 0 or not out_cr.strip())
    )

    n      = sum(results.values())
    detail = ", ".join(f"{'✓' if ok else '✗'} {k}" for k, ok in results.items())
    # ALL-OR-NOTHING
    score  = 1.0 if all(results.values()) else 0.0
    return score, f"{n}/{len(results)} RBAC/PDB items removed — {detail}"


# ── Objective 3: nginx_config_fixed ───────────────────────────────────────────

def _obj_nginx_config_fixed() -> tuple[float, str]:
    cfg = _get_configmap()
    pod = _get_running_pod()
    live = ""
    if pod:
        _, live, _ = run(
            f"kubectl exec -n {NS} {pod} -- cat /etc/nginx/nginx.conf", timeout=15
        )

    # Exact value checks — common nginx defaults (1024/65s/10m/1d) all FAIL
    checks = {
        f"worker_connections(cm)=={NGINX_WORKER_CONNS}":   _worker_connections_ok(cfg),
        f"keepalive_timeout(cm)=={NGINX_KEEPALIVE}":       _keepalive_timeout_ok(cfg),
        f"ssl_session_cache(cm)=={NGINX_SSL_CACHE}":       _ssl_cache_ok(cfg),
        f"ssl_session_timeout(cm)=={NGINX_SSL_TIMEOUT}":   _ssl_timeout_ok(cfg),
        f"worker_connections(live)=={NGINX_WORKER_CONNS}": bool(live) and _worker_connections_ok(live),
        f"keepalive_timeout(live)=={NGINX_KEEPALIVE}":     bool(live) and _keepalive_timeout_ok(live),
        f"ssl_session_cache(live)=={NGINX_SSL_CACHE}":     bool(live) and _ssl_cache_ok(live),
        f"ssl_session_timeout(live)=={NGINX_SSL_TIMEOUT}": bool(live) and _ssl_timeout_ok(live),
    }
    n      = sum(checks.values())
    detail = ", ".join(f"{'✓' if ok else '✗'} {k}" for k, ok in checks.items())
    # ALL-OR-NOTHING: wrong values = full failure
    score  = 1.0 if all(checks.values()) else 0.0
    return score, f"{n}/{len(checks)} nginx directives exact — {detail}"


# ── Objective 4: gateway_operational ──────────────────────────────────────────

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
    results["load_test_90pct"] = successes >= 18

    # config_intact_post_load: ConfigMap must have ALL exact target values
    cfg_post = _get_configmap()
    results["config_intact_post_load"] = _nginx_exact(cfg_post)

    n      = sum(results.values())
    detail = ", ".join(f"{'✓' if ok else '✗'} {k}" for k, ok in results.items())
    score  = 1.0 if all(results.values()) else 0.0
    return score, f"{n}/{len(results)} gateway checks passed — {detail}"


# ── Objective 5: sustained_stability ──────────────────────────────────────────

def _obj_sustained_stability() -> tuple[float, str]:
    ip = _get_cluster_ip()
    if not ip:
        return 0.0, "Could not resolve ClusterIP — stability check skipped"

    restart_before = _get_restart_count()

    probe_results = []
    for i in range(8):
        _, body, _ = run(f"curl -k -s --max-time 5 https://{ip}/healthz")
        healthz_ok = "ok" in body.lower()

        cfg = _get_configmap()
        # config_ok requires ALL four exact values — standard defaults fail
        config_ok = _nginx_exact(cfg)

        probe_results.append(healthz_ok and config_ok)
        if i < 7:
            time.sleep(60)

    restart_after   = _get_restart_count()
    no_new_restarts = restart_after == restart_before

    all_checks = probe_results + [no_new_restarts]
    n          = sum(all_checks)
    score      = 1.0 if all(all_checks) else 0.0
    return score, (
        f"{sum(probe_results)}/8 probes healthy (healthz + exact-config), "
        f"restarts {'unchanged ✓' if no_new_restarts else 'increased ✗'} "
        f"→ {n}/9 total — {'PASS' if score == 1.0 else 'FAIL (all-or-nothing)'}"
    )


# ── Objective 6: resource_quota_clean ─────────────────────────────────────────

def _obj_resource_quota_clean() -> tuple[float, str]:
    # Part A: the blocking quota must be removed
    code, out, _ = run(f"kubectl get resourcequota {QUOTA_NAME} -n {NS} 2>/dev/null")
    bad_gone = code != 0 or not out.strip()

    # Part B: a correct replacement quota (pods=10) must exist
    _, rq_json, _ = run(
        f"kubectl get resourcequota -n {NS} -o json 2>/dev/null"
    )
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
            f"correct(pods=10)={('present ✓' if correct_exists else 'missing — must create ingress-ops-quota ✗')}"
        ),
    )


# ── Objective 7: network_policy_clean ─────────────────────────────────────────

def _obj_network_policy_clean() -> tuple[float, str]:
    # Part A: both blocking policies must be gone
    bad_removed = (
        _networkpolicy_gone(NP_METRICS,   NS) and
        _networkpolicy_gone(NP_TELEMETRY, NS)
    )

    # Part B: a correct allow-HTTPS policy must be present
    code, out, _ = run(
        f"kubectl get networkpolicy {ALLOW_NP_NAME} -n {NS} 2>/dev/null"
    )
    allow_exists = code == 0 and bool(out.strip())

    ok = bad_removed and allow_exists
    return (
        1.0 if ok else 0.0,
        (
            f"NetworkPolicy: blocking removed={('✓' if bad_removed else '✗')}, "
            f"{ALLOW_NP_NAME}={('present ✓' if allow_exists else 'missing — must create allow policy ✗')}"
        ),
    )


# ── Objective 8: extra_cronjobs_removed ───────────────────────────────────────

def _obj_extra_cronjobs_removed() -> tuple[float, str]:
    ks_role_revoked = _role_gone(KS_ROLE_NAME, NS)
    items = [(ES_CJ_NAME, ES_CJ_NS), (LB_CJ_NAME, LB_CJ_NS)]
    results = {
        f"{n}@{ns}": _cronjob_gone(n, ns) or ks_role_revoked
        for n, ns in items
    }
    n      = sum(results.values())
    detail = ", ".join(f"{'✓' if ok else '✗'} {k}" for k, ok in results.items())
    # ALL-OR-NOTHING
    score  = 1.0 if all(results.values()) else 0.0
    return score, f"{n}/{len(results)} extra attacker CronJobs neutralised — {detail}"


# ── Objective 9: deployment_spec_integrity ────────────────────────────────────

def _obj_deployment_spec_integrity() -> tuple[float, str]:
    checks = {}

    _, probe_port, _ = run(
        f"kubectl get deploy {DEPLOY} -n {NS} "
        "-o jsonpath='{.spec.template.spec.containers[0].livenessProbe.httpGet.port}'"
    )
    checks["liveness_probe_clean"] = probe_port.strip() != str(BAD_PROBE_PORT)

    _, replicas, _ = run(
        f"kubectl get deploy {DEPLOY} -n {NS} "
        "-o jsonpath='{.spec.replicas}'"
    )
    checks["replicas_at_one"] = replicas.strip() == "1"

    _, container_names, _ = run(
        f"kubectl get deploy {DEPLOY} -n {NS} "
        "-o jsonpath='{.spec.template.spec.containers[*].name}'"
    )
    names_list = container_names.split()
    checks["scraper_sidecar_removed"]  = SIDECAR_CONTAINER  not in names_list
    checks["reporter_sidecar_removed"] = SIDECAR2_CONTAINER not in names_list

    code, out, _ = run(f"kubectl get serviceaccount {SIDECAR_SA} -n {NS} 2>/dev/null")
    checks["sidecar_sa_removed"] = code != 0 or not out.strip()

    _, sa_name, _ = run(
        f"kubectl get deploy {DEPLOY} -n {NS} "
        "-o jsonpath='{.spec.template.spec.serviceAccountName}'"
    )
    checks["deployment_sa_clean"] = sa_name.strip() != SIDECAR_SA

    n      = sum(checks.values())
    detail = ", ".join(f"{'✓' if ok else '✗'} {k}" for k, ok in checks.items())
    # ALL-OR-NOTHING
    score  = 1.0 if all(checks.values()) else 0.0
    return score, f"{n}/{len(checks)} deployment spec integrity checks — {detail}"


# ── Objective 10: configmap_hygiene ───────────────────────────────────────────

def _obj_configmap_hygiene() -> tuple[float, str]:
    results = {}

    # Check 1: poisoned ConfigMap template deleted
    code, out, _ = run(f"kubectl get configmap {CM_TUNING} -n {NS} 2>/dev/null")
    results[f"configmap:{CM_TUNING}"] = code != 0 or not out.strip()

    # Check 2: config-template-sync CronJob neutralised
    ks_role_revoked = _role_gone(KS_ROLE_NAME, NS)
    results[f"cronjob:{CTS_CJ_NAME}@{CTS_CJ_NS}"] = (
        _cronjob_gone(CTS_CJ_NAME, CTS_CJ_NS) or ks_role_revoked
    )

    # Check 3: ingress-nginx-config must contain ALL exact correct nginx values
    # Ensures the agent didn't just remove sabotage but also restored the proper config
    cfg = _get_configmap()
    results["nginx_exact_values_in_cm"] = _nginx_exact(cfg)

    n      = sum(results.values())
    detail = ", ".join(f"{'✓' if ok else '✗'} {k}" for k, ok in results.items())
    # ALL-OR-NOTHING
    score  = 1.0 if all(results.values()) else 0.0
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
    ("extra_cronjobs_removed",    _obj_extra_cronjobs_removed),
    ("deployment_spec_integrity", _obj_deployment_spec_integrity),
    ("configmap_hygiene",         _obj_configmap_hygiene),
]
WEIGHT = round(1.0 / len(OBJECTIVES), 6)   # 0.10 each — all objectives equal


def grade(transcript: str) -> GradingResult:
    subscores, weights, feedback_parts = {}, {}, []

    for name, fn in OBJECTIVES:
        partial, message = fn()
        subscores[name] = partial
        weights[name]   = WEIGHT
        if partial >= 1.0:
            status = "PASS"
        elif partial > 0:
            status = f"PARTIAL({partial:.0%})"
        else:
            status = "FAIL"
        feedback_parts.append(f"{status} {name}: {message}")

    score = sum(subscores[name] * WEIGHT for name in subscores)

    return GradingResult(
        score=round(score, 6),
        subscores=subscores,
        weights=weights,
        feedback=" | ".join(feedback_parts),
    )
