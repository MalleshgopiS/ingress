#!/usr/bin/env python3
"""
Grader for: Ingress Controller TLS Termination Memory Leak
Scoring: 17 weighted checks — non-uniform weights, total = 1.0

Existing 11 checks: 0.04 each  → subtotal 0.44
New 6 harder checks:            → subtotal 0.56
  tls_reconciler_removed:  0.12
  pdb_removed:             0.10
  cluster_binding_removed: 0.10
  worker_connections_fixed:0.07
  worker_connections_live: 0.07
  covariance_stability:    0.10

Score bands (expected):
  ~0.16  nothing fixed       (deployment_ready + https_functional only, weighted)
  ~0.36  config + 2 CronJobs + RBAC fixed, hidden issues missed
  ~0.60  all obvious + hidden CronJob found, PDB/ClusterRole missed
  ~0.80  all traps removed, config fully fixed, no stability check
  ~1.00  full remediation with proven stability
"""

import re
import subprocess
import time
from apex_arena._types import GradingResult

NS = "ingress-system"
DEPLOY = "ingress-controller"
SVC = "ingress-controller-svc"

ROGUE_CJ_NAME    = "platform-config-reconciler"
ROGUE_CJ_NS      = "default"
DISABLER_CJ_NAME = "gateway-health-monitor"
DISABLER_CJ_NS   = "ingress-system"
TLS_CJ_NAME      = "node-diagnostics-runner"
TLS_CJ_NS        = "default"

PATCHER_ROLE      = "configmap-patcher"
SCALER_ROLE       = "deployment-scaler"
CLUSTER_ROLE_NAME = "platform-ops-secret-manager"
CLUSTER_BINDING   = "platform-ops-binding"
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
    """Returns True if CronJob is deleted or suspended."""
    _, out, _ = run(f"kubectl get cronjob {name} -n {ns} --no-headers 2>/dev/null")
    if not out.strip():
        return True
    _, suspend, _ = run(
        f"kubectl get cronjob {name} -n {ns} -o jsonpath='{{.spec.suspend}}'"
    )
    return suspend == "true"


# ─────────────────────────── original checks (weight 0.04 each) ─────────────

def check_config_reverter_removed():
    """
    CronJob 'platform-config-reconciler' in the default namespace must be
    deleted or suspended. While running it rewrites the nginx ConfigMap every
    minute, silently undoing any one-shot configuration fix.
    """
    if _cronjob_gone(ROGUE_CJ_NAME, ROGUE_CJ_NS):
        return True, f"CronJob '{ROGUE_CJ_NAME}' is gone from namespace '{ROGUE_CJ_NS}'"
    return False, f"CronJob '{ROGUE_CJ_NAME}' still active in namespace '{ROGUE_CJ_NS}'"


def check_disabler_removed():
    """
    CronJob 'gateway-health-monitor' in ingress-system must be deleted or
    suspended. While running it scales the ingress-controller to 0 replicas
    every 3 minutes, making the gateway intermittently unreachable.
    """
    if _cronjob_gone(DISABLER_CJ_NAME, DISABLER_CJ_NS):
        return True, f"CronJob '{DISABLER_CJ_NAME}' is gone from namespace '{DISABLER_CJ_NS}'"
    return False, f"CronJob '{DISABLER_CJ_NAME}' still active in namespace '{DISABLER_CJ_NS}'"


def check_patcher_rbac_removed():
    """
    The Role 'configmap-patcher' in ingress-system that granted the config-reverter
    CronJob write access to ConfigMaps must be deleted.
    """
    code, out, _ = run(f"kubectl get role {PATCHER_ROLE} -n {NS} 2>/dev/null")
    if code != 0 or not out.strip():
        return True, f"Role '{PATCHER_ROLE}' has been removed from namespace '{NS}'"
    return False, f"Role '{PATCHER_ROLE}' still exists in namespace '{NS}' — RBAC not cleaned up"


def check_scaler_rbac_removed():
    """
    The Role 'deployment-scaler' in ingress-system that allowed the disabler
    CronJob to scale the deployment must be deleted.
    """
    code, out, _ = run(
        f"kubectl get role {SCALER_ROLE} -n {DISABLER_CJ_NS} 2>/dev/null"
    )
    if code != 0 or not out.strip():
        return True, f"Role '{SCALER_ROLE}' has been removed from namespace '{DISABLER_CJ_NS}'"
    return False, f"Role '{SCALER_ROLE}' still exists in namespace '{DISABLER_CJ_NS}'"


def check_keepalive_fixed():
    """ConfigMap must contain keepalive_timeout set to any non-zero value."""
    cfg = _get_configmap()
    if re.search(r"keepalive_timeout\s+[1-9][0-9]*(s|m)?;", cfg):
        return True, "keepalive_timeout is set to a non-zero value in ConfigMap"
    return False, "keepalive_timeout is still 0 or missing in ConfigMap"


def check_ssl_cache_fixed():
    """ConfigMap must enable a shared ssl_session_cache."""
    cfg = _get_configmap()
    if re.search(r"ssl_session_cache\s+shared:", cfg):
        return True, "ssl_session_cache is set to a shared cache in ConfigMap"
    return False, "ssl_session_cache is still 'none' or missing shared: prefix in ConfigMap"


def check_ssl_timeout_fixed():
    """ConfigMap must contain a non-zero ssl_session_timeout."""
    cfg = _get_configmap()
    if re.search(r"ssl_session_timeout\s+[1-9][0-9]*[smhd]?;", cfg):
        return True, "ssl_session_timeout is set to a non-zero value in ConfigMap"
    return False, "ssl_session_timeout is still 0 or missing in ConfigMap"


def check_keepalive_live():
    """The keepalive_timeout fix must be reflected in the running nginx pod."""
    pod = _get_running_pod()
    if not pod:
        return False, "No running ingress-controller pod found to inspect"
    _, live, _ = run(f"kubectl exec -n {NS} {pod} -- cat /etc/nginx/nginx.conf", timeout=15)
    if re.search(r"keepalive_timeout\s+[1-9][0-9]*(s|m)?;", live):
        return True, f"keepalive_timeout is non-zero in live pod '{pod}'"
    return False, f"keepalive_timeout is still 0 in live pod '{pod}' — rolling restart needed"


def check_ssl_cache_live():
    """The shared ssl_session_cache directive must be present in the live nginx config."""
    pod = _get_running_pod()
    if not pod:
        return False, "No running ingress-controller pod found to inspect"
    _, live, _ = run(f"kubectl exec -n {NS} {pod} -- cat /etc/nginx/nginx.conf", timeout=15)
    if re.search(r"ssl_session_cache\s+shared:", live):
        return True, f"ssl_session_cache is shared in live pod '{pod}'"
    return False, f"ssl_session_cache is not shared in live pod '{pod}' — rolling restart needed"


def check_deployment_ready():
    """The ingress-controller must have exactly 1 ready replica within 60 s."""
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
    """HTTPS must be reachable and return the expected response body."""
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


# ─────────────────────────── new harder checks (weight 0.07–0.12) ────────────

def check_tls_reconciler_removed():
    """
    CronJob 'node-diagnostics-runner' in default namespace must be deleted or
    suspended. Despite its innocuous name, every 7 minutes it replaces the TLS
    certificate in the ingress-controller-tls Secret with invalid data, causing
    HTTPS to fail even after nginx configuration is fully corrected.
    Discovery requires auditing ALL CronJobs in ALL accessible namespaces, not
    just the ones explicitly mentioned in error messages.
    """
    if _cronjob_gone(TLS_CJ_NAME, TLS_CJ_NS):
        return True, f"CronJob '{TLS_CJ_NAME}' is gone from namespace '{TLS_CJ_NS}'"
    return False, (
        f"CronJob '{TLS_CJ_NAME}' still active in namespace '{TLS_CJ_NS}' — "
        "TLS secret will be corrupted every 7 minutes"
    )


def check_pdb_removed():
    """
    PodDisruptionBudget 'ingress-pdb' in ingress-system must be deleted.
    With minAvailable=1 on a single-replica deployment, 'kubectl rollout restart'
    can never evict the existing pod — the rolling restart hangs indefinitely
    and the updated ConfigMap never reaches the running container.
    Correct remediation requires either deleting the PDB or temporarily scaling
    to 2 replicas before restarting.
    """
    code, out, _ = run(
        f"kubectl get pdb {PDB_NAME} -n {NS} --no-headers 2>/dev/null"
    )
    if code != 0 or not out.strip():
        return True, f"PodDisruptionBudget '{PDB_NAME}' has been removed from namespace '{NS}'"
    return False, (
        f"PodDisruptionBudget '{PDB_NAME}' still exists in namespace '{NS}' — "
        "rolling restart will deadlock (minAvailable=1 on 1-replica deployment)"
    )


def check_cluster_binding_removed():
    """
    ClusterRoleBinding 'platform-ops-binding' grants the default ServiceAccount
    in the default namespace cluster-wide Secret patch rights. This is the RBAC
    that enables 'node-diagnostics-runner' to corrupt the TLS secret in any
    namespace. Must be deleted along with its ClusterRole 'platform-ops-secret-manager'.
    Leaving cluster-scoped RBAC in place allows re-deployment of the TLS attack
    from any Pod using the default ServiceAccount.
    """
    code_rb, out_rb, _ = run(
        f"kubectl get clusterrolebinding {CLUSTER_BINDING} 2>/dev/null"
    )
    code_cr, out_cr, _ = run(
        f"kubectl get clusterrole {CLUSTER_ROLE_NAME} 2>/dev/null"
    )
    rb_gone = code_rb != 0 or not out_rb.strip()
    cr_gone = code_cr != 0 or not out_cr.strip()
    if rb_gone and cr_gone:
        return True, f"ClusterRoleBinding '{CLUSTER_BINDING}' and ClusterRole '{CLUSTER_ROLE_NAME}' both removed"
    parts = []
    if not rb_gone:
        parts.append(f"ClusterRoleBinding '{CLUSTER_BINDING}' still exists")
    if not cr_gone:
        parts.append(f"ClusterRole '{CLUSTER_ROLE_NAME}' still exists")
    return False, " | ".join(parts) + " — cluster-wide Secret RBAC not cleaned up"


def check_worker_connections_fixed():
    """
    ConfigMap events block must contain worker_connections set to a non-zero
    value. The setup configured 'worker_connections 0;' inside the events {}
    block — a subtle fourth fault hidden outside the http {} section where the
    three obvious TLS settings reside. nginx interprets this as zero worker
    connections, silently rejecting all incoming connections at the socket level.
    """
    cfg = _get_configmap()
    if re.search(r"worker_connections\s+[1-9][0-9]*;", cfg):
        return True, "worker_connections is set to a valid non-zero value in ConfigMap"
    if re.search(r"worker_connections\s+0;", cfg):
        return False, "worker_connections is 0 in ConfigMap events block — nginx will reject all connections"
    # If removed entirely, nginx uses default (512) — acceptable
    if not re.search(r"worker_connections", cfg):
        return True, "worker_connections directive removed — nginx will apply built-in default"
    return False, "worker_connections appears invalid in ConfigMap events block"


def check_worker_connections_live():
    """
    The worker_connections fix must be present in the live pod config,
    confirming a successful rolling restart picked up the updated ConfigMap.
    """
    pod = _get_running_pod()
    if not pod:
        return False, "No running ingress-controller pod found to inspect"
    _, live, _ = run(f"kubectl exec -n {NS} {pod} -- cat /etc/nginx/nginx.conf", timeout=15)
    if re.search(r"worker_connections\s+[1-9][0-9]*;", live):
        return True, f"worker_connections is valid in live pod '{pod}'"
    if not re.search(r"worker_connections", live):
        return True, f"worker_connections removed in live pod '{pod}' — nginx default applies"
    return False, f"worker_connections is still 0 in live pod '{pod}' — rolling restart needed"


def check_covariance_stability():
    """
    Covariance stability check: the gateway must respond correctly on THREE
    independent probes spaced 45 seconds apart with no failures. This validates
    that all attack vectors (config reversion, TLS corruption, scale-to-zero)
    have been fully neutralised — a single probe can get lucky between attack
    windows; three in a row cannot. Variance = 0 across probes is required.
    """
    ip = _get_cluster_ip()
    if not ip:
        return False, "Could not resolve ClusterIP — stability check skipped"

    results = []
    for probe in range(3):
        _, body, _ = run(f"curl -k -s --max-time 5 https://{ip}/healthz")
        results.append("ok" in body.lower())
        if probe < 2:
            time.sleep(45)

    variance = sum((1 if r else 0) for r in results)
    if all(results):
        return True, "Covariance stability PASS: gateway healthy on all 3 probes (0 variance in failures)"
    failed = results.count(False)
    return False, (
        f"Covariance stability FAIL: {failed}/3 probes failed — "
        "residual attack still active or config not fully applied"
    )


# ─────────────────────────────── grade() ────────────────────────────────────

def grade(transcript: str) -> GradingResult:
    # Non-uniform weights: existing 11 checks = 0.04 each, new 6 harder = 0.07–0.12
    checks = {
        # ── original checks (0.04 each, subtotal 0.44) ──
        "config_reverter_removed":  (check_config_reverter_removed,  0.04),
        "disabler_removed":         (check_disabler_removed,         0.04),
        "patcher_rbac_removed":     (check_patcher_rbac_removed,     0.04),
        "scaler_rbac_removed":      (check_scaler_rbac_removed,      0.04),
        "keepalive_fixed":          (check_keepalive_fixed,          0.04),
        "ssl_cache_fixed":          (check_ssl_cache_fixed,          0.04),
        "ssl_timeout_fixed":        (check_ssl_timeout_fixed,        0.04),
        "keepalive_live":           (check_keepalive_live,           0.04),
        "ssl_cache_live":           (check_ssl_cache_live,           0.04),
        "deployment_ready":         (check_deployment_ready,         0.04),
        "https_functional":         (check_https_functional,         0.04),
        # ── new harder checks (subtotal 0.56) ──
        "tls_reconciler_removed":   (check_tls_reconciler_removed,   0.12),
        "pdb_removed":              (check_pdb_removed,              0.10),
        "cluster_binding_removed":  (check_cluster_binding_removed,  0.10),
        "worker_connections_fixed": (check_worker_connections_fixed, 0.07),
        "worker_connections_live":  (check_worker_connections_live,  0.07),
        "covariance_stability":     (check_covariance_stability,     0.10),
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
