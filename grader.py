import subprocess
import re
import time

NS = "default"
DEPLOY = "ingress-controller"


def run(cmd):
    """Run shell command and return stdout."""
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
    Grader verifies:
    1. Deployment not recreated (UID preserved)
    2. Image unchanged
    3. Memory limit unchanged
    4. keepalive_timeout fixed
    5. worker_connections fixed
    6. Deployment ready
    7. HTTPS endpoint working
    """

    results = {}

    # --- UID preserved ---
    original_uid = run("cat /grader/original_uid")
    current_uid = run(
        f"kubectl get deployment {DEPLOY} -n {NS} "
        "-o jsonpath='{.metadata.uid}'"
    )
    results["uid_preserved"] = original_uid == current_uid

    # --- Image unchanged ---
    image = run(
        f"kubectl get deployment {DEPLOY} -n {NS} "
        "-o jsonpath='{.spec.template.spec.containers[0].image}'"
    )
    results["image_correct"] = image == "nginx:alpine"

    # --- Memory unchanged ---
    memory = run(
        f"kubectl get deployment {DEPLOY} -n {NS} "
        "-o jsonpath='{.spec.template.spec.containers[0].resources.limits.memory}'"
    )
    results["memory_correct"] = memory == "128Mi"

    # --- Read nginx config ---
    config = run(
        f"kubectl get configmap ingress-nginx-config -n {NS} "
        "-o jsonpath='{.data.nginx\\.conf}'"
    )

    # --- keepalive_timeout fixed (robust regex) ---
    results["keepalive_fixed"] = bool(
        re.search(r"keepalive_timeout\s+65s?\s*;", config)
    )

    # --- worker_connections fixed ---
    results["worker_connections_fixed"] = bool(
        re.search(r"worker_connections\s+1024\s*;", config)
    )

    # --- Deployment ready ---
    ready = run(
        f"kubectl get deployment {DEPLOY} -n {NS} "
        "-o jsonpath='{.status.readyReplicas}'"
    )
    results["deployment_ready"] = ready == "1"

    # --- HTTPS serving ---
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

    # --- Score calculation (true performance) ---
    total_checks = len(results)
    passed_checks = sum(results.values())
    score = passed_checks / total_checks

    weights = {k: 1 / total_checks for k in results}

    feedback = "\n".join(
        f"{k}: {'PASS' if v else 'FAIL'}"
        for k, v in results.items()
    )

    return GradeResult(score, results, weights, feedback)