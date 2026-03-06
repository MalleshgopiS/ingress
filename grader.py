import subprocess
import re
import time

NS = "default"
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

    # 🎯 Calibrated weights (sum = 1.0, mean ≈ 0.5)
    weights = {
        "timeout_fixed": 0.20,
        "config_live": 0.20,
        "rollout_done": 0.15,
        "deployment_ready": 0.10,
        "https_ok": 0.15,
        "health_ok": 0.10,
        "stable": 0.05,
        "image_unchanged": 0.05,
    }

    # 1️⃣ ConfigMap contains fix
    cfg = run(
        f"kubectl get configmap ingress-nginx-config -n {NS} "
        "-o jsonpath='{.data.nginx\\.conf}'"
    )
    results["timeout_fixed"] = bool(
        re.search(r"keepalive_timeout\s+65(s)?;", cfg)
    )

    # 2️⃣ Fix active in running pod
    pod = run(
        f"kubectl get pods -n {NS} -l app=ingress-controller "
        "-o jsonpath='{.items[0].metadata.name}'"
    )
    live = run(
        f"kubectl exec -n {NS} {pod} -- cat /etc/nginx/nginx.conf"
    )
    results["config_live"] = "keepalive_timeout 65" in live

    # 3️⃣ Rollout completed
    obs = run(
        f"kubectl get deploy {DEPLOY} -n {NS} "
        "-o jsonpath='{.status.observedGeneration}'"
    )
    gen = run(
        f"kubectl get deploy {DEPLOY} -n {NS} "
        "-o jsonpath='{.metadata.generation}'"
    )
    results["rollout_done"] = obs == gen

    # 4️⃣ Deployment healthy
    ready = run(
        f"kubectl get deploy {DEPLOY} -n {NS} "
        "-o jsonpath='{.status.readyReplicas}'"
    )
    results["deployment_ready"] = ready == "1"

    # 5️⃣ HTTPS functional
    ip = run(
        f"kubectl get svc {DEPLOY} -n {NS} "
        "-o jsonpath='{.spec.clusterIP}'"
    )

    https_ok = False
    for _ in range(5):
        r = run(f"curl -k -s https://{ip}")
        if "Ingress Controller Running" in r:
            https_ok = True
            break
        time.sleep(2)
    results["https_ok"] = https_ok

    # 6️⃣ Health endpoint
    health_ok = False
    for _ in range(5):
        r = run(f"curl -k -s https://{ip}/healthz")
        if "ok" in r.lower():
            health_ok = True
            break
        time.sleep(2)
    results["health_ok"] = health_ok

    # 7️⃣ Stability under load
    restarts_before = run(
        f"kubectl get pod {pod} -n {NS} "
        "-o jsonpath='{.status.containerStatuses[0].restartCount}'"
    )

    for _ in range(10):
        run(f"curl -k -s https://{ip} > /dev/null")
        time.sleep(1)

    restarts_after = run(
        f"kubectl get pod {pod} -n {NS} "
        "-o jsonpath='{.status.containerStatuses[0].restartCount}'"
    )

    results["stable"] = restarts_before == restarts_after

    # 8️⃣ Image unchanged
    image = run(
        f"kubectl get deploy {DEPLOY} -n {NS} "
        "-o jsonpath='{.spec.template.spec.containers[0].image}'"
    )
    results["image_unchanged"] = "nginx" in image

    # Final score
    score = sum(weights[k] for k, v in results.items() if v)

    feedback = "\n".join(
        f"{k}: {'PASS' if v else 'FAIL'} (w={weights[k]})"
        for k, v in results.items()
    )

    return GradeResult(score, results, weights, feedback)