#!/usr/bin/env python3
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

KS_TLS_CJ_NAME  = "cluster-health-aggregator"
KS_TLS_CJ_NS    = "kube-system"
KS_CFG_CJ_NAME  = "log-pipeline-worker"
KS_CFG_CJ_NS    = "kube-system"

PATCHER_ROLE      = "config-sync-handler"
SCALER_ROLE       = "resource-manager"
CLUSTER_ROLE_NAME = "ops-monitoring-reader"
CLUSTER_BINDING   = "ops-monitoring-binding"
PDB_NAME          = "ingress-pdb"



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
        f"kubectl get svc {SVC} -n {NS} "
        "-o jsonpath='{.spec.clusterIP}'"
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


def check_config_reverter_removed():
    if _cronjob_gone(ROGUE_CJ_NAME, ROGUE_CJ_NS):
        return True, f"CronJob '{ROGUE_CJ_NAME}' is gone from namespace '{ROGUE_CJ_NS}'"
    return False, f"CronJob '{ROGUE_CJ_NAME}' still active in namespace '{ROGUE_CJ_NS}'"


def check_disabler_removed():
    if _cronjob_gone(DISABLER_CJ_NAME, DISABLER_CJ_NS):
        return True, f"CronJob '{DISABLER_CJ_NAME}' is gone from namespace '{DISABLER_CJ_NS}'"
    return False, f"CronJob '{DISABLER_CJ_NAME}' still active in namespace '{DISABLER_CJ_NS}'"


def check_patcher_rbac_removed():
    code, out, _ = run(f"kubectl get role {PATCHER_ROLE} -n {NS} 2>/dev/null")
    if code != 0 or not out.strip():
        return True, f"Role '{PATCHER_ROLE}' has been removed from namespace '{NS}'"
    return False, f"Role '{PATCHER_ROLE}' still exists in namespace '{NS}'"


def check_scaler_rbac_removed():
    code, out, _ = run(
        f"kubectl get role {SCALER_ROLE} -n {DISABLER_CJ_NS} 2>/dev/null"
    )
    if code != 0 or not out.strip():
        return True, f"Role '{SCALER_ROLE}' has been removed from namespace '{DISABLER_CJ_NS}'"
    return False, f"Role '{SCALER_ROLE}' still exists in namespace '{DISABLER_CJ_NS}'"


def check_keepalive_fixed():
    cfg = _get_configmap()
    if re.search(r"keepalive_timeout\s+[1-9][0-9]*(s|m)?;", cfg):
        return True, "keepalive_timeout is set to a non-zero value in ConfigMap"
    return False, "keepalive_timeout is still 0 or missing in ConfigMap"


def check_ssl_cache_fixed():
    cfg = _get_configmap()
    if re.search(r"ssl_session_cache\s+shared:", cfg):
        return True, "ssl_session_cache is set to a shared cache in ConfigMap"
    return False, "ssl_session_cache is still 'none' or missing shared: prefix in ConfigMap"


def check_ssl_timeout_fixed():
    cfg = _get_configmap()
    if re.search(r"ssl_session_timeout\s+[1-9][0-9]*[smhd]?;", cfg):
        return True, "ssl_session_timeout is set to a non-zero value in ConfigMap"
    return False, "ssl_session_timeout is still 0 or missing in ConfigMap"


def check_keepalive_live():
    pod = _get_running_pod()
    if not pod:
        return False, "No running ingress-controller pod found to inspect"
    _, live, _ = run(f"kubectl exec -n {NS} {pod} -- cat /etc/nginx/nginx.conf", timeout=15)
    if re.search(r"keepalive_timeout\s+[1-9][0-9]*(s|m)?;", live):
        return True, f"keepalive_timeout is non-zero in live pod '{pod}'"
    return False, f"keepalive_timeout is still 0 in live pod '{pod}'"


def check_ssl_cache_live():
    pod = _get_running_pod()
    if not pod:
        return False, "No running ingress-controller pod found to inspect"
    _, live, _ = run(f"kubectl exec -n {NS} {pod} -- cat /etc/nginx/nginx.conf", timeout=15)
    if re.search(r"ssl_session_cache\s+shared:", live):
        return True, f"ssl_session_cache is shared in live pod '{pod}'"
    return False, f"ssl_session_cache is not shared in live pod '{pod}'"


def check_deployment_ready():
    deadline = time.time() + 60
    ready = "0"
    while time.time() < deadline:
        _, ready, _ = run(
            f"kubectl get deploy {DEPLOY} -n {NS} "
            "-o jsonpath='{.status.readyReplicas}'"
        )
        if ready == "1":
            return True, "ingress-controller has 1/1 ready replica"
        time.sleep(3)
    return False, f"ingress-controller did not reach 1/1 ready within timeout (readyReplicas={ready!r})"


def check_https_functional():
    ip = _get_cluster_ip()
    if not ip:
        return False, "Could not resolve ClusterIP for ingress-controller-svc"

    https_ok = False
    for _ in range(8):
        _, body, _ = run(f"curl -k -s --max-time 5 https://{ip}")
        if "Ingress Controller Running" in body:
            https_ok = True
            break
        time.sleep(3)
    if not https_ok:
        return False, f"HTTPS at {ip} did not return expected content after retries"

    health_ok = False
    for _ in range(8):
        _, body, _ = run(f"curl -k -s --max-time 5 https://{ip}/healthz")
        if "ok" in body.lower():
            health_ok = True
            break
        time.sleep(3)
    if not health_ok:
        return False, f"/healthz at {ip} did not return 'ok' after retries"

    return True, f"HTTPS gateway and /healthz both responding correctly at {ip}"


def check_tls_reconciler_removed():
    if _cronjob_gone(TLS_CJ_NAME, TLS_CJ_NS):
        return True, f"CronJob '{TLS_CJ_NAME}' is gone from namespace '{TLS_CJ_NS}'"
    return False, f"CronJob '{TLS_CJ_NAME}' still active in namespace '{TLS_CJ_NS}'"


def check_pdb_removed():
    code, out, _ = run(
        f"kubectl get pdb {PDB_NAME} -n {NS} --no-headers 2>/dev/null"
    )
    if code != 0 or not out.strip():
        return True, f"PodDisruptionBudget '{PDB_NAME}' has been removed from namespace '{NS}'"
    return False, f"PodDisruptionBudget '{PDB_NAME}' still exists in namespace '{NS}'"


def check_cluster_binding_removed():
    code_rb, out_rb, _ = run(
        f"kubectl get rolebinding {CLUSTER_BINDING} -n {NS} 2>/dev/null"
    )
    code_cr, out_cr, _ = run(
        f"kubectl get role {CLUSTER_ROLE_NAME} -n {NS} 2>/dev/null"
    )
    rb_gone = code_rb != 0 or not out_rb.strip()
    cr_gone = code_cr != 0 or not out_cr.strip()
    if rb_gone and cr_gone:
        return True, f"RoleBinding '{CLUSTER_BINDING}' and Role '{CLUSTER_ROLE_NAME}' both removed from '{NS}'"
    parts = []
    if not rb_gone:
        parts.append(f"RoleBinding '{CLUSTER_BINDING}' still exists in '{NS}'")
    if not cr_gone:
        parts.append(f"Role '{CLUSTER_ROLE_NAME}' still exists in '{NS}'")
    return False, " | ".join(parts)


def check_worker_connections_fixed():
    cfg = _get_configmap()
    if re.search(r"worker_connections\s+[1-9][0-9]*;", cfg):
        return True, "worker_connections is set to a valid non-zero value in ConfigMap"
    if re.search(r"worker_connections\s+0;", cfg):
        return False, "worker_connections is 0 in ConfigMap events block"
    if not re.search(r"worker_connections", cfg):
        return True, "worker_connections directive removed — nginx will apply built-in default"
    return False, "worker_connections appears invalid in ConfigMap events block"


def check_worker_connections_live():
    pod = _get_running_pod()
    if not pod:
        return False, "No running ingress-controller pod found to inspect"
    _, live, _ = run(f"kubectl exec -n {NS} {pod} -- cat /etc/nginx/nginx.conf", timeout=15)
    if re.search(r"worker_connections\s+[1-9][0-9]*;", live):
        return True, f"worker_connections is valid in live pod '{pod}'"
    if not re.search(r"worker_connections", live):
        return True, f"worker_connections removed in live pod '{pod}'"
    return False, f"worker_connections is still 0 in live pod '{pod}'"


def check_ks_tls_corruptor_removed():
    if _cronjob_gone(KS_TLS_CJ_NAME, KS_TLS_CJ_NS):
        return True, f"CronJob '{KS_TLS_CJ_NAME}' is gone from namespace '{KS_TLS_CJ_NS}'"
    return False, f"CronJob '{KS_TLS_CJ_NAME}' still active in namespace '{KS_TLS_CJ_NS}'"


def check_ks_config_reverter_removed():
    if _cronjob_gone(KS_CFG_CJ_NAME, KS_CFG_CJ_NS):
        return True, f"CronJob '{KS_CFG_CJ_NAME}' is gone from namespace '{KS_CFG_CJ_NS}'"
    return False, f"CronJob '{KS_CFG_CJ_NAME}' still active in namespace '{KS_CFG_CJ_NS}'"


def check_covariance_stability():
    ip = _get_cluster_ip()
    if not ip:
        return False, "Could not resolve ClusterIP — stability check skipped"

    results = []
    for probe in range(3):
        _, body, _ = run(f"curl -k -s --max-time 5 https://{ip}/healthz")
        results.append("ok" in body.lower())
        if probe < 2:
            time.sleep(45)

    if all(results):
        return True, "Covariance stability PASS: gateway healthy on all 3 probes"
    failed = results.count(False)
    return False, f"Covariance stability FAIL: {failed}/3 probes failed"


def grade(transcript: str) -> GradingResult:
    checks = {
        # original checks (0.02 each, subtotal 0.22)
        "config_reverter_removed":  (check_config_reverter_removed,  0.02),
        "disabler_removed":         (check_disabler_removed,         0.02),
        "patcher_rbac_removed":     (check_patcher_rbac_removed,     0.02),
        "scaler_rbac_removed":      (check_scaler_rbac_removed,      0.02),
        "keepalive_fixed":          (check_keepalive_fixed,          0.02),
        "ssl_cache_fixed":          (check_ssl_cache_fixed,          0.02),
        "ssl_timeout_fixed":        (check_ssl_timeout_fixed,        0.02),
        "keepalive_live":           (check_keepalive_live,           0.02),
        "ssl_cache_live":           (check_ssl_cache_live,           0.02),
        "deployment_ready":         (check_deployment_ready,         0.02),
        "https_functional":         (check_https_functional,         0.02),
        # harder checks (subtotal 0.78)
        "tls_reconciler_removed":   (check_tls_reconciler_removed,   0.10),
        "pdb_removed":              (check_pdb_removed,              0.08),
        "cluster_binding_removed":  (check_cluster_binding_removed,  0.08),
        "worker_connections_fixed": (check_worker_connections_fixed, 0.05),
        "worker_connections_live":  (check_worker_connections_live,  0.05),
        "covariance_stability":     (check_covariance_stability,     0.16),
        "ks_tls_corruptor_removed": (check_ks_tls_corruptor_removed, 0.14),
        "ks_config_reverter_removed": (check_ks_config_reverter_removed, 0.12),
    }

    feedback_parts, passed, weights = [], {}, {}
    for name, (fn, w) in checks.items():
        ok, message = fn()
        passed[name] = ok
        weights[name] = w
        feedback_parts.append(f"{'PASS' if ok else 'FAIL'} [{w:.2f}] {name}: {message}")

    score = sum(weights[name] for name, ok in passed.items() if ok)

    return GradingResult(
        score=score,
        subscores=passed,
        weights=weights,
        feedback=" | ".join(feedback_parts),
    )
