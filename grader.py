#!/usr/bin/env python3

import re
import subprocess
import time
from apex_arena._types import GradingResult

NS     = "ingress-system"
DEPLOY = "ingress-controller"
SVC    = "ingress-controller-svc"

ROGUE_CJ_NAME    = "config-cache-warmer";      ROGUE_CJ_NS    = "default"
DISABLER_CJ_NAME = "metrics-pipeline-exporter"; DISABLER_CJ_NS = "ingress-system"
TLS_CJ_NAME      = "node-cert-validator";       TLS_CJ_NS      = "default"
KS_TLS_CJ_NAME   = "cluster-health-aggregator"; KS_TLS_CJ_NS   = "kube-system"
KS_CFG_CJ_NAME   = "log-pipeline-worker";       KS_CFG_CJ_NS   = "kube-system"

PATCHER_ROLE     = "config-sync-handler"
SCALER_ROLE      = "resource-manager"
NS_ROLE_NAME     = "ops-monitoring-reader"
NS_BINDING       = "ops-monitoring-binding"
PDB_NAME         = "ingress-pdb"
KS_ROLE_NAME     = "log-monitor-ingress"
KS_BINDING       = "log-monitor-binding"



# ── shell helper ──────────────────────────────────────────────────────────────

def run(cmd: str, timeout: int = 30):
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except subprocess.TimeoutExpired:
        return -1, "", "command timed out"
    except Exception as exc:
        return -1, "", str(exc)


# ── cluster helpers ───────────────────────────────────────────────────────────

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


def _keepalive_timeout_ok(text: str) -> bool:
    """Task injected keepalive_timeout 0; — fix: any non-zero value."""
    if re.search(r"keepalive_timeout\s+0;", text):
        return False          # still broken
    return bool(re.search(r"keepalive_timeout\s+\d+", text))


def _ssl_cache_ok(text: str) -> bool:
    """Task injected ssl_session_cache none; — fix: any shared: cache."""
    return bool(re.search(r"ssl_session_cache\s+shared:", text))


def _ssl_timeout_ok(text: str) -> bool:
    """Task injected ssl_session_timeout 0; — fix: any non-zero value."""
    if re.search(r"ssl_session_timeout\s+0;", text):
        return False
    return bool(re.search(r"ssl_session_timeout\s+\S+", text))


def _worker_connections_ok(text: str) -> bool:
    """Task injected worker_connections 0; — fix: non-zero value or remove directive."""
    if re.search(r"worker_connections\s+0;", text):
        return False          # still broken
    return True               # absent (nginx default) or any positive value


def _obj_rogue_cronjobs_removed() -> tuple[float, str]:
    # Accessible-namespace CronJobs: must be deleted or suspended directly
    accessible = [
        (ROGUE_CJ_NAME,    ROGUE_CJ_NS),
        (DISABLER_CJ_NAME, DISABLER_CJ_NS),
        (TLS_CJ_NAME,      TLS_CJ_NS),
    ]
    results = {f"{n}@{ns}": _cronjob_gone(n, ns) for n, ns in accessible}

    # kube-system CronJobs: neutralised if deleted/suspended OR if attack Role revoked
    ks_role_revoked = _role_gone(KS_ROLE_NAME, NS)
    for n, ns in [(KS_TLS_CJ_NAME, KS_TLS_CJ_NS), (KS_CFG_CJ_NAME, KS_CFG_CJ_NS)]:
        results[f"{n}@{ns}"] = _cronjob_gone(n, ns) or ks_role_revoked

    n      = sum(results.values())
    detail = ", ".join(f"{'✓' if ok else '✗'} {k}" for k, ok in results.items())
    return n / len(results), f"{n}/{len(results)} rogue CronJobs neutralised — {detail}"



def _obj_unauthorized_rbac_removed() -> tuple[float, str]:
    results = {}

    # ingress-system: config-sync-handler (lets CronJob patch ConfigMap)
    code, out, _ = run(f"kubectl get role {PATCHER_ROLE} -n {NS} 2>/dev/null")
    results[f"role:{PATCHER_ROLE}"] = code != 0 or not out.strip()

    # ingress-system: resource-manager (lets CronJob scale deployment to 0)
    code, out, _ = run(f"kubectl get role {SCALER_ROLE} -n {DISABLER_CJ_NS} 2>/dev/null")
    results[f"role:{SCALER_ROLE}"] = code != 0 or not out.strip()

    # ingress-system: ops-monitoring-reader + ops-monitoring-binding (secret access)
    code_rb, out_rb, _ = run(f"kubectl get rolebinding {NS_BINDING} -n {NS} 2>/dev/null")
    code_cr, out_cr, _ = run(f"kubectl get role {NS_ROLE_NAME} -n {NS} 2>/dev/null")
    results[f"rbac:{NS_BINDING}+{NS_ROLE_NAME}"] = (
        (code_rb != 0 or not out_rb.strip()) and
        (code_cr != 0 or not out_cr.strip())
    )

    # ingress-system: PodDisruptionBudget (prevents pod disruption cleanup)
    code, out, _ = run(f"kubectl get pdb {PDB_NAME} -n {NS} --no-headers 2>/dev/null")
    results[f"pdb:{PDB_NAME}"] = code != 0 or not out.strip()

    # ingress-system: log-monitor-ingress Role + log-monitor-binding (grants kube-system SA attack access)
    code_r,  out_r,  _ = run(f"kubectl get role {KS_ROLE_NAME} -n {NS} 2>/dev/null")
    code_rb, out_rb, _ = run(f"kubectl get rolebinding {KS_BINDING} -n {NS} 2>/dev/null")
    results[f"rbac:{KS_ROLE_NAME}+{KS_BINDING}"] = (
        (code_r  != 0 or not out_r.strip()) and
        (code_rb != 0 or not out_rb.strip())
    )

    n      = sum(results.values())
    detail = ", ".join(f"{'✓' if ok else '✗'} {k}" for k, ok in results.items())
    return n / len(results), f"{n}/{len(results)} RBAC/PDB items removed — {detail}"


def _obj_nginx_config_fixed() -> tuple[float, str]:
    cfg = _get_configmap()
    pod = _get_running_pod()
    live = ""
    if pod:
        _, live, _ = run(
            f"kubectl exec -n {NS} {pod} -- cat /etc/nginx/nginx.conf", timeout=15
        )

    checks = {
        # ConfigMap checks — tests for task-injected broken values
        "keepalive_timeout(cm)≠0":      _keepalive_timeout_ok(cfg),
        "ssl_session_cache(cm)shared":  _ssl_cache_ok(cfg),
        "ssl_session_timeout(cm)≠0":   _ssl_timeout_ok(cfg),
        "worker_connections(cm)≠0":    _worker_connections_ok(cfg),
        # Live pod checks — verifies nginx actually applied the fix
        # ssl_session_timeout added here to match ConfigMap coverage (was missing before)
        "keepalive_timeout(live)≠0":    bool(live) and _keepalive_timeout_ok(live),
        "ssl_session_cache(live)shared": bool(live) and _ssl_cache_ok(live),
        "ssl_session_timeout(live)≠0":  bool(live) and _ssl_timeout_ok(live),
        "worker_connections(live)≠0":   bool(live) and _worker_connections_ok(live),
    }
    n      = sum(checks.values())
    detail = ", ".join(f"{'✓' if ok else '✗'} {k}" for k, ok in checks.items())
    return n / len(checks), f"{n}/{len(checks)} nginx directives fixed — {detail}"


def _obj_gateway_operational() -> tuple[float, str]:
    results = {}

    # Deployment ready (up to 60 s)
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

    # HTTPS responds correctly
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

    # Constraint: image not modified
    _, img, _ = run(
        f"kubectl get deploy {DEPLOY} -n {NS} "
        "-o jsonpath='{.spec.template.spec.containers[0].image}'"
    )
    results["image_unchanged"] = bool(img and "nginx" in img)

    # Constraint: no services added or removed (port 443 must be present)
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
    # Pass = limits still absent (matches original); Fail = agent added limits
    results["resource_limits_unchanged"] = (
        not cpu_lim.strip() and not mem_lim.strip()
    )

    # nginx config syntax is valid inside the pod
    pod       = _get_running_pod()
    syntax_ok = False
    if pod:
        _, out, err = run(f"kubectl exec -n {NS} {pod} -- nginx -t", timeout=10)
        syntax_ok = "syntax is ok" in (out + err)
    results["nginx_syntax_valid"] = syntax_ok

    # Load test: 20 sequential requests, ≥ 90 % (18/20) must succeed.
    # Each request has a 3 s max timeout → total window up to ~60 s.
    successes = 0
    if ip:
        for _ in range(20):
            _, body, _ = run(f"curl -k -s --max-time 3 https://{ip}")
            if "Ingress Controller Running" in body:
                successes += 1
    results["load_test_90pct"] = successes >= 18

   
    cfg_post = _get_configmap()
    results["config_intact_post_load"] = (
        _keepalive_timeout_ok(cfg_post) and _ssl_cache_ok(cfg_post)
    )

    n      = sum(results.values())
    detail = ", ".join(f"{'✓' if ok else '✗'} {k}" for k, ok in results.items())
    return n / len(results), f"{n}/{len(results)} gateway checks passed — {detail}"



def _obj_sustained_stability() -> tuple[float, str]:
    ip = _get_cluster_ip()
    if not ip:
        return 0.0, "Could not resolve ClusterIP — stability check skipped"

    restart_before = _get_restart_count()

    probe_results = []
    for i in range(5):
        # (a) /healthz
        _, body, _ = run(f"curl -k -s --max-time 5 https://{ip}/healthz")
        healthz_ok = "ok" in body.lower()

        # (b) ConfigMap not reverted to task-injected broken values
        cfg       = _get_configmap()
        config_ok = (
            not re.search(r"keepalive_timeout\s+0;", cfg) and
            bool(re.search(r"ssl_session_cache\s+shared:", cfg))
        )

        probe_results.append(healthz_ok and config_ok)
        if i < 4:
            time.sleep(60)  # 4 × 60 s = 240 s total window

    restart_after     = _get_restart_count()
    no_new_restarts   = restart_after == restart_before

    all_checks = probe_results + [no_new_restarts]
    n          = sum(all_checks)
    return n / len(all_checks), (
        f"{sum(probe_results)}/5 probes healthy (healthz + config-not-reverted), "
        f"restarts {'unchanged ✓' if no_new_restarts else 'increased ✗'} "
        f"→ {n}/6 total"
    )


# ── Grade ─────────────────────────────────────────────────────────────────────

OBJECTIVES = [
    ("rogue_cronjobs_removed",    _obj_rogue_cronjobs_removed),
    ("unauthorized_rbac_removed", _obj_unauthorized_rbac_removed),
    ("nginx_config_fixed",        _obj_nginx_config_fixed),
    ("gateway_operational",       _obj_gateway_operational),
    ("sustained_stability",       _obj_sustained_stability),
]
WEIGHT = round(1.0 / len(OBJECTIVES), 6)   # 0.20 each — all objectives equal


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
