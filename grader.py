import subprocess
import re
import time

NS = "default"
DEPLOY = "ingress-controller"


def run(cmd):
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
    weights = {}

    # -------------------------------
    # Weights (tuned for Apex target)
    # -------------------------------
    weights["timeout_fixed"] = 0.35
    weights["deployment_restarted"] = 0.20
    weights["https_serving"] = 0.15
    weights["uid_preserved"] = 0.10
    weights["image_correct"] = 0.10
    weights["memory_correct"] = 0.10

    # -------------------------------
    # 1. Config Fix
    # -------------------------------
    config = run(
        f"kubectl get configmap ingress-nginx-config -n {NS} "
        "-o jsonpath='{.data.nginx\\.conf}'"
    )

    results["timeout_fixed"] = bool(
        re.search(r"keepalive_timeout\s+65;", config)
    )

    # -------------------------------
    # 2. Deployment Restarted
    # -------------------------------
    ready = run(
        f"kubectl get deployment {DEPLOY} -n {NS} "
        "-o jsonpath='{.status.readyReplicas}'"
    )
    results["deployment_restarted"] = ready == "1"

    # -------------------------------
    # 3. HTTPS Functional
    # -------------------------------
    svc_ip = run(
        f"kubectl get svc {DEPLOY} -n {NS} "
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

    # -------------------------------
    # 4. Deployment Not Recreated
    # -------------------------------
    original_uid = run("cat /grader/original_uid")
    current_uid = run(
        f"kubectl get deployment {DEPLOY} -n {NS} "
        "-o jsonpath='{.metadata.uid}'"
    )
    results["uid_preserved"] = original_uid == current_uid

    # -------------------------------
    # 5. Image Not Modified
    # -------------------------------
    image = run(
        f"kubectl get deployment {DEPLOY} -n {NS} "
        "-o jsonpath='{.spec.template.spec.containers[0].image}'"
    )
    results["image_correct"] = image == "nginx:alpine"

    # -------------------------------
    # 6. Resource Limits Preserved
    # -------------------------------
    memory = run(
        f"kubectl get deployment {DEPLOY} -n {NS} "
        "-o jsonpath='{.spec.template.spec.containers[0].resources.limits.memory}'"
    )
    results["memory_correct"] = memory == "128Mi"

    # -------------------------------
    # Final Score
    # -------------------------------
    score = 0
    for k, passed in results.items():
        if passed:
            score += weights[k]

    # -------------------------------
    # Feedback
    # -------------------------------
    feedback_lines = []
    for k, v in results.items():
        status = "PASS" if v else "FAIL"
        feedback_lines.append(f"{k}: {status} (weight {weights[k]})")

    feedback = "\n".join(feedback_lines)

    return GradeResult(score, results, weights, feedback)