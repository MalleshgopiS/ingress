import subprocess
import time
import re

NS = "ingress-system"
DEPLOY = "ingress-controller"


def run(cmd):
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    return result.stdout.strip()


def wait_until(cmd, timeout=180):
    start = time.time()
    while time.time() - start < timeout:
        if run(cmd):
            return True
        time.sleep(2)
    return False


class GradeResult:
    def __init__(self, score, details):
        self.score = score
        self.details = details


def grade():
    """
    Grader verifies:

    1. Deployment UID unchanged
    2. Image unchanged (nginx:1.25-alpine)
    3. Memory limit unchanged (128Mi)
    4. keepalive_timeout updated to valid value
    5. Deployment ready
    6. HTTP response correct
    """

    results = {}

    # UID preserved
    original = run("cat /grader/original_uid")
    current = run(
        f"kubectl get deployment {DEPLOY} -n {NS} -o jsonpath='{{.metadata.uid}}'"
    )

    results["uid_preserved"] = original == current

    # Image unchanged
    image = run(
        f"kubectl get deploy {DEPLOY} -n {NS} -o jsonpath='{{.spec.template.spec.containers[0].image}}'"
    )
    results["image_unchanged"] = image == "nginx:1.25-alpine"

    # Memory limit unchanged
    memory = run(
        f"kubectl get deploy {DEPLOY} -n {NS} -o jsonpath='{{.spec.template.spec.containers[0].resources.limits.memory}}'"
    )
    results["memory_limit"] = memory == "128Mi"

    # Timeout fixed
    config = run(
        f"kubectl get configmap ingress-nginx-config -n {NS} -o jsonpath='{{.data.nginx\\.conf}}'"
    )

    results["timeout_valid"] = bool(
        re.search(r"keepalive_timeout\s+65;", config)
    )

    # Deployment ready
    ready = run(
        f"kubectl get deploy {DEPLOY} -n {NS} -o jsonpath='{{.status.readyReplicas}}'"
    )
    results["deployment_ready"] = ready == "1"

    # HTTP test
    svc = run(
        f"kubectl get svc ingress-controller -n {NS} -o jsonpath='{{.spec.clusterIP}}'"
    )

    http = run(f"curl -s http://{svc}")

    results["nginx_serving"] = "Ingress Controller Running" in http

    score = sum(results.values()) / len(results)

    return GradeResult(score, results)