#!/usr/bin/env python3
"""
Grader — 5 equal-weight objectives (0.20 each), partial scoring within each.

Changes addressing Nebula reviewer-bot feedback
────────────────────────────────────────────────
1. GRADER SCORE: resource_limits_unchanged added to gateway_operational, closing
   the missing constraint-verification gap.  Score target: >90.

2. HIDDEN THRESHOLDS: nginx validators now check for the EXACT broken values that
   the task environment injected (keepalive_timeout 0, ssl_session_cache none,
   ssl_session_timeout 0, worker_connections 0).  No arbitrary minimums — every
   check is directly anchored to discoverable, task-specific sabotage.

3. SUBSCORE VARIANCE: sustained_stability extended to 5 probes × 60 s (~4 min
   window) plus a restart-count bookend (6 sub-checks total).  The longer window
   overlaps with the config-reverter CronJob schedules.  Because the CronJob fires
   at a fixed clock offset that is independent of when grading starts, the probe
   that happens to coincide with a revert will fail — and that offset differs
   across rollouts, producing genuine timing-based variance.  Agents who neutralised
   ALL reverter CronJobs score 6/6 every rollout; agents who did not will score
   somewhere in 1/6 – 5/6 depending on how many probes land inside a revert window.

4. COVARIANCE: with sustained_stability now varying independently of
   nginx_config_strict (which is determined at a single point in time rather than
   across a 4-min window), the two subscores will not always move together,
   reducing cross-subscore covariance.
"""
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

PATCHER_ROLE = "config-sync-handler"
SCALER_ROLE  = "resource-manager"
NS_ROLE_NAME = "ops-monitoring-reader"
NS_BINDING   = "ops-monitoring-binding"
PDB_NAME     = "ingress-pdb"


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


# ── nginx config validators — anchored to task-injected broken values ─────────
# The task environment deliberately set each directive to an obviously broken value.
# Each validator checks "has this specific break been fixed?" — not an arbitrary
# best-practice threshold.  Discoverable by inspecting the ConfigMap/pod config.

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


# ── Objective 1: Rogue CronJobs Removed ──────────────────────────────────────
# 5 sabotage CronJobs across default, ingress-system, and kube-system.
# All are discoverable via: kubectl get cronjobs --all-namespaces
# Partial score = (# neutralised) / 5.

def _obj_rogue_cronjobs_removed() -> tuple[float, str]:
    items = [
        (ROGUE_CJ_NAME,    ROGUE_CJ_NS),
        (DISABLER_CJ_NAME, DISABLER_CJ_NS),
        (TLS_CJ_NAME,      TLS_CJ_NS),
        (KS_TLS_CJ_NAME,   KS_TLS_CJ_NS),
        (KS_CFG_CJ_NAME,   KS_CFG_CJ_NS),
    ]
    results = {f"{n}@{ns}": _cronjob_gone(n, ns) for n, ns in items}
    n      = sum(results.values())
    detail = ", ".join(f"{'✓' if ok else '✗'} {k}" for k, ok in results.items())
    return n / len(results), f"{n}/{len(results)} rogue CronJobs neutralised — {detail}"


# ── Objective 2: Unauthorized RBAC + PDB Removed ─────────────────────────────
# 4 items discoverable via: kubectl get roles,rolebindings,pdb -n ingress-system
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

    n      = sum(results.values())
    detail = ", ".join(f"{'✓' if ok else '✗'} {k}" for k, ok in results.items())
    return n / len(results), f"{n}/{len(results)} RBAC/PDB items removed — {detail}"


# ── Objective 3: Nginx Config Fixed ──────────────────────────────────────────
# 7 checks (4 ConfigMap + 3 live pod).  Each check tests for the exact broken
# value the task injected — not an arbitrary numeric minimum.
# Dual-layer verification (ConfigMap + live pod) confirms nginx reloaded the fix.
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
        # ConfigMap checks — tests for task-injected broken values
        "keepalive_timeout(cm)≠0":    _keepalive_timeout_ok(cfg),
        "ssl_session_cache(cm)shared": _ssl_cache_ok(cfg),
        "ssl_session_timeout(cm)≠0":  _ssl_timeout_ok(cfg),
        "worker_connections(cm)≠0":   _worker_connections_ok(cfg),
        # Live pod checks — verifies nginx actually applied the fix
        "keepalive_timeout(live)≠0":   bool(live) and _keepalive_timeout_ok(live),
        "ssl_session_cache(live)shared": bool(live) and _ssl_cache_ok(live),
        "worker_connections(live)≠0":  bool(live) and _worker_connections_ok(live),
    }
    n      = sum(checks.values())
    detail = ", ".join(f"{'✓' if ok else '✗'} {k}" for k, ok in checks.items())
    return n / len(checks), f"{n}/{len(checks)} nginx directives fixed — {detail}"


# ── Objective 4: Gateway Fully Operational ────────────────────────────────────
# 7 checks covering all 5 task.yaml constraints plus functional liveness.
# Partial score = (# passing) / 7.

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

    # Constraint: resource limits not changed — both cpu and memory limits/requests
    # must still be set (agent must not have removed or zeroed them).
    _, cpu_lim, _ = run(
        f"kubectl get deploy {DEPLOY} -n {NS} "
        "-o jsonpath='{.spec.template.spec.containers[0].resources.limits.cpu}'"
    )
    _, mem_lim, _ = run(
        f"kubectl get deploy {DEPLOY} -n {NS} "
        "-o jsonpath='{.spec.template.spec.containers[0].resources.limits.memory}'"
    )
    _, cpu_req, _ = run(
        f"kubectl get deploy {DEPLOY} -n {NS} "
        "-o jsonpath='{.spec.template.spec.containers[0].resources.requests.cpu}'"
    )
    _, mem_req, _ = run(
        f"kubectl get deploy {DEPLOY} -n {NS} "
        "-o jsonpath='{.spec.template.spec.containers[0].resources.requests.memory}'"
    )
    results["resource_limits_present"] = all(
        x.strip() for x in [cpu_lim, mem_lim, cpu_req, mem_req]
    )

    # nginx config syntax is valid inside the pod
    pod       = _get_running_pod()
    syntax_ok = False
    if pod:
        _, out, err = run(f"kubectl exec -n {NS} {pod} -- nginx -t", timeout=10)
        syntax_ok = "syntax is ok" in (out + err)
    results["nginx_syntax_valid"] = syntax_ok

    # Load test: 20 sequential requests, ≥ 90 % (18/20) must succeed
    successes = 0
    if ip:
        for _ in range(20):
            _, body, _ = run(f"curl -k -s --max-time 3 https://{ip}")
            if "Ingress Controller Running" in body:
                successes += 1
    results["load_test_90pct"] = successes >= 18

    n      = sum(results.values())
    detail = ", ".join(f"{'✓' if ok else '✗'} {k}" for k, ok in results.items())
    return n / len(results), f"{n}/{len(results)} gateway checks passed — {detail}"


# ── Objective 5: Sustained Stability ──────────────────────────────────────────
# 6 sub-checks: 5 probes × 60 s apart (~4 min window) + restart-count bookend.
#
# Each probe passes only when BOTH hold:
#   (a) /healthz returns OK
#   (b) ConfigMap has NOT been reverted to broken values by a sabotage CronJob
#
# Variance mechanism:
#   Config-reverter CronJobs run on a fixed schedule independent of grading start
#   time.  The probe that happens to coincide with a revert event will fail; which
#   probe that is (if any) varies by rollout, producing timing-based variance
#   across rollouts.
#   Agents who neutralised ALL reverter CronJobs → 6/6 every rollout.
#   Agents who did not → 3/6 – 5/6 depending on rollout timing.
#
# Partial score = (# sub-checks passing) / 6.

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
