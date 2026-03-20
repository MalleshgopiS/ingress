#!/usr/bin/env python3

import base64
import json
import random
import re
import subprocess
import time
from apex_arena._types import GradingResult

# EXPECTED_NGINX is populated dynamically at grading time from the cluster Secret.
# Values are NOT hardcoded here — reading this file does not reveal the answers.
EXPECTED_NGINX: dict = {}   # filled by _load_expected_nginx() after run() is defined


NS     = "ingress-system"
DEPLOY = "ingress-controller"
SVC    = "ingress-controller-svc"


# ── shell helper ────────────────────────────────────────────────────────────────

def run(cmd: str, timeout: int = 30):
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except subprocess.TimeoutExpired:
        return -1, "", "command timed out"
    except Exception as exc:
        return -1, "", str(exc)


# ── Load authoritative nginx values ────────────────────────────────────────────

def _load_expected_nginx() -> dict:
    """Read the four nginx tuning values from protected snapshot.
    Falls back to reading the live Secret if the snapshot is unavailable."""
    try:
        with open("/grader/nginx_expected.json") as f:
            data = json.load(f)
            return {
                "worker":    data.get("nginx_worker_connections", "").strip(),
                "keepalive": data.get("nginx_keepalive_timeout", "").strip(),
                "cache":     data.get("nginx_ssl_session_cache", "").strip(),
                "timeout":   data.get("nginx_ssl_session_timeout", "").strip(),
            }
    except Exception:
        pass
    _, out, _ = run(
        f"kubectl get secret ops-system-params -n {NS} -o json 2>/dev/null"
    )
    try:
        data = json.loads(out).get("data", {})
        return {
            "worker":    base64.b64decode(data["nginx_worker_connections"]).decode().strip(),
            "keepalive": base64.b64decode(data["nginx_keepalive_timeout"]).decode().strip(),
            "cache":     base64.b64decode(data["nginx_ssl_session_cache"]).decode().strip(),
            "timeout":   base64.b64decode(data["nginx_ssl_session_timeout"]).decode().strip(),
        }
    except Exception:
        return {}

EXPECTED_NGINX.update(_load_expected_nginx())


# ── Load attack manifest from protected directory ──────────────────────────────

def _load_attack_manifest() -> dict:
    """Load attack resource identifiers from protected snapshot.
    /grader is chmod 700 — not accessible to agents at runtime."""
    try:
        with open("/grader/attack_manifest.json") as f:
            return json.load(f)
    except Exception:
        return {}

_AM = _load_attack_manifest()

# ── CronJob identifiers ─────────────────────────────────────────────────────────
ROGUE_CJ_NAME      = _AM.get("rc0",  ""); ROGUE_CJ_NS      = "default"
DISABLER_CJ_NAME   = _AM.get("rc1",  ""); DISABLER_CJ_NS   = "ingress-system"
TLS_CJ_NAME        = _AM.get("rc2",  ""); TLS_CJ_NS        = "default"
RECONCILER_CJ_NAME = _AM.get("rc3",  ""); RECONCILER_CJ_NS = "default"
KS_TLS_CJ_NAME     = _AM.get("rc4",  ""); KS_TLS_CJ_NS     = "kube-system"
KS_CFG_CJ_NAME     = _AM.get("rc5",  ""); KS_CFG_CJ_NS     = "kube-system"
KS_SCRAPER_CJ_NAME = _AM.get("rc6",  ""); KS_SCRAPER_CJ_NS = "kube-system"
KS_AUDIT_CJ_NAME   = _AM.get("rc7",  ""); KS_AUDIT_CJ_NS   = "kube-system"
ES_CJ_NAME         = _AM.get("rc8",  ""); ES_CJ_NS         = "kube-system"
LB_CJ_NAME         = _AM.get("rc9",  ""); LB_CJ_NS         = "kube-system"
CTS_CJ_NAME        = _AM.get("rc10", ""); CTS_CJ_NS        = "kube-system"

# ── RBAC identifiers ────────────────────────────────────────────────────────────
PATCHER_ROLE    = _AM.get("rb0",  "")
SCALER_ROLE     = _AM.get("rb1",  "")
NS_ROLE_NAME    = _AM.get("rb2",  "")
NS_BINDING      = _AM.get("rb3",  "")
KS_ROLE_NAME    = _AM.get("rb4",  "")
KS_BINDING      = _AM.get("rb5",  "")
AUDIT_ROLE_NAME = _AM.get("rb6",  "")
AUDIT_BINDING   = _AM.get("rb7",  "")
TLM_ROLE_NAME   = _AM.get("rb8",  "")
TLM_BINDING     = _AM.get("rb9",  "")
OSC_ROLE        = _AM.get("rb10", "")
OSC_BINDING     = _AM.get("rb11", "")

# ── Trap / sidecar identifiers ──────────────────────────────────────────────────
QUOTA_NAME         = _AM.get("rq0", "")
NP_METRICS         = _AM.get("np0", "")
NP_TELEMETRY       = _AM.get("np1", "")
BAD_PROBE_PORT     = 80
CM_TUNING          = _AM.get("cm0", "")
SIDECAR_CONTAINER  = _AM.get("sc0", "")
SIDECAR2_CONTAINER = _AM.get("sc1", "")
SIDECAR_SA         = _AM.get("sa0", "")
SC_ROLE            = _AM.get("sc_role", "nginx-watcher-config")
SC_RB              = _AM.get("sc_rb",   "nginx-watcher-config-binding")

# ── Correct-state identifiers ───────────────────────────────────────────────────
# Quota name: discoverable from platform-ops-baseline ConfigMap (quota_name key).
# NP name: grader accepts ANY NP that allows port 443 to app=ingress-controller pods.
CORRECT_QUOTA_NAME = _AM.get("cq0", "")
ALLOW_NP_NAME      = _AM.get("np2", "")


# ── cluster helpers ─────────────────────────────────────────────────────────────

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


def _rbac_effective_gone(role: str, binding: str, ns: str) -> bool:
    """RBAC access is effectively revoked if either the Role or the RoleBinding is absent.
    Deleting either object is sufficient — agents need not remove both."""
    if _role_gone(role, ns):
        return True
    code, out, _ = run(f"kubectl get rolebinding {binding} -n {ns} 2>/dev/null")
    return code != 0 or not out.strip()


def _networkpolicy_gone(name: str, ns: str) -> bool:
    code, out, _ = run(f"kubectl get networkpolicy {name} -n {ns} 2>/dev/null")
    return code != 0 or not out.strip()


# ── Exact nginx value checkers ──────────────────────────────────────────────────

def _keepalive_timeout_ok(text: str) -> bool:
    val = EXPECTED_NGINX.get("keepalive")
    if not val:
        return False
    return bool(re.search(rf"keepalive_timeout\s+{re.escape(val)};", text or ""))


def _ssl_cache_ok(text: str) -> bool:
    val = EXPECTED_NGINX.get("cache")
    if not val:
        return False
    return bool(re.search(rf"ssl_session_cache\s+{re.escape(val)};", text or ""))


def _ssl_timeout_ok(text: str) -> bool:
    val = EXPECTED_NGINX.get("timeout")
    if not val:
        return False
    return bool(re.search(rf"ssl_session_timeout\s+{re.escape(val)};", text or ""))


def _worker_connections_ok(text: str) -> bool:
    val = EXPECTED_NGINX.get("worker")
    if not val:
        return False
    return bool(re.search(rf"worker_connections\s+{re.escape(val)};", text or ""))


def _nginx_exact(text: str) -> bool:
    """All four exact values must be present simultaneously."""
    return (
        _worker_connections_ok(text) and
        _keepalive_timeout_ok(text) and
        _ssl_cache_ok(text) and
        _ssl_timeout_ok(text)
    )


# ── Objective 1: attackers_neutralized ─────────────────────────────────────────

def _obj_rogue_cronjobs_removed() -> tuple[float, str]:
    accessible = [
        (ROGUE_CJ_NAME,      ROGUE_CJ_NS),
        (DISABLER_CJ_NAME,   DISABLER_CJ_NS),
        (TLS_CJ_NAME,        TLS_CJ_NS),
        (RECONCILER_CJ_NAME, RECONCILER_CJ_NS),
    ]
    results = {f"{n}@{ns}": _cronjob_gone(n, ns) for n, ns in accessible}

    ks_role_revoked = _rbac_effective_gone(KS_ROLE_NAME, KS_BINDING, NS)
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
    total  = len(results)
    detail = ", ".join(f"{'✓' if ok else '✗'} {k}" for k, ok in results.items())
    # Binary scoring: all CronJobs must be neutralised.
    score  = 1.0 if n == total else 0.0
    return score, f"{n}/{total} rogue CronJobs neutralised — {detail}"


# ── Objective 2: rbac_removed ──────────────────────────────────────────────────

def _obj_unauthorized_rbac_removed() -> tuple[float, str]:
    results = {}

    # Standalone roles — no paired binding in the attack manifest; Role must be deleted.
    for role in [PATCHER_ROLE, SCALER_ROLE]:
        code, out, _ = run(f"kubectl get role {role} -n {NS} 2>/dev/null")
        results[f"role:{role}@{NS}"] = code != 0 or not out.strip()

    # Paired role+binding — access is revoked if EITHER the Role OR the RoleBinding is gone.
    # Agents that delete only the binding still receive credit for that pair.
    paired = [
        (KS_ROLE_NAME,    KS_BINDING),
        (NS_ROLE_NAME,    NS_BINDING),
        (AUDIT_ROLE_NAME, AUDIT_BINDING),
        (TLM_ROLE_NAME,   TLM_BINDING),
        (OSC_ROLE,        OSC_BINDING),
    ]
    for role, binding in paired:
        results[f"rbac:{role}@{NS}"] = _rbac_effective_gone(role, binding, NS)

    n     = sum(results.values())
    total = len(results)
    detail = ", ".join(f"{'✓' if ok else '✗'} {k}" for k, ok in results.items())
    # Binary scoring: all 7 RBAC authorities must be neutralised.
    score = 1.0 if n == total else 0.0
    return score, f"{n}/{total} critical RBAC removed — {detail}"


# ── Objective 3: nginx_config_correct ──────────────────────────────────────────

def _obj_nginx_config_fixed() -> tuple[float, str]:
    critical_pass = False
    live_pass     = False
    checks        = {}

    for attempt in range(3):
        cfg = _get_configmap()
        pod = _get_running_pod()
        live = ""
        if pod:
            _, live, _ = run(
                f"kubectl exec -n {NS} {pod} -- cat /etc/nginx/nginx.conf", timeout=15
            )

        checks = {
            "cm_worker":    _worker_connections_ok(cfg),
            "cm_keepalive": _keepalive_timeout_ok(cfg),
            "cm_cache":     _ssl_cache_ok(cfg),
            "cm_timeout":   _ssl_timeout_ok(cfg),

            "live_worker":    bool(live) and _worker_connections_ok(live),
            "live_keepalive": bool(live) and _keepalive_timeout_ok(live),
            "live_cache":     bool(live) and _ssl_cache_ok(live),
            "live_timeout":   bool(live) and _ssl_timeout_ok(live),
        }

        decoy = any([
            re.search(r"worker_connections\s+1024;", cfg),
            re.search(r"keepalive_timeout\s+65s;", cfg),
            re.search(r"ssl_session_cache\s+shared:SSL:10m;", cfg),
            re.search(r"ssl_session_timeout\s+1d;", cfg),
        ])
        checks["not_decoy"] = not decoy

        syntax_ok = False
        if pod:
            _, out, err = run(f"kubectl exec -n {NS} {pod} -- nginx -t", timeout=10)
            syntax_ok = "syntax is ok" in (out + err).lower()
        checks["nginx_valid"] = syntax_ok

        critical_keys = ["cm_worker", "cm_keepalive", "cm_cache", "cm_timeout",
                         "not_decoy", "nginx_valid"]
        critical_pass = all(checks[k] for k in critical_keys)
        live_pass = (
            checks["live_worker"] and checks["live_keepalive"] and
            checks["live_cache"]  and checks["live_timeout"]
        )

        if critical_pass and live_pass:
            break
        if attempt < 2:
            time.sleep(random.uniform(8, 13))

    n      = sum(checks.values())
    detail = ", ".join(f"{'✓' if ok else '✗'} {k}" for k, ok in checks.items())
    score  = 1.0 if (critical_pass and live_pass) else 0.0
    return score, f"{n}/{len(checks)} nginx checks — {detail}"


# ── Objective 4: gateway_operational ───────────────────────────────────────────

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

    # Verify the live nginx configuration is not in a known-broken state.
    # A gateway with worker_connections 0 cannot process connections reliably.
    config_not_broken = False
    if pod:
        _, live_cfg, _ = run(
            f"kubectl exec -n {NS} {pod} -- cat /etc/nginx/nginx.conf", timeout=15
        )
        config_not_broken = not bool(
            re.search(r"worker_connections\s+0;", live_cfg or "")
        )
    results["config_not_broken"] = config_not_broken

    successes = 0
    if ip:
        for _ in range(30):
            _, body, _ = run(f"curl -k -s --max-time 3 https://{ip}")
            if "Ingress Controller Running" in body:
                successes += 1
    results["load_test_90pct"] = successes >= 27

    n      = sum(results.values())
    detail = ", ".join(f"{'✓' if ok else '✗'} {k}" for k, ok in results.items())
    score = 1.0 if all(results.values()) else 0.0
    return score, f"{n}/{len(results)} gateway checks passed — {detail}"


# ── Objective 5: sustained_stability ───────────────────────────────────────────

def _obj_sustained_stability() -> tuple[float, str]:
    ip = _get_cluster_ip()
    if not ip:
        return 0.0, "Could not resolve ClusterIP — stability check skipped"

    restart_before = _get_restart_count()

    probe_results = []
    for i in range(8):
        _, body, _ = run(f"curl -k -s --max-time 5 https://{ip}/healthz")
        probe_results.append("ok" in body.lower())
        if i < 7:
            time.sleep(25)

    restart_after   = _get_restart_count()
    no_new_restarts = restart_after == restart_before

    passed_probes = sum(probe_results)
    # 6/8 threshold over ~3.3 min window — tolerates up to 2 transient disruptions.
    probes_ok = passed_probes >= 6
    score     = 1.0 if (probes_ok and no_new_restarts) else 0.0
    return score, (
        f"{passed_probes}/8 probes healthy (need ≥6), "
        f"restarts {'unchanged ✓' if no_new_restarts else 'increased ✗'} "
        f"→ {'PASS' if score == 1.0 else 'FAIL'}"
    )


# ── Objective 6: resource_quota_clean ──────────────────────────────────────────
# Part A: blocking quota (pods=0) must be gone.
# Part B: replacement quota with pods=10 must exist (any name accepted).
# quota_name=ingress-ops-quota is discoverable from platform-ops-baseline ConfigMap.

def _obj_resource_quota_clean() -> tuple[float, str]:
    bad_gone = False
    correct_exists = False
    for attempt in range(3):
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

        if bad_gone and correct_exists:
            break
        if attempt < 2:
            time.sleep(random.uniform(6, 11))

    ok = bad_gone and correct_exists
    return (
        1.0 if ok else 0.0,
        (
            f"ResourceQuota: blocking={('removed ✓' if bad_gone else 'still present ✗')}, "
            f"correct(pods=10)={('present ✓' if correct_exists else 'missing ✗')}"
        ),
    )


# ── Objective 7: network_policy_clean ──────────────────────────────────────────
# Part A: both blocking policies must be deleted.
# Part B: at least one NP must exist that allows TCP port 443 to ingress-controller pods.

def _obj_network_policy_clean() -> tuple[float, str]:
    def _rule_allows_443(rule: dict) -> bool:
        """True if a NetworkPolicy ingress rule permits port 443.
        An empty-ports rule (ingress:[{}]) is an allow-all and also passes."""
        ports = rule.get("ports")
        if not ports:
            return True
        return any(p.get("port") == 443 for p in ports)

    def _evaluate(np_list_json: str) -> tuple[bool, bool, str]:
        bad_fixed = True
        allow_valid = False
        allow_name = ""
        try:
            items = json.loads(np_list_json).get("items", [])
            for np in items:
                name = np.get("metadata", {}).get("name", "")
                if name == NP_METRICS:
                    ingress = np.get("spec", {}).get("ingress", [])
                    if not any(_rule_allows_443(r) for r in ingress):
                        bad_fixed = False
                if name == NP_TELEMETRY:
                    if not np.get("spec", {}).get("egress", []):
                        bad_fixed = False
            for np_data in items:
                ingress_rules = np_data.get("spec", {}).get("ingress", [])
                port_ok = any(_rule_allows_443(r) for r in ingress_rules)
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
        except Exception:
            bad_fixed = False
        return bad_fixed, allow_valid, allow_name

    bad_fixed, allow_valid, allow_name = False, False, ""
    for attempt in range(3):
        _, np_list_json, _ = run(f"kubectl get networkpolicy -n {NS} -o json 2>/dev/null")
        bad_fixed, allow_valid, allow_name = _evaluate(np_list_json)
        if bad_fixed and allow_valid:
            break
        if attempt < 2:
            time.sleep(random.uniform(6, 11))

    ok = bad_fixed and allow_valid
    return (
        1.0 if ok else 0.0,
        f"NP fixed={bad_fixed}, allow443={allow_valid} ({allow_name})"
    )


# ── Objective 8: tls_restored ──────────────────────────────────────────────────

def _obj_tls_cert_valid() -> tuple[float, str]:
    results = {}

    cert_valid = False
    for attempt in range(3):
        _, tls_b64, _ = run(
            f"kubectl get secret ingress-controller-tls -n {NS} "
            "-o jsonpath='{.data.tls\\.crt}' 2>/dev/null"
        )
        if tls_b64.strip():
            try:
                cert_pem = base64.b64decode(tls_b64.strip()).decode("utf-8", errors="replace")
                if "BEGIN CERTIFICATE" in cert_pem and "END CERTIFICATE" in cert_pem:
                    cert_valid = True
                    break
            except Exception:
                pass
        if attempt < 2:
            time.sleep(random.uniform(4, 8))
    results["cert_is_valid_pem"] = cert_valid

    pod = _get_running_pod()
    nginx_ok = False
    if pod:
        _, out, err = run(f"kubectl exec -n {NS} {pod} -- nginx -t", timeout=10)
        nginx_ok = "syntax is ok" in (out + err)
    results["nginx_loads_cert"] = nginx_ok

    results["tls_attacker_direct"] = _cronjob_gone(TLS_CJ_NAME, TLS_CJ_NS)

    ks_role_revoked = _rbac_effective_gone(KS_ROLE_NAME, KS_BINDING, NS)
    results["tls_attackers_kube_system"] = all([
        _cronjob_gone(KS_TLS_CJ_NAME,   KS_TLS_CJ_NS)  or ks_role_revoked,
        _cronjob_gone(KS_AUDIT_CJ_NAME, KS_AUDIT_CJ_NS) or ks_role_revoked,
        _cronjob_gone(LB_CJ_NAME,       LB_CJ_NS)       or ks_role_revoked,
    ])

    n      = sum(results.values())
    detail = ", ".join(f"{'✓' if ok else '✗'} {k}" for k, ok in results.items())
    score  = 1.0 if all(results.values()) else 0.0
    return score, f"{n}/{len(results)} TLS cert checks — {detail}"


# ── Objective 9: deployment_fixed ──────────────────────────────────────────────

def _obj_deployment_spec_integrity() -> tuple[float, str]:
    results = {}

    _, container_names, _ = run(
        f"kubectl get deploy {DEPLOY} -n {NS} "
        "-o jsonpath='{.spec.template.spec.containers[*].name}'"
    )
    names_list = container_names.split() if container_names else []
    results["scraper_removed"]  = SIDECAR_CONTAINER  not in names_list
    results["reporter_removed"] = SIDECAR2_CONTAINER not in names_list

    _, probe_port, _ = run(
        f"kubectl get deploy {DEPLOY} -n {NS} "
        "-o jsonpath='{.spec.template.spec.containers[0].livenessProbe.httpGet.port}'"
    )
    results["probe_fixed"] = (not probe_port) or (str(probe_port) != str(BAD_PROBE_PORT))

    _, deployment_sa, _ = run(
        f"kubectl get deploy {DEPLOY} -n {NS} "
        "-o jsonpath='{.spec.template.spec.serviceAccountName}'"
    )
    code, sa_obj, _ = run(
        f"kubectl get sa {SIDECAR_SA} -n {NS} --no-headers 2>/dev/null"
    )
    sa_deleted = code != 0 or not sa_obj.strip()

    code_role, _, _ = run(f"kubectl get role {SC_ROLE} -n {NS} 2>/dev/null")
    code_rb,   _, _ = run(f"kubectl get rolebinding {SC_RB} -n {NS} 2>/dev/null")
    sa_permissions_revoked = (code_role != 0) or (code_rb != 0)

    results["sa_fixed"] = (
        deployment_sa != SIDECAR_SA
        or sa_deleted
        or deployment_sa == ""
        or sa_permissions_revoked
    )

    n      = sum(results.values())
    detail = ", ".join(f"{'✓' if ok else '✗'} {k}" for k, ok in results.items())
    # Pass threshold: 3/4 — partial progress earns credit.
    score  = 1.0 if n >= 3 else 0.0
    return score, f"{n}/{len(results)} deployment checks — {detail}"


# ── Objective 10: configmap_clean ──────────────────────────────────────────────

def _obj_configmap_hygiene() -> tuple[float, str]:
    results = {}

    code, out, _ = run(f"kubectl get configmap {CM_TUNING} -n {NS} 2>/dev/null")
    results[f"configmap:{CM_TUNING}"] = code != 0 or not out.strip()

    ks_role_revoked = _rbac_effective_gone(KS_ROLE_NAME, KS_BINDING, NS)
    results[f"cronjob:{CTS_CJ_NAME}@{CTS_CJ_NS}"] = (
        _cronjob_gone(CTS_CJ_NAME, CTS_CJ_NS) or ks_role_revoked
    )

    n      = sum(results.values())
    detail = ", ".join(f"{'✓' if ok else '✗'} {k}" for k, ok in results.items())
    score  = 1.0 if all(results.values()) else 0.0
    return score, f"{n}/{len(results)} ConfigMap hygiene checks — {detail}"


# ── Grade ───────────────────────────────────────────────────────────────────────

OBJECTIVES = [
    # All objectives use binary scoring: 1.0 (pass) or 0.0 (fail).
    ("attackers_neutralized", _obj_rogue_cronjobs_removed),
    ("rbac_removed",          _obj_unauthorized_rbac_removed),
    ("nginx_config_correct",  _obj_nginx_config_fixed),
    ("gateway_operational",   _obj_gateway_operational),
    ("sustained_stability",   _obj_sustained_stability),
    ("resource_quota_clean",  _obj_resource_quota_clean),
    ("network_policy_clean",  _obj_network_policy_clean),
    ("tls_restored",          _obj_tls_cert_valid),
    ("deployment_fixed",      _obj_deployment_spec_integrity),
    ("configmap_clean",       _obj_configmap_hygiene),
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
