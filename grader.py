import subprocess
import time
import re

NS = "ingress-system"
DEPLOY = "ingress-controller"


def run(cmd):
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    return r.stdout.strip()


class GradeResult:
    def __init__(self, score, subscores, weights, feedback):
        self.score = score
        self.subscores = subscores
        self.weights = weights
        self.feedback = feedback


def grade(context=None):
    results = {}

    # Core fix weighted higher
    weights = {
        "timeout_fixed": 0.30,
        "config_live": 0.20,
        "rollout_done": 0.15,
        "deployment_ready": 0.10,
        "https_ok": 0.10,
        "health_ok": 0.05,
        "stable": 0.05,
        "image_unchanged": 0.05
    }

    # 1. ConfigMap fix
    config = run(
        f"kubectl get configmap ingress-nginx-config -n {NS} "
        "-o jsonpath='{.data.nginx\\.conf}'"
    )
    results["timeout_fixed"] = bool(
        re.search(r"keepalive_timeout\s+65(s)?;", config)
    )

    # 2. Config applied in pod
    pod = run(
        f"kubectl get pods -n {NS} -l app=ingress-controller "
        "-o jsonpath='{.items[0].metadata.name}'"
    )
    active = run(f"kubectl exec -n {NS} {pod} -- cat /etc/nginx/nginx.conf")
    results["config_live"] = "keepalive_timeout 65" in active

    # 3. Rollout happened
    observed = run(
        f"kubectl get deployment {DEPLOY} -n {NS} "
        "-o jsonpath='{.status.observedGeneration}'"
    )
    meta = run(
        f"kubectl get deployment {DEPLOY} -n {NS} "
        "-o jsonpath='{.metadata.generation}'"
    )
    results["rollout_done"] = observed == meta

    # 4. Deployment ready
    ready = run(
        f"kubectl get deployment {DEPLOY} -n {NS} "
        "-o jsonpath='{.status.readyReplicas}'"
    )
    results["deployment_ready"] = ready == "1"

    # 5. HTTPS checks
    svc_ip = run(
        f"kubectl get svc {DEPLOY} -n {NS} "
        "-o jsonpath='{.spec.clusterIP}'"
    )

    https_ok = False
    health_ok = False
    for _ in range(5):
        root = run(f"curl -ks https://{svc_ip}/")
        health = run(f"curl -ks https://{svc_ip}/healthz")
        if "Ingress Controller Running" in root:
            https_ok = True
        if "ok" in health.lower():
            health_ok = True
        if https_ok and health_ok:
            break
        time.sleep(2)

    results["https_ok"] = https_ok
    results["health_ok"] = health_ok

    # 6. Stability
    before = run(
        f"kubectl get pod -n {NS} {pod} "
        "-o jsonpath='{{.status.containerStatuses[0].restartCount}}'"
    )
    for _ in range(10):
        run(f"curl -ks https://{svc_ip}/ > /dev/null")
        time.sleep(1)
    after = run(
        f"kubectl get pod -n {NS} {pod} "
        "-o jsonpath='{{.status.containerStatuses[0].restartCount}}'"
    )
    results["stable"] = before == after

    # 7. Image unchanged
    image = run(
        f"kubectl get deployment {DEPLOY} -n {NS} "
        "-o jsonpath='{.spec.template.spec.containers[0].image}'"
    )
    results["image_unchanged"] = "nginx" in image

    score = sum(weights[k] for k, v in results.items() if v)

    feedback = "\n".join(
        f"{k}: {'PASS' if v else 'FAIL'} (w={weights[k]})"
        for k, v in results.items()
    )

    return GradeResult(score, results, weights, feedback)