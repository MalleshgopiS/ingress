import subprocess
import re
import time

"""
Grader for task:
Ingress Controller TLS Termination Memory Leak Causing Periodic Gateway Crashes

This grader verifies the following conditions:

1. uid_preserved
   The Deployment UID must remain unchanged to ensure the deployment
   was not deleted and recreated.

2. image_correct
   The container image must remain 'nginx:alpine'.

3. memory_correct
   The container memory limit must remain '128Mi'.

4. timeout_fixed
   The nginx configuration must update:
       keepalive_timeout 0;
   to:
       keepalive_timeout 65;

5. deployment_ready
   The deployment must have at least one ready replica.

6. https_serving
   The service must successfully serve HTTPS traffic and return
   the expected response: "Ingress Controller Running".
"""

NS = "ingress-system"
DEPLOY = "ingress-controller"


def run(cmd):
    """Execute shell command and return stdout."""
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    return result.stdout.strip()


class GradeResult:
    def __init__(self, score, subscores, weights, feedback):
        self.score = score
        self.subscores = subscores
        self.weights = weights
        self.feedback = feedback


def grade(context=None):

    results = {}

    # -------------------------------------------------
    # Check 1: Deployment UID must remain unchanged
    # -------------------------------------------------
    original_uid = run("cat /grader/original_uid")

    current_uid = run(
        f"kubectl get deployment {DEPLOY} -n {NS} "
        "-o jsonpath='{.metadata.uid}'"
    )

    results["uid_preserved"] = original_uid == current_uid

    # -------------------------------------------------
    # Check 2: Container image unchanged
    # -------------------------------------------------
    image = run(
        f"kubectl get deployment {DEPLOY} -n {NS} "
        "-o jsonpath='{.spec.template.spec.containers[0].image}'"
    )

    results["image_correct"] = image == "nginx:alpine"

    # -------------------------------------------------
    # Check 3: Memory limit unchanged
    # -------------------------------------------------
    memory = run(
        f"kubectl get deployment {DEPLOY} -n {NS} "
        "-o jsonpath='{.spec.template.spec.containers[0].resources.limits.memory}'"
    )

    results["memory_correct"] = memory == "128Mi"

    # -------------------------------------------------
    # Check 4: keepalive_timeout fixed
    # -------------------------------------------------
    config = run(
        f"kubectl get configmap ingress-nginx-config -n {NS} "
        "-o jsonpath='{.data.nginx\\.conf}'"
    )

    results["timeout_fixed"] = bool(
        re.search(r"keepalive_timeout\s+65;", config)
    )

    # -------------------------------------------------
    # Check 5: Deployment ready
    # -------------------------------------------------
    ready = run(
        f"kubectl get deployment {DEPLOY} -n {NS} "
        "-o jsonpath='{.status.readyReplicas}'"
    )

    results["deployment_ready"] = ready == "1"

    # -------------------------------------------------
    # Check 6: HTTPS endpoint responding
    # -------------------------------------------------
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

    # -------------------------------------------------
    # Mean score calculation
    # -------------------------------------------------
    total_checks = len(results)
    passed_checks = sum(results.values())

    mean_score = passed_checks / total_checks

    weights = {k: 1 / total_checks for k in results}

    # FIX: feedback must be string, not dict
    feedback_lines = []
    for k, v in results.items():
        status = "PASS" if v else "FAIL"
        feedback_lines.append(f"{k}: {status}")

    feedback = "\n".join(feedback_lines)

    return GradeResult(mean_score, results, weights, feedback)