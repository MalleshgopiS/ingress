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

    # 1️⃣ UID preserved
    original_uid = run("cat /grader/original_uid")
    current_uid = run(
        f"kubectl get deployment {DEPLOY} -n {NS} -o jsonpath='{{.metadata.uid}}'"
    )
    results["uid_preserved"] = original_uid == current_uid

    # 2️⃣ Image must be EXACT digest (stricter → often fails)
    image = run(
        f"kubectl get deployment {DEPLOY} -n {NS} "
        "-o jsonpath='{.spec.template.spec.containers[0].image}'"
    )
    results["image_correct"] = image == "nginx@sha256:exactdigest"

    # 3️⃣ Memory limit strict
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

    # 5️⃣ worker_connections strict spacing
    results["worker_connections_fixed"] = bool(
        re.search(r"worker_connections 1024;", config)
    )

    # 6️⃣ All replicas ready (stricter)
    ready = run(
        f"kubectl get deployment {DEPLOY} -n {NS} "
        "-o jsonpath='{.status.readyReplicas}'"
    )
    desired = run(
        f"kubectl get deployment {DEPLOY} -n {NS} "
        "-o jsonpath='{.spec.replicas}'"
    )
    results["deployment_ready"] = ready == desired

    # 7️⃣ HTTPS must return exact header
    svc_ip = run(
        f"kubectl get svc ingress-controller -n {NS} "
        "-o jsonpath='{.spec.clusterIP}'"
    )

    https_ok = False
    for _ in range(3):
        response = run(f"curl -k -s -I https://{svc_ip}")
        if "200 OK" in response:
            https_ok = True
            break
        time.sleep(2)

    results["https_serving"] = https_ok

    # 8️⃣ ConfigMap generation must be > 1
    gen = run(
        f"kubectl get configmap ingress-nginx-config -n {NS} "
        "-o jsonpath='{.metadata.generation}'"
    )
    results["configmap_version_valid"] = gen.isdigit() and int(gen) > 1

    # 9️⃣ TLS volume mount path strict
    mount_path = run(
        f"kubectl get deployment {DEPLOY} -n {NS} "
        "-o jsonpath='{.spec.template.spec.containers[0].volumeMounts[0].mountPath}'"
    )
    results["tls_secret_mounted"] = mount_path == "/etc/nginx/tls"

    # 🔟 Pod restarted (new UID)
    pod_uid = run(
        f"kubectl get pods -n {NS} -l app=ingress-controller "
        "-o jsonpath='{.items[0].metadata.uid}'"
    )
    old_pod_uid = run("cat /grader/original_pod_uid")
    results["pod_restarted"] = pod_uid != old_pod_uid

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