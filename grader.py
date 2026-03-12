import subprocess
import re
import time

NS = "ingress-system"
DEPLOY = "ingress-controller"
SVC = "ingress-controller-svc"
ROGUE_CJ_NAME = "platform-config-reconciler"
ROGUE_CJ_NS = "kube-system"


def run(cmd):
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    return r.stdout.strip()


class GradeResult:
    def __init__(self, score, subscores, weights, feedback):
        self.score = score
        self.subscores = subscores
        self.weights = weights
        self.feedback = feedback


def grade(context=None):
    results = {}

    # Weight rationale:
    #   rogue_cronjob_removed  — the core fix; without it config reverts every 2m
    #   config checks          — did agent actually fix all three nginx settings?
    #   live checks            — are fixes reflected in the running pod?
    #   infra checks           — basic deployment/endpoint health
    weights = {
        "rogue_cronjob_removed": 0.25,
        "keepalive_configmap":   0.10,
        "ssl_cache_configmap":   0.15,
        "ssl_timeout_configmap": 0.10,
        "keepalive_live":        0.08,
        "ssl_cache_live":        0.07,
        "rollout_done":          0.08,
        "deployment_ready":      0.05,
        "https_ok":              0.06,
        "health_ok":             0.04,
        "image_unchanged":       0.02,
    }

    # 1️⃣ Rogue CronJob must be gone (deleted or suspended)
    cj_out = run(
        f"kubectl get cronjob {ROGUE_CJ_NAME} -n {ROGUE_CJ_NS} "
        "--no-headers 2>/dev/null"
    )
    cj_exists = bool(cj_out.strip())

    if not cj_exists:
        results["rogue_cronjob_removed"] = True
    else:
        # Accept suspended as a valid alternative to deletion
        suspend = run(
            f"kubectl get cronjob {ROGUE_CJ_NAME} -n {ROGUE_CJ_NS} "
            "-o jsonpath='{.spec.suspend}'"
        )
        results["rogue_cronjob_removed"] = suspend == "true"

    # ── Fetch ConfigMap ────────────────────────────────────────────
    cfg = run(
        f"kubectl get configmap ingress-nginx-config -n {NS} "
        "-o jsonpath='{.data.nginx\\.conf}'"
    )

    # 2️⃣ keepalive_timeout set to a non-zero value (>=30s)
    results["keepalive_configmap"] = bool(
        re.search(r"keepalive_timeout\s+([3-9]\d|[1-9]\d{2,})(s|m)?;", cfg)
    )

    # 3️⃣ ssl_session_cache enabled with shared cache
    results["ssl_cache_configmap"] = bool(
        re.search(r"ssl_session_cache\s+shared:", cfg)
    )

    # 4️⃣ ssl_session_timeout set to non-zero
    results["ssl_timeout_configmap"] = bool(
        re.search(r"ssl_session_timeout\s+([1-9][0-9]*[smhd]?);", cfg)
    )

    # ── Live pod config checks ─────────────────────────────────────
    pod = run(
        f"kubectl get pods -n {NS} -l app=ingress-controller "
        "--field-selector=status.phase=Running "
        "-o jsonpath='{.items[0].metadata.name}'"
    )

    if pod:
        live = run(f"kubectl exec -n {NS} {pod} -- cat /etc/nginx/nginx.conf")
        results["keepalive_live"] = bool(
            re.search(r"keepalive_timeout\s+([3-9]\d|[1-9]\d{2,})(s|m)?;", live)
        )
        results["ssl_cache_live"] = bool(
            re.search(r"ssl_session_cache\s+shared:", live)
        )
    else:
        results["keepalive_live"] = False
        results["ssl_cache_live"] = False

    # ── Rollout & readiness ────────────────────────────────────────
    obs = run(
        f"kubectl get deploy {DEPLOY} -n {NS} "
        "-o jsonpath='{.status.observedGeneration}'"
    )
    gen = run(
        f"kubectl get deploy {DEPLOY} -n {NS} "
        "-o jsonpath='{.metadata.generation}'"
    )
    results["rollout_done"] = obs == gen and obs != ""

    ready = run(
        f"kubectl get deploy {DEPLOY} -n {NS} "
        "-o jsonpath='{.status.readyReplicas}'"
    )
    results["deployment_ready"] = ready == "1"

    # ── Functional HTTPS checks ────────────────────────────────────
    ip = run(
        f"kubectl get svc {SVC} -n {NS} "
        "-o jsonpath='{.spec.clusterIP}'"
    )

    https_ok = False
    for _ in range(8):
        r = run(f"curl -k -s --max-time 5 https://{ip}")
        if "Ingress Controller Running" in r:
            https_ok = True
            break
        time.sleep(3)
    results["https_ok"] = https_ok

    health_ok = False
    for _ in range(8):
        r = run(f"curl -k -s --max-time 5 https://{ip}/healthz")
        if "ok" in r.lower():
            health_ok = True
            break
        time.sleep(3)
    results["health_ok"] = health_ok

    # ── Image guard ────────────────────────────────────────────────
    image = run(
        f"kubectl get deploy {DEPLOY} -n {NS} "
        "-o jsonpath='{.spec.template.spec.containers[0].image}'"
    )
    results["image_unchanged"] = "nginx" in image

    # ── Final score ────────────────────────────────────────────────
    score = sum(weights[k] for k, v in results.items() if v)

    feedback = "\n".join(
        f"{k}: {'PASS' if v else 'FAIL'} (w={weights[k]})"
        for k, v in results.items()
    )

    return GradeResult(score, results, weights, feedback)
