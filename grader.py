#!/usr/bin/env python3
import json
import subprocess
import time
import re

NS = "ingress-system"


def run(cmd):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True).stdout.strip()


def https_ok():
    for _ in range(5):
        out = run("curl -sk https://ingress-controller/ || true")
        if "Ingress Controller Running" in out:
            return True
        time.sleep(2)
    return False


def health_ok():
    for _ in range(5):
        out = run("curl -sk https://ingress-controller/healthz || true")
        if "ok" in out.lower():
            return True
        time.sleep(2)
    return False


def get_restart_count(pod):
    return run(
        f"kubectl get pod {pod} -n {NS} "
        "-o jsonpath='{.status.containerStatuses[0].restartCount}'"
    )


def grade():
    results = {}

    # Pod name
    pod = run(
        f"kubectl get pods -n {NS} "
        "-l app=ingress-controller "
        "-o jsonpath='{.items[0].metadata.name}'"
    )

    # 1. ConfigMap fix
    cfg = run(
        f"kubectl get configmap ingress-nginx-config -n {NS} "
        "-o jsonpath='{.data.nginx\\.conf}'"
    )
    results["timeout_fixed"] = bool(
        re.search(r"keepalive_timeout\s+65(s)?;", cfg)
    )

    # 2. Fix active inside pod
    active = run(
        f"kubectl exec -n {NS} {pod} -- "
        "cat /etc/nginx/nginx.conf || true"
    )
    results["config_live"] = bool(
        re.search(r"keepalive_timeout\s+65(s)?;", active)
    )

    # 3. Rollout completed
    observed = run(
        f"kubectl get deploy ingress-controller -n {NS} "
        "-o jsonpath='{.status.observedGeneration}'"
    )
    meta = run(
        f"kubectl get deploy ingress-controller -n {NS} "
        "-o jsonpath='{.metadata.generation}'"
    )
    results["rollout_done"] = observed == meta

    # 4. Deployment healthy
    ready = run(
        f"kubectl get deploy ingress-controller -n {NS} "
        "-o jsonpath='{.status.readyReplicas}'"
    )
    results["deployment_ready"] = ready == "1"

    # 5. HTTPS works
    results["https_ok"] = https_ok()

    # 6. Health endpoint
    results["health_ok"] = health_ok()

    # 7. Stability
    before = get_restart_count(pod)
    for _ in range(10):
        run("curl -sk https://ingress-controller/ >/dev/null 2>&1 || true")
        time.sleep(1)
    after = get_restart_count(pod)
    results["stable"] = before == after

    # 8. Image unchanged
    image = run(
        f"kubectl get deploy ingress-controller -n {NS} "
        "-o jsonpath='{.spec.template.spec.containers[0].image}'"
    )
    results["image_unchanged"] = "nginx" in image

    # 🎯 Calibrated weights
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

    score = sum(weights[k] for k in results if results[k])

    print(json.dumps({
        "score": round(score, 3),
        "subscores": results,
        "weights": weights
    }))


if __name__ == "__main__":
    grade()