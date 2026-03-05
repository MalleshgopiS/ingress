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
    """Ingress controller config verification"""

    results = {}

    original_uid = run("cat /grader/original_uid")
    current_uid = run(
        f"kubectl get deployment {DEPLOY} -n {NS} "
        "-o jsonpath='{.metadata.uid}'"
    )
    results["uid_preserved"] = original_uid == current_uid

    image = run(
        f"kubectl get deployment {DEPLOY} -n {NS} "
        "-o jsonpath='{.spec.template.spec.containers[0].image}'"
    )
    results["image_correct"] = image == "nginx:alpine"

    memory = run(
        f"kubectl get deployment {DEPLOY} -n {NS} "
        "-o jsonpath='{.spec.template.spec.containers[0].resources.limits.memory}'"
    )
    results["memory_correct"] = memory == "128Mi"

    replicas = run(
        f"kubectl get deployment {DEPLOY} -n {NS} "
        "-o jsonpath='{.spec.replicas}'"
    )
    results["replica_count"] = replicas == "1"

    config = run(
        f"kubectl get configmap ingress-nginx-config -n {NS} "
        "-o jsonpath='{.data.nginx\\.conf}'"
    )

    results["keepalive_fixed"] = bool(
        re.search(r"keepalive_timeout\s+65s?\s*;", config)
    )

    results["worker_fixed"] = bool(
        re.search(r"worker_connections\s+1024\s*;", config)
    )

    results["worker_in_events"] = bool(
        re.search(r"events\s*{[^}]*worker_connections\s+1024", config, re.DOTALL)
    )

    results["timeout_not_zero"] = "keepalive_timeout 0" not in config
    results["worker_not_one"] = "worker_connections 1" not in config

    ready = run(
        f"kubectl get deployment {DEPLOY} -n {NS} "
        "-o jsonpath='{.status.availableReplicas}'"
    )
    results["deployment_available"] = ready == "1"

    pod_running = run(
        f"kubectl get pods -n {NS} -l app=ingress-controller "
        "-o jsonpath='{.items[0].status.phase}'"
    )
    results["pod_running"] = pod_running == "Running"

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

    total = len(results)
    passed = sum(results.values())
    score = passed / total

    weights = {k: 1 / total for k in results}
    feedback = "\n".join(f"{k}: {'PASS' if v else 'FAIL'}" for k, v in results.items())

    return GradeResult(score, results, weights, feedback)