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
        f"kubectl get deployment {DEPLOY} -n {NS} "
        "-o jsonpath='{.metadata.uid}'"
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
        re.search(r"keepalive_timeout\s+65;", config)
    )

    # 5️⃣ Deployment ready
    ready = run(
        f"kubectl get deployment {DEPLOY} -n {NS} "
        "-o jsonpath='{.status.readyReplicas}'"
    )
    results["deployment_ready"] = ready == "1"

    # 6️⃣ Pod restarted
    original_pod_uid = run("cat /grader/original_pod_uid")
    current_pod_uid = run(
        f"kubectl get pods -n {NS} -l app=ingress-controller "
        "-o jsonpath='{.items[0].metadata.uid}'"
    )
    results["pod_restarted"] = original_pod_uid != current_pod_uid

    # 7️⃣ Service exists
    svc = run(
        f"kubectl get svc ingress-controller -n {NS} "
        "-o jsonpath='{.metadata.name}'"
    )
    results["service_exists"] = svc == "ingress-controller"

    # 8️⃣ HTTPS responding (often flaky)
    svc_ip = run(
        f"kubectl get svc ingress-controller -n {NS} "
        "-o jsonpath='{.spec.clusterIP}'"
    )

    https_ok = False
    for _ in range(3):
        response = run(f"curl -k -s --max-time 2 https://{svc_ip}")
        if "Ingress Controller Running" in response:
            https_ok = True
            break
        time.sleep(1)

    results["https_serving"] = https_ok

    # 9️⃣ ConfigMap mounted
    mount = run(
        f"kubectl get pod -n {NS} -l app=ingress-controller "
        "-o jsonpath='{.items[0].spec.volumes[?(@.configMap)].configMap.name}'"
    )
    results["config_mounted"] = "ingress-nginx-config" in mount

    # 🔟 TLS secret mounted (often fails)
    tls_mount = run(
        f"kubectl get pod -n {NS} -l app=ingress-controller "
        "-o jsonpath='{.items[0].spec.volumes[?(@.secret)].secret.secretName}'"
    )
    results["tls_mounted"] = "ingress-tls" in tls_mount

    # ===== SCORING =====
    total = len(results)
    passed = sum(results.values())
    mean_score = passed / total

    weights = {k: 1 / total for k in results}

    feedback = "\n".join(
        f"{k}: {'PASS' if v else 'FAIL'}"
        for k, v in results.items()
    )

    return GradeResult(mean_score, results, weights, feedback)