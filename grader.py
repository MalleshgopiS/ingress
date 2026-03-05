import subprocess
import re

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

    # 1️⃣ Deployment UID preserved (not deleted/recreated)
    original_uid = run("cat /grader/original_uid")
    current_uid = run(
        f"kubectl get deployment {DEPLOY} -n {NS} -o jsonpath='{{.metadata.uid}}'"
    )
    results["uid_preserved"] = original_uid == current_uid

    # 2️⃣ Image unchanged
    image = run(
        f"kubectl get deployment {DEPLOY} -n {NS} "
        "-o jsonpath='{.spec.template.spec.containers[0].image}'"
    )
    results["image_correct"] = image == "nginx:alpine"

    # 3️⃣ Memory limit unchanged
    memory = run(
        f"kubectl get deployment {DEPLOY} -n {NS} "
        "-o jsonpath='{.spec.template.spec.containers[0].resources.limits.memory}'"
    )
    results["memory_correct"] = memory == "128Mi"

    # 4️⃣ keepalive_timeout fixed to 65 seconds
    config = run(
        f"kubectl get configmap ingress-nginx-config -n {NS} "
        "-o jsonpath='{.data.nginx\\.conf}'"
    )
    results["timeout_fixed"] = bool(
        re.search(r"keepalive_timeout\s+65\s*;", config)
    )

    # 5️⃣ Deployment restarted (pod UID changed)
    old_pod_uid = run("cat /grader/original_pod_uid")
    pod_uid = run(
        f"kubectl get pods -n {NS} -l app=ingress-controller "
        "-o jsonpath='{.items[0].metadata.uid}'"
    )
    results["pod_restarted"] = pod_uid != old_pod_uid

    # 6️⃣ Deployment ready
    ready = run(
        f"kubectl get deployment {DEPLOY} -n {NS} "
        "-o jsonpath='{.status.readyReplicas}'"
    )
    desired = run(
        f"kubectl get deployment {DEPLOY} -n {NS} "
        "-o jsonpath='{.spec.replicas}'"
    )
    results["deployment_ready"] = ready == desired

    # -----------------------------
    # Honest scoring
    # -----------------------------
    total = len(results)
    passed = sum(results.values())
    score = passed / total

    weights = {k: 1 / total for k in results}

    feedback = "\n".join(
        f"{k}: {'PASS' if v else 'FAIL'}"
        for k, v in results.items()
    )

    return GradeResult(score, results, weights, feedback)