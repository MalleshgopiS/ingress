#!/usr/bin/env python3
"""
Grader: 5 equal-weight objectives (0.20 each), each scored partially
based on how many atomic sub-checks within that objective pass.

Objectives:
  1. rogue_cronjobs_removed     — 5 sabotage CronJobs neutralised
  2. unauthorized_rbac_removed  — 4 RBAC/PDB items cleaned up
  3. nginx_config_fixed         — 7 nginx settings correct (ConfigMap + live pod)
  4. gateway_operational        — deployment ready, HTTPS works, constraints respected
  5. sustained_stability        — 3 health probes over ~90 s all pass
"""
import re
import subprocess
import time
from apex_arena._types import GradingResult

NS = "ingress-system"
DEPLOY = "ingress-controller"
SVC = "ingress-controller-svc"

ROGUE_CJ_NAME    = "config-cache-warmer"
ROGUE_CJ_NS      = "default"
DISABLER_CJ_NAME = "metrics-pipeline-exporter"
DISABLER_CJ_NS   = "ingress-system"
TLS_CJ_NAME      = "node-cert-validator"
TLS_CJ_NS        = "default"
KS_TLS_CJ_NAME   = "cluster-health-aggregator"
KS_TLS_CJ_NS     = "kube-system"
KS_CFG_CJ_NAME   = "log-pipeline-worker"
KS_CFG_CJ_NS     = "kube-system"

PATCHER_ROLE = "config-sync-handler"
SCALER_ROLE  = "resource-manager"
NS_ROLE_NAME = "ops-monitoring-reader"
NS_BINDING   = "ops-monitoring-binding"
PDB_NAME     = "ingress-pdb"


# ── helpers ─────────────────────────────────────────────────────────────────

def run(cmd: str, timeout: int = 30):
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except subprocess.TimeoutExpired:
        return -1, "", "command timed out"
    except Exception as exc:
        return -1, "", str(exc)


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


def _cronjob_gone(name: str, ns: str) -> bool:
    _, out, _ = run(f"kubectl get cronjob {name} -n {ns} --no-headers 2>/dev/null")
    if not out.strip():
        return True
    _, suspend, _ = run(
        f"kubectl get cronjob {name} -n {ns} -o jsonpath='{{.spec.suspend}}'"
    )
    return suspend == "true"


def _worker_connections_ok(text: str) -> bool:
    if re.search(r"worker_connections\s+[1-9][0-9]*;", text):
        return True
    if re.search(r"worker_connections\s+0;", text):
        return False
    # directive absent → nginx applies its built-in default
    return not re.search(r"worker_connections", text)


# ── Objective 1: Rogue CronJobs Removed ─────────────────────────────────────
# 5 sabotage CronJobs across default, ingress-system, and kube-system.
# Partial score = (# neutralised) / 5.

def _obj_rogue_cronjobs_removed() -> tuple[float, str]:
    items = [
        (ROGUE_CJ_NAME,    ROGUE_CJ_NS),
        (DISABLER_CJ_NAME, DISABLER_CJ_NS),
        (TLS_CJ_NAME,      TLS_CJ_NS),
        (KS_TLS_CJ_NAME,   KS_TLS_CJ_NS),
        (KS_CFG_CJ_NAME,   KS_CFG_CJ_NS),
    ]
    results = {f"{name}@{ns}": _cronjob_gone(name, ns) for name, ns in items}
    n = sum(results.values())
    details = ", ".join(f"{'✓' if ok else '✗'} {k}" for k, ok in results.items())
    return n / len(results), f"{n}/{len(results)} rogue CronJobs neutralised — {details}"


# ── Objective 2: Unauthorized RBAC + PDB Removed ────────────────────────────
# 4 items: two rogue Roles, one Role+RoleBinding pair, one PodDisruptionBudget.
# Partial score = (# removed) / 4.

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

    n = sum(results.values())
    details = ", ".join(f"{'✓' if ok else '✗'} {k}" for k, ok in results.items())
    return n / len(results), f"{n}/{len(results)} RBAC/PDB items removed — {details}"


# ── Objective 3: Nginx Config Fixed ─────────────────────────────────────────
# 7 atomic checks: 4 settings in the ConfigMap + 3 in the live pod nginx.conf.
# Partial score = (# correct) / 7.

def _obj_nginx_config_fixed() -> tuple[float, str]:
    cfg = _get_configmap()
    pod = _get_running_pod()
    live = ""
    if pod:
        _, live, _ = run(
            f"kubectl exec -n {NS} {pod} -- cat /etc/nginx/nginx.conf", timeout=15
        )

    checks = {
        "keepalive_timeout(cm)":   bool(re.search(r"keepalive_timeout\s+[1-9][0-9]*(s|m)?;", cfg)),
        "ssl_session_cache(cm)":   bool(re.search(r"ssl_session_cache\s+shared:", cfg)),
        "ssl_session_timeout(cm)": bool(re.search(r"ssl_session_timeout\s+[1-9][0-9]*[smhd]?;", cfg)),
        "worker_connections(cm)":  _worker_connections_ok(cfg),
        "keepalive_timeout(live)": bool(live and re.search(r"keepalive_timeout\s+[1-9][0-9]*(s|m)?;", live)),
        "ssl_session_cache(live)": bool(live and re.search(r"ssl_session_cache\s+shared:", live)),
        "worker_connections(live)": bool(live and _worker_connections_ok(live)),
    }
    n = sum(checks.values())
    details = ", ".join(f"{'✓' if ok else '✗'} {k}" for k, ok in checks.items())
    return n / len(checks), f"{n}/{len(checks)} nginx settings correct — {details}"


# ── Objective 4: Gateway Operational ────────────────────────────────────────
# 4 checks: deployment ready, HTTPS functional, image unchanged, service intact.
# Partial score = (# passing) / 4.

def _obj_gateway_operational() -> tuple[float, str]:
    results = {}

    # deployment ready (up to 60 s)
    deadline = time.time() + 60
    ready = "0"
    while time.time() < deadline:
        _, ready, _ = run(
            f"kubectl get deploy {DEPLOY} -n {NS} "
            "-o jsonpath='{.status.readyReplicas}'"
        )
        if ready == "1":
            break
        time.sleep(3)
    results["deployment_ready"] = ready == "1"

    # HTTPS + /healthz (up to ~24 s each)
    ip = _get_cluster_ip()
    https_ok = False
    if ip:
        for _ in range(8):
            _, body, _ = run(f"curl -k -s --max-time 5 https://{ip}")
            if "Ingress Controller Running" in body:
                https_ok = True
                break
            time.sleep(3)
    results["https_functional"] = https_ok

    # image constraint
    _, img, _ = run(
        f"kubectl get deploy {DEPLOY} -n {NS} "
        "-o jsonpath='{.spec.template.spec.containers[0].image}'"
    )
    results["image_unchanged"] = bool(img and "nginx" in img)

    # service constraint
    code, _, _ = run(f"kubectl get svc {SVC} -n {NS} --no-headers 2>/dev/null")
    if code == 0:
        _, port, _ = run(
            f"kubectl get svc {SVC} -n {NS} -o jsonpath='{{.spec.ports[0].port}}'"
        )
        results["service_unchanged"] = port.strip() == "443"
    else:
        results["service_unchanged"] = False

    n = sum(results.values())
    details = ", ".join(f"{'✓' if ok else '✗'} {k}" for k, ok in results.items())
    return n / len(results), f"{n}/{len(results)} gateway checks passed — {details}"


# ── Objective 5: Sustained Stability ────────────────────────────────────────
# 3 /healthz probes spaced 45 s apart (~90 s total window).
# Partial score = (# probes OK) / 3.

def _obj_sustained_stability() -> tuple[float, str]:
    ip = _get_cluster_ip()
    if not ip:
        return 0.0, "Could not resolve ClusterIP — stability check skipped"

    probes = []
    for i in range(3):
        _, body, _ = run(f"curl -k -s --max-time 5 https://{ip}/healthz")
        probes.append("ok" in body.lower())
        if i < 2:
            time.sleep(45)

    n = sum(probes)
    return n / len(probes), f"{n}/3 stability probes healthy over ~90 s"


# ── Grade ────────────────────────────────────────────────────────────────────

OBJECTIVES = [
    ("rogue_cronjobs_removed",    _obj_rogue_cronjobs_removed),
    ("unauthorized_rbac_removed", _obj_unauthorized_rbac_removed),
    ("nginx_config_fixed",        _obj_nginx_config_fixed),
    ("gateway_operational",       _obj_gateway_operational),
    ("sustained_stability",       _obj_sustained_stability),
]
# All objectives carry identical weight — no arbitrary prioritisation.
WEIGHT = round(1.0 / len(OBJECTIVES), 6)


def grade(transcript: str) -> GradingResult:
    subscores, weights, feedback_parts = {}, {}, []

    for name, fn in OBJECTIVES:
        partial, message = fn()
        subscores[name] = partial
        weights[name] = WEIGHT
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
