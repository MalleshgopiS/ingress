import subprocess
import re
import time

NS = "default"
DEPLOY = "ingress-controller"
CM = "ingress-nginx-config"


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

    checks = [
        "timeout_fixed",
        "revision_changed",
        "deployment_ready",
        "https_serving",
        "rollout_stable",
        "config_syntax_valid",
    ]

    weight_each = 1.0 / len(checks)
    weights = {k: weight_each for k in checks}

    # 1. Core fix
    config = run(
        f"kubectl get configmap {CM} -n {NS} "
        "-o jsonpath='{.data.nginx\\.conf}'"
    )
    results["timeout_fixed"] = bool(
        re.search(r"keepalive_timeout\s+65;", config)
    )

    # 2. Real rollout happened
    revision = run(
        f"kubectl get deployment {DEPLOY} -n {NS} "
        "-o jsonpath='{.metadata.annotations.deployment\\.kubernetes\\.io/revision}'"
    )
    results["revision_changed"] = revision != "1"

    # 3. Deployment Ready
    ready = run(
        f"kubectl get deployment {DEPLOY} -n {NS} "
        "-o jsonpath='{.status.readyReplicas}'"
    )
    results["deployment_ready"] = ready == "1"

    # 4. HTTPS Functional
    svc_ip = run(
        f"kubectl get svc {DEPLOY} -n {NS} "
        "-o jsonpath='{.spec.clusterIP}'"
    )

    https_ok = False
    for _ in range(5):
        resp = run(f"curl -k -s https://{svc_ip}")
        if "Ingress Controller Running" in resp:
            https_ok = True
            break
        time.sleep(2)
    results["https_serving"] = https_ok

    # 5. Rollout stability (fixed to avoid false failure)
    # Wait briefly to allow pod stabilization
    time.sleep(5)
    unavailable = run(
        f"kubectl get deployment {DEPLOY} -n {NS} "
        "-o jsonpath='{.status.unavailableReplicas}'"
    )
    results["rollout_stable"] = unavailable in ["", "0", "<no value>"]

    # 6. Basic config structure valid
    results["config_syntax_valid"] = "http {" in config and "server {" in config

    score = sum(weights[k] for k, v in results.items() if v)

    feedback = "\n".join(
        f"{k}: {'PASS' if v else 'FAIL'} (w={weights[k]:.3f})"
        for k, v in results.items()
    )

    return GradeResult(score, results, weights, feedback)