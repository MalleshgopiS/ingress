import subprocess
import re
import time

NS = "default"
DEPLOY = "ingress-controller"


def run(cmd):
    """Run a shell command and return stdout."""
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    return result.stdout.strip()


class GradeResult:
    def __init__(self, score, subscores, weights, feedback):
        self.score = score
        self.subscores = subscores
        self.weights = weights
        self.feedback = feedback


def grade(context=None):
    """
    Grade the task by verifying:
    1. Deployment UID unchanged
    2. Container image unchanged
    3. Memory limit unchanged
    4. keepalive_timeout fixed
    5. Deployment ready
    6. HTTPS serving correctly
    """

    results = {}

    # Verify deployment was not deleted/recreated
    original_uid = run("cat /grader/original_uid")

    current_uid = run(
        f"kubectl get deployment {DEPLOY} -n {NS} "
        "-o jsonpath='{.metadata.uid}'"
    )

    results["uid_preserved"] = original_uid == current_uid

    # Verify container image unchanged
    image = run(
        f"kubectl get deployment {DEPLOY} -n {NS} "
        "-o jsonpath='{.spec.template.spec.containers[0].image}'"
    )

    results["image_correct"] = image == "nginx:alpine"

    # Verify memory limit unchanged
    memory = run(
        f"kubectl get deployment {DEPLOY} -n {NS} "
        "-o jsonpath='{.spec.template.spec.containers[0].resources.limits.memory}'"
    )

    results["memory_correct"] = memory == "128Mi"

    # Verify nginx config updated
    config = run(
        f"kubectl get configmap ingress-nginx-config -n {NS} "
        "-o jsonpath='{.data.nginx\\.conf}'"
    )

    results["timeout_fixed"] = bool(
        re.search(r"keepalive_timeout\s+65;", config)
    )

    # Verify deployment is ready
    ready = run(
        f"kubectl get deployment {DEPLOY} -n {NS} "
        "-o jsonpath='{.status.readyReplicas}'"
    )

    results["deployment_ready"] = ready == "1"

    # Verify HTTPS endpoint responds
    svc_ip = run(
        f"kubectl get svc ingress-controller -n {NS} "
        "-o jsonpath='{.spec.clusterIP}'"
    )

    https_ok = False

    for _ in range(5):
        response = run(f"curl -k -s https://{svc_ip}")
        if "Ingress Controller Running" in response:
            https_ok = True
            break
        time.sleep(2)

    results["https_serving"] = https_ok

    total_checks = len(results)
    passed_checks = sum(results.values())

    score = passed_checks / total_checks

    weights = {k: 1 / total_checks for k in results}

    feedback = "\n".join(
        f"{k}: {'PASS' if v else 'FAIL'}"
        for k, v in results.items()
    )

    return GradeResult(score, results, weights, feedback)