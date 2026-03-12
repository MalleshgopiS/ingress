#!/usr/bin/env python3
"""
Grader: 5 equal-weight objectives (0.20 each), each scored partially.

Hardening changes vs. previous version (target mean < 0.40):
  1. nginx_config_strict  — values must be semantically correct (e.g. keepalive ≥ 30 s,
                            ssl_session_cache shared:NAME:SIZE, ssl_session_timeout ≥ 5 min,
                            worker_connections ≥ 512). Setting "1" no longer passes.
  2. gateway_operational  — adds nginx -t syntax check and a 20-request load test (≥ 90 %
                            success). ConfigMap + live pod must both be correct.
  3. sustained_stability  — 5 probes over ~3 min; each probe checks /healthz AND that the
                            ConfigMap was not reverted. Agents who left config-reverter
                            CronJobs running will fail mid-window when they fire.
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


# ── shell helper ─────────────────────────────────────────────────────────────

def run(cmd: str, timeout: int = 30):
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except subprocess.TimeoutExpired:
        return -1, "", "command timed out"
    except Exception as exc:
        return -1, "", str(exc)


# ── cluster state helpers ─────────────────────────────────────────────────────

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


# ── strict nginx-value validators (shared across objectives) ──────────────────

def _keepalive_timeout_ok(text: str) -> bool:
    """keepalive_timeout must be set and ≥ 30 s (not just any non-zero value)."""
    m = re.search(r"keepalive_timeout\s+(\d+)(s|m)?;", text)
    if not m:
        return False
    val  = int(m.group(1))
    unit = m.group(2) or "s"
    return val * (60 if unit == "m" else 1) >= 30


def _ssl_cache_ok(text: str) -> bool:
    """ssl_session_cache must be 'shared:NAME:SIZE' (e.g. shared:SSL:10m).
    Setting 'none' or a bare 'shared:' does not pass."""
    return bool(re.search(r"ssl_session_cache\s+shared:\w+:\d+[mgkMGK];", text))


def _ssl_timeout_ok(text: str) -> bool:
    """ssl_session_timeout must be ≥ 5 minutes (300 s)."""
    m = re.search(r"ssl_session_timeout\s+(\d+)([smhd]?);", text)
    if not m:
        return False
    val  = int(m.group(1))
    unit = m.group(2) or "s"
    secs = val * {"s": 1, "m": 60, "h": 3600, "d": 86400}.get(unit, 1)
    return secs >= 300


def _worker_connections_ok(text: str) -> bool:
    """worker_connections must be ≥ 512, or absent (nginx built-in default applies)."""
    m = re.search(r"worker_connections\s+(\d+);", text)
    if m:
        return int(m.group(1)) >= 512
    return not re.search(r"worker_connections", text)


# ── Objective 1: Rogue CronJobs Removed ──────────────────────────────────────
# 5 sabotage CronJobs — partial score = (# neutralised) / 5.

def _obj_rogue_cronjobs_removed() -> tuple[float, str]:
    items = [
        (ROGUE_CJ_NAME,    ROGUE_CJ_NS),
        (DISABLER_CJ_NAME, DISABLER_CJ_NS),
        (TLS_CJ_NAME,      TLS_CJ_NS),
        (KS_TLS_CJ_NAME,   KS_TLS_CJ_NS),
        (KS_CFG_CJ_NAME,   KS_CFG_CJ_NS),
    ]
    results = {f"{n}@{ns}": _cronjob_gone(n, ns) for n, ns in items}
    n       = sum(results.values())
    detail  = ", ".join(f"{'✓' if ok else '✗'} {k}" for k, ok in results.items())
    return n / len(results), f"{n}/{len(results)} rogue CronJobs neutralised — {detail}"


# ── Objective 2: Unauthorized RBAC + PDB Removed ─────────────────────────────
# 4 items — partial score = (# removed) / 4.

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


# ── Objective 3: Nginx Config Strictly Correct ───────────────────────────────
# 7 checks (4 in ConfigMap, 3 in live pod) with STRICT value requirements.
# Setting keepalive_timeout 1; or worker_connections 1; does NOT pass.
# Partial score = (# correct) / 7.

def _obj_nginx_config_strict() -> tuple[float, str]:
    cfg = _get_configmap()
    pod = _get_running_pod()
    live = ""
    if pod:
        _, live, _ = run(
            f"kubectl exec -n {NS} {pod} -- cat /etc/nginx/nginx.conf", timeout=15
        )

    checks = {
        # ConfigMap checks
        "keepalive_timeout(cm)≥30s":    _keepalive_timeout_ok(cfg),
        "ssl_session_cache(cm)shared":  _ssl_cache_ok(cfg),
        "ssl_session_timeout(cm)≥5min": _ssl_timeout_ok(cfg),
        "worker_connections(cm)≥512":   _worker_connections_ok(cfg),
        # Live pod checks (verifies nginx actually reloaded the correct config)
        "keepalive_timeout(live)≥30s":  bool(live) and _keepalive_timeout_ok(live),
        "ssl_session_cache(live)shared": bool(live) and _ssl_cache_ok(live),
        "worker_connections(live)≥512": bool(live) and _worker_connections_ok(live),
    }
    n      = sum(checks.values())
    detail = ", ".join(f"{'✓' if ok else '✗'} {k}" for k, ok in checks.items())
    return n / len(checks), f"{n}/{len(checks)} nginx settings strictly correct — {detail}"


# ── Objective 4: Gateway Fully Operational ────────────────────────────────────
# 6 checks: basic liveness, constraints, nginx syntax validation, and a load test.
# The load test requires ≥ 90 % success over 20 sequential requests —
# a misconfigured nginx (bad keepalive / ssl cache) will fail under this pressure.
# Partial score = (# passing) / 6.

def _obj_gateway_operational() -> tuple[float, str]:
    results = {}

    # deployment ready (up to 60 s)
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

    # HTTPS returns expected content (retries × 8)
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

    # nginx -t syntax check inside the running pod
    pod = _get_running_pod()
    syntax_ok = False
    if pod:
        _, out, err = run(f"kubectl exec -n {NS} {pod} -- nginx -t", timeout=10)
        syntax_ok = "syntax is ok" in (out + err)
    results["nginx_syntax_valid"] = syntax_ok

    # load test: 20 sequential requests, ≥ 90 % (18/20) must succeed
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
# 5 probes spaced 40 s apart (~3 min window). Each probe checks TWO things:
#   (a) /healthz returns OK
#   (b) ConfigMap settings have NOT been reverted (keepalive + ssl_cache must
#       still be valid at probe time)
# Agents who neutralised all sabotage CronJobs will pass every probe.
# Agents who left config-reverter CronJobs active will fail mid-window
# when the CronJob fires and rolls back the ConfigMap.
# Partial score = (# probes fully passing) / 5.

def _obj_sustained_stability() -> tuple[float, str]:
    ip = _get_cluster_ip()
    if not ip:
        return 0.0, "Could not resolve ClusterIP — stability check skipped"

    probe_results = []
    for i in range(5):
        # (a) /healthz
        _, body, _ = run(f"curl -k -s --max-time 5 https://{ip}/healthz")
        healthz_ok = "ok" in body.lower()

        # (b) ConfigMap not reverted
        cfg        = _get_configmap()
        config_ok  = _keepalive_timeout_ok(cfg) and _ssl_cache_ok(cfg)

        probe_results.append(healthz_ok and config_ok)

        if i < 4:
            time.sleep(40)  # 4 × 40 s = 160 s window total

    n = sum(probe_results)
    return n / len(probe_results), (
        f"{n}/5 stability probes passed (healthz + config-not-reverted) over ~3 min"
    )


# ── Grade ─────────────────────────────────────────────────────────────────────

OBJECTIVES = [
    ("rogue_cronjobs_removed",    _obj_rogue_cronjobs_removed),
    ("unauthorized_rbac_removed", _obj_unauthorized_rbac_removed),
    ("nginx_config_strict",       _obj_nginx_config_strict),
    ("gateway_operational",       _obj_gateway_operational),
    ("sustained_stability",       _obj_sustained_stability),
]
WEIGHT = round(1.0 / len(OBJECTIVES), 6)  # 0.20 each — all objectives equal


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
