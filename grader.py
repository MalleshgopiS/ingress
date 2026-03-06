import subprocess
import re
import time

NS = "default"
DEPLOY = "ingress-controller"
CM = "ingress-nginx-config"


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

    checks = [
        "timeout_fixed",
        "revision_changed",
        "deployment_ready",
        "https_serving",
        "rollout_stable",
        "config_structure_valid",
        "service_endpoints_ready",
        "pod_ready",
    ]

    weight_each = 1.0 / len(checks)
    weights = {k: weight_each for k in checks}

    # 1. Config fixed
    config = run(f"kubectl get configmap {CM} -n {NS} -o jsonpath='{{.data.nginx\\.conf}}'")
    results["timeout_fixed"] = bool(re.search(r"keepalive_timeout\s+65;", config))

    # 2. Rollout happened
    revision = run(f"kubectl get deployment {DEPLOY} -n {NS} -o jsonpath='{{.metadata.annotations.deployment\\.kubernetes\\.io/revision}}'")
    results["revision_changed"] = revision != "1"

    # 3. Deployment ready
    ready = run(f"kubectl get deployment {DEPLOY} -n {NS} -o jsonpath='{{.status.readyReplicas}}'")
    results["deployment_ready"] = ready == "1"

    # 4. HTTPS works
    svc_ip = run(f"kubectl get svc {DEPLOY} -n {NS} -o jsonpath='{{.spec.clusterIP}}'")
    https_ok = False
    for _ in range(5):
        resp = run(f"curl -k -s https://{svc_ip}")
        if "Ingress Controller Running" in resp:
            https_ok = True
            break
        time.sleep(2)
    results["https_serving"] = https_ok

    # 5. Stable rollout
    time.sleep(5)
    unavailable = run(f"kubectl get deployment {DEPLOY} -n {NS} -o jsonpath='{{.status.unavailableReplicas}}'")
    results["rollout_stable"] = unavailable in ["", "0", "<no value>"]

    # 6. Config structure preserved
    results["config_structure_valid"] = "http {" in config and "server {" in config

    # 7. Service endpoints populated
    endpoints = run(f"kubectl get endpoints {DEPLOY} -n {NS} -o jsonpath='{{.subsets[*].addresses[*].ip}}'")
    results["service_endpoints_ready"] = len(endpoints.strip()) > 0

    # 8. Pod ready
    pod_ready = run(f"kubectl get pods -n {NS} -l app=ingress-controller -o jsonpath='{{.items[0].status.containerStatuses[0].ready}}'")
    results["pod_ready"] = pod_ready == "true"

    score = sum(weights[k] for k, v in results.items() if v)

    feedback = "\n".join(f"{k}: {'PASS' if v else 'FAIL'} (w={weights[k]:.3f})" for k, v in results.items())

    return GradeResult(score, results, weights, feedback)