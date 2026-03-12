#!/usr/bin/env python3
"""
Grader for: Ingress Controller TLS Termination Memory Leak
Scoring: 11 checks × ~0.0909 weight each = 1.0 max score

Score bands (expected):
  ~0.18  nothing fixed       (deployment_ready + https_functional only)
  ~0.36  config fixed only   (CronJob-A reverts within 1 min → live checks fail)
  ~0.55  CronJob-A + config  (CronJob-B still disables deployment)
  ~0.73  both CronJobs + config, RBAC not cleaned
  ~1.00  full remediation
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

PATCHER_ROLE = "configmap-patcher"
SCALER_ROLE  = "deployment-scaler"


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


# ─────────────────────────── individual checks ──────────────────────────────

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
    every 3 minutes, making the gateway intermittently unreachable even after
    a configuration fix.
    """
    if _cronjob_gone(DISABLER_CJ_NAME, DISABLER_CJ_NS):
        return True, f"CronJob '{DISABLER_CJ_NAME}' is gone from namespace '{DISABLER_CJ_NS}'"
    return False, f"CronJob '{DISABLER_CJ_NAME}' still active in namespace '{DISABLER_CJ_NS}'"


def check_patcher_rbac_removed():
    """
    The Role 'configmap-patcher' in ingress-system that granted the config-reverter
    CronJob write access to ConfigMaps must be deleted.
    Leaving the RBAC in place allows any actor to re-deploy the same attack.
    """
    code, out, _ = run(f"kubectl get role {PATCHER_ROLE} -n {NS} 2>/dev/null")
    if code != 0 or not out.strip():
        return True, f"Role '{PATCHER_ROLE}' has been removed from namespace '{NS}'"
    return False, f"Role '{PATCHER_ROLE}' still exists in namespace '{NS}' — RBAC not cleaned up"


def check_scaler_rbac_removed():
    """
    The Role 'deployment-scaler' in ingress-system that allowed the disabler
    CronJob to scale the deployment must be deleted. Without removing it the
    cluster remains open to the same scale-to-zero pattern.
    """
    code, out, _ = run(
        f"kubectl get role {SCALER_ROLE} -n {DISABLER_CJ_NS} 2>/dev/null"
    )
    if code != 0 or not out.strip():
        return True, f"Role '{SCALER_ROLE}' has been removed from namespace '{DISABLER_CJ_NS}'"
    return False, f"Role '{SCALER_ROLE}' still exists in namespace '{DISABLER_CJ_NS}'"


def check_keepalive_fixed():
    """
    ConfigMap must contain keepalive_timeout set to any non-zero value.
    A value of 0 disables keep-alive, closing every connection immediately
    and producing the observed per-request TLS renegotiation overhead.
    """
    cfg = _get_configmap()
    if re.search(r"keepalive_timeout\s+[1-9][0-9]*(s|m)?;", cfg):
        return True, "keepalive_timeout is set to a non-zero value in ConfigMap"
    return False, "keepalive_timeout is still 0 or missing in ConfigMap"


def check_ssl_cache_fixed():
    """
    ConfigMap must enable a shared ssl_session_cache. With 'none' every TLS
    handshake performs a full session negotiation — the primary source of the
    memory growth reported between gateway restarts.
    """
    cfg = _get_configmap()
    if re.search(r"ssl_session_cache\s+shared:", cfg):
        return True, "ssl_session_cache is set to a shared cache in ConfigMap"
    return False, "ssl_session_cache is still 'none' or missing shared: prefix in ConfigMap"


def check_ssl_timeout_fixed():
    """
    ConfigMap must contain a non-zero ssl_session_timeout. A value of 0
    causes cached sessions to expire instantly, making the shared cache
    useless and perpetuating the full-handshake pattern.
    """
    cfg = _get_configmap()
    if re.search(r"ssl_session_timeout\s+[1-9][0-9]*[smhd]?;", cfg):
        return True, "ssl_session_timeout is set to a non-zero value in ConfigMap"
    return False, "ssl_session_timeout is still 0 or missing in ConfigMap"


def check_keepalive_live():
    """
    The keepalive_timeout fix must be reflected in the running nginx pod.
    Validates that the volume-mounted config inside the container carries the
    corrected directive — confirming a rolling restart was performed.
    """
    pod = _get_running_pod()
    if not pod:
        return False, "No running ingress-controller pod found to inspect"
    _, live, _ = run(f"kubectl exec -n {NS} {pod} -- cat /etc/nginx/nginx.conf", timeout=15)
    if re.search(r"keepalive_timeout\s+[1-9][0-9]*(s|m)?;", live):
        return True, f"keepalive_timeout is non-zero in live pod '{pod}'"
    return False, f"keepalive_timeout is still 0 in live pod '{pod}' — rolling restart needed"


def check_ssl_cache_live():
    """
    The shared ssl_session_cache directive must be present in the live nginx
    config inside the running pod, confirming the rolling restart picked up
    the updated ConfigMap.
    """
    pod = _get_running_pod()
    if not pod:
        return False, "No running ingress-controller pod found to inspect"
    _, live, _ = run(f"kubectl exec -n {NS} {pod} -- cat /etc/nginx/nginx.conf", timeout=15)
    if re.search(r"ssl_session_cache\s+shared:", live):
        return True, f"ssl_session_cache is shared in live pod '{pod}'"
    return False, f"ssl_session_cache is not shared in live pod '{pod}' — rolling restart needed"


def check_deployment_ready():
    """
    The ingress-controller must have exactly 1 ready replica within 60 s.
    Fails if the disabler CronJob scaled the deployment to 0 and the agent
    did not restore replicas or remove the CronJob in time.
    """
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
    """
    HTTPS must be reachable and return the expected response body at / and
    'ok' at /healthz. Checked with retries to allow for rolling restart
    convergence. Fails if the deployment is at 0 replicas or config is wrong.
    """
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


# ─────────────────────────────── grade() ────────────────────────────────────

def grade(transcript: str) -> GradingResult:
    checks = {
        "config_reverter_removed": check_config_reverter_removed,
        "disabler_removed":        check_disabler_removed,
        "patcher_rbac_removed":    check_patcher_rbac_removed,
        "scaler_rbac_removed":     check_scaler_rbac_removed,
        "keepalive_fixed":         check_keepalive_fixed,
        "ssl_cache_fixed":         check_ssl_cache_fixed,
        "ssl_timeout_fixed":       check_ssl_timeout_fixed,
        "keepalive_live":          check_keepalive_live,
        "ssl_cache_live":          check_ssl_cache_live,
        "deployment_ready":        check_deployment_ready,
        "https_functional":        check_https_functional,
    }

    feedback_parts, passed = [], {}
    for name, fn in checks.items():
        ok, message = fn()
        passed[name] = ok
        feedback_parts.append(f"{'PASS' if ok else 'FAIL'} {name}: {message}")

    weight = 1.0 / len(checks)
    weights = {name: weight for name in checks}
    score = sum(weights[name] for name, ok in passed.items() if ok)

    return GradingResult(
        score=score,
        subscores=passed,
        weights=weights,
        feedback=" | ".join(feedback_parts),
    )
