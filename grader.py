import subprocess
import time
import re


NS = "aurora-ingress"
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

    # 🎯 Weighted so core fix matters most
    weights = {
        "timeout_fixed": 0.30,
        "config_applied_to_pod": 0.20,
        "rollout_completed": 0.15,
        "deployment_ready": 0.10,
        "https_root_ok": 0.10,
        "https_health_ok": 0.05,
        "stable_under_traffic": 0.05,
        "no_container_changes": 0.05
    }

    # -----------------------------
    # 1. Core config fix (major)
    # -----------------------------
    config = run(
        f"kubectl get configmap ingress-controller-config -n {NS} "
        "-o jsonpath='{.data.nginx\\.conf}'"
    )
    results["timeout_fixed"] = bool(
        re.search(r"keepalive_timeout\s+65(s)?;", config)
    )

    # -----------------------------
    # 2. Config really applied in pod (anti-cheat)
    # -----------------------------
    pod = run(
        f"kubectl get pods -n {NS} -l app=ingress-controller "
        "-o jsonpath='{.items[0].metadata.name}'"
    )
    active_conf = run(
        f"kubectl exec -n {NS} {pod} -- cat /etc/nginx/nginx.conf"
    )
    results["config_applied_to_pod"] = "keepalive_timeout 65" in active_conf

    # -----------------------------
    # 3. Rollout actually happened
    # -----------------------------
    observed = run(
        f"kubectl get deployment {DEPLOY} -n {NS} "
        "-o jsonpath='{.status.observedGeneration}'"
    )
    meta = run(
        f"kubectl get deployment {DEPLOY} -n {NS} "
        "-o jsonpath='{.metadata.generation}'"
    )
    results["rollout_completed"] = observed == meta

    # -----------------------------
    # 4. Deployment healthy
    # -----------------------------
    ready = run(
        f"kubectl get deployment {DEPLOY} -n {NS} "
        "-o jsonpath='{.status.readyReplicas}'"
    )
    results["deployment_ready"] = ready == "1"

    # -----------------------------
    # 5. HTTPS functional checks
    # -----------------------------
    svc_ip = run(
        f"kubectl get svc {DEPLOY} -n {NS} "
        "-o jsonpath='{.spec.clusterIP}'"
    )

    root_ok = False
    health_ok = False

    for _ in range(5):
        root = run(f"curl -ks https://{svc_ip}/")
        health = run(f"curl -ks https://{svc_ip}/healthz")
        if "Ingress Controller Running" in root:
            root_ok = True
        if "ok" in health.lower():
            health_ok = True
        if root_ok and health_ok:
            break
        time.sleep(2)

    results["https_root_ok"] = root_ok
    results["https_health_ok"] = health_ok

    # -----------------------------
    # 6. Stability under load
    # -----------------------------
    restarts_before = run(
        f"kubectl get pod -n {NS} {pod} "
        "-o jsonpath='{{.status.containerStatuses[0].restartCount}}'"
    )

    for _ in range(15):
        run(f"curl -ks https://{svc_ip}/ > /dev/null")
        time.sleep(1)

    restarts_after = run(
        f"kubectl get pod -n {NS} {pod} "
        "-o jsonpath='{{.status.containerStatuses[0].restartCount}}'"
    )

    results["stable_under_traffic"] = restarts_before == restarts_after

    # -----------------------------
    # 7. Anti-cheat: image unchanged
    # -----------------------------
    image = run(
        f"kubectl get deployment {DEPLOY} -n {NS} "
        "-o jsonpath='{.spec.template.spec.containers[0].image}'"
    )
    results["no_container_changes"] = "nginx" in image

    # -----------------------------
    # Final score
    # -----------------------------
    score = sum(weights[k] for k, v in results.items() if v)

    feedback = "\n".join(
        f"{k}: {'PASS' if v else 'FAIL'} (w={weights[k]})"
        for k, v in results.items()
    )

    return GradeResult(score, results, weights, feedback)