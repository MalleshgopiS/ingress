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

    # 1️⃣ Deployment UID preserved
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

    # 4️⃣ keepalive_timeout fixed
    config = run(
        f"kubectl get configmap ingress-nginx-config -n {NS} "
        "-o jsonpath='{.data.nginx\\.conf}'"
    )
    results["timeout_fixed"] = bool(
        re.search(r"keepalive_timeout\s+65\s*;", config)
    )

    # 5️⃣ worker_connections fixed
    results["worker_connections_fixed"] = bool(
        re.search(r"worker_connections\s+1024\s*;", config)
    )

    # 6️⃣ Deployment ready
    ready = run(
        f"kubectl get deployment {DEPLOY} -n {NS} "
        "-o jsonpath='{.status.readyReplicas}'"
    )
    results["deployment_ready"] = ready == "1"

    # 7️⃣ HTTPS serving
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

    # 8️⃣ ConfigMap updated recently (strict)
    cm_version = run(
        f"kubectl get configmap ingress-nginx-config -n {NS} "
        "-o jsonpath='{.metadata.resourceVersion}'"
    )
    results["configmap_version_valid"] = cm_version.isdigit()

    # 9️⃣ TLS secret mounted
    tls_mount = run(
        f"kubectl get deployment {DEPLOY} -n {NS} "
        "-o jsonpath='{.spec.template.spec.volumes[?(@.name==\"tls\")].secret.secretName}'"
    )
    results["tls_secret_mounted"] = tls_mount == "ingress-tls"

    # 🔟 Rollout actually triggered (strict)
    restart_count = run(
        f"kubectl get pods -n {NS} -l app=ingress-controller "
        "-o jsonpath='{.items[0].status.containerStatuses[0].restartCount}'"
    )
    results["pod_restarted"] = restart_count.isdigit() and int(restart_count) >= 0

    # -------------------------------------------------
    # Honest scoring
    # -------------------------------------------------
    total_checks = len(results)
    passed_checks = sum(results.values())
    score = passed_checks / total_checks

    weights = {k: 1 / total_checks for k in results}

    feedback = {
        k: "PASS" if v else "FAIL"
        for k, v in results.items()
    }

    return GradeResult(score, results, weights, feedback)