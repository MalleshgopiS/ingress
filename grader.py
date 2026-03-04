import subprocess
import re

NS = "ingress-system"
DEPLOY = "ingress-controller"


def run(cmd):
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    return result.stdout.strip()


class GradeResult:
    def __init__(self, score, details):
        self.score = score
        self.details = details


def grade():

    results = {}

    original_uid = run("cat /grader/original_uid")

    current_uid = run(
        f"kubectl get deployment {DEPLOY} -n {NS} -o jsonpath='{{.metadata.uid}}'"
    )

    results["uid_preserved"] = original_uid == current_uid

    image = run(
        f"kubectl get deploy {DEPLOY} -n {NS} -o jsonpath='{{.spec.template.spec.containers[0].image}}'"
    )

    results["image_correct"] = image == "nginx:1.25-alpine"

    memory = run(
        f"kubectl get deploy {DEPLOY} -n {NS} -o jsonpath='{{.spec.template.spec.containers[0].resources.limits.memory}}'"
    )

    results["memory_correct"] = memory == "128Mi"

    config = run(
        f"kubectl get configmap ingress-nginx-config -n {NS} -o jsonpath='{{.data.nginx\\.conf}}'"
    )

    results["timeout_fixed"] = bool(
        re.search(r"keepalive_timeout\s+65;", config)
    )

    ready = run(
        f"kubectl get deploy {DEPLOY} -n {NS} -o jsonpath='{{.status.readyReplicas}}'"
    )

    results["deployment_ready"] = ready == "1"

    svc_ip = run(
        f"kubectl get svc ingress-controller -n {NS} -o jsonpath='{{.spec.clusterIP}}'"
    )

    http = run(f"curl -k -s https://{svc_ip}")

    results["https_serving"] = "Ingress Controller Running" in http

    score = sum(results.values()) / len(results)

    return GradeResult(score, results)