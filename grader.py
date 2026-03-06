import subprocess
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
    results["image_unchanged"] = image == "nginx:alpine"

    memory_limit = run(
        f"kubectl get deployment {DEPLOY} -n {NS} "
        "-o jsonpath='{.spec.template.spec.containers[0].resources.limits.memory}'"
    )
    results["memory_fixed"] = memory_limit == "256Mi"

    ready = run(
        f"kubectl get deployment {DEPLOY} -n {NS} "
        "-o jsonpath='{.status.readyReplicas}'"
    )
    results["deployment_ready"] = ready == "1"

    restarts = run(
        f"kubectl get pods -n {NS} -l app=ingress "
        "-o jsonpath='{.items[0].status.containerStatuses[0].restartCount}'"
    )
    results["no_crashloop"] = restarts == "0"

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

    results["https_working"] = https_ok

    weights = {
        "memory_fixed": 0.30,
        "deployment_ready": 0.20,
        "https_working": 0.20,
        "uid_preserved": 0.10,
        "image_unchanged": 0.10,
        "no_crashloop": 0.10,
    }

    weighted_score = sum(weights[k] for k, v in results.items() if v)
    final_score = 0.3 + (weighted_score * 0.4)

    feedback = "\n".join(
        f"{k}: {'PASS' if v else 'FAIL'}" for k, v in results.items()
    )

    return GradeResult(final_score, results, weights, feedback)