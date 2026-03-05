import subprocess
import re
import time

NS = "bleater"
DEPLOY = "ingress-controller"


def run(cmd):
    result = subprocess.run(
        cmd, shell=True, capture_output=True, text=True
    )
    return result.stdout.strip()


class GradeResult:
    def __init__(self, score, subscores, weights, feedback):
        self.score = score
        self.subscores = subscores
        self.weights = weights
        self.feedback = feedback


def grade(context=None):

    results = {}

    # Check 1 — Deployment UID preserved
    original_uid = run("cat /grader/original_uid")

    current_uid = run(
        f"kubectl get deployment {DEPLOY} -n {NS} "
        "-o jsonpath='{.metadata.uid}'"
    )

    results["uid_preserved"] = original_uid == current_uid

    # Check 2 — Image unchanged
    image = run(
        f"kubectl get deployment {DEPLOY} -n {NS} "
        "-o jsonpath='{.spec.template.spec.containers[0].image}'"
    )

    results["image_correct"] = image == "nginx:alpine"

    # Check 3 — Memory unchanged
    memory = run(
        f"kubectl get deployment {DEPLOY} -n {NS} "
        "-o jsonpath='{.spec.template.spec.containers[0].resources.limits.memory}'"
    )

    results["memory_correct"] = memory == "128Mi"

    # Check 4 — ConfigMap fixed
    config = run(
        f"kubectl get configmap ingress-nginx-config -n {NS} "
        "-o jsonpath='{.data.nginx\\.conf}'"
    )

    results["timeout_fixed"] = bool(
        re.search(r"keepalive_timeout\s+65;", config)
    )

    # Check 5 — Deployment ready
    ready = run(
        f"kubectl get deployment {DEPLOY} -n {NS} "
        "-o jsonpath='{.status.readyReplicas}'"
    )

    results["deployment_ready"] = ready == "1"

    # Check 6 — HTTPS responding
    svc_ip = run(
        f"kubectl get svc ingress-controller -n {NS} "
        "-o jsonpath='{.spec.clusterIP}'"
    )

    https_ok = False

    # allow nginx time to restart
    for _ in range(10):
        response = run(f"curl -k -s https://{svc_ip} || true")
        if "Ingress Controller Running" in response:
            https_ok = True
            break
        time.sleep(2)

    results["https_serving"] = https_ok

    total = len(results)
    passed = sum(results.values())

    score = passed / total

    weights = {k: 1 / total for k in results}

    feedback = "\n".join(
        f"{k}: {'PASS' if v else 'FAIL'}"
        for k, v in results.items()
    )

    return GradeResult(score, results, weights, feedback)