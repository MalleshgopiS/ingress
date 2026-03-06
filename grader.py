import subprocess
import re
import time

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


def grade(context=None):   # ✅ FIXED SIGNATURE
    results = {}

    # 🎯 Calibrated weights → mean ≈ 0.50
    weights = {
        "timeout_fixed": 0.20,
        "config_live": 0.20,
        "rollout_done": 0.15,
        "deployment_ready": 0.10,
        "https_ok": 0.15,
        "health_ok": 0.10,
        "stable": 0.05,
        "image_unchanged": 0.05
    }

    # 1. ConfigMap fix
    cfg = run(
        f"kubectl get configmap ingress-nginx-config -n {NS} "
        "-o jsonpath='{.data.nginx\\.conf}'"
    )
    results["timeout_fixed"] = bool(
        re.search(r"keepalive_timeout\s+65(s)?;", cfg)
    )

    # 2. Fix active in pod
    pod = run(
        f"kubectl get pods -n {NS} -l app=ingress-controller "
        "-o jsonpath='{.items[0].metadata.name}'"
    )
    active = run(
        f"kubectl exec -n {NS} {pod} -- cat /etc/nginx/nginx.conf"
    )
    results["config_live"] = bool(
        re.search(r"keepalive_timeout\s+65(s)?;", active)
    )

    # 3. Rollout completed
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

    # 5. HTTPS works
    svc_ip = run(
        f"kubectl get svc {DEPLOY} -n {NS} "
        "-o jsonpath='{.spec.clusterIP}'"
    )
    https_ok = False
    for _ in range(5):
        resp = run(f"curl -k -s https://{svc_ip}")
        if "Ingress Controller Running" in resp:
            https_ok = True
            break
        time.sleep(2)
    results["https_ok"] = https_ok

    # 6. Health endpoint
    health_ok = False
    for _ in range(5):
        resp = run(f"curl -k -s https://{svc_ip}/healthz")
        if "ok" in resp.lower():
            health_ok = True
            break
        time.sleep(2)
    results["health_ok"] = health_ok

    # 7. Stability check
    def get_restart_count():
        return run(
            f"kubectl get pod {pod} -n {NS} "
            "-o jsonpath='{.status.containerStatuses[0].restartCount}'"
        )

    before = get_restart_count()
    for _ in range(10):
        run(f"curl -k -s https://{svc_ip} > /dev/null")
        time.sleep(1)
    after = get_restart_count()
    results["stable"] = before == after

    # 8. Image unchanged
    image = run(
        f"kubectl get deployment {DEPLOY} -n {NS} "
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