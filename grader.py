#!/usr/bin/env python3
import subprocess
import re
from pathlib import Path

def run(cmd):
    return subprocess.check_output(cmd, shell=True, text=True).strip()

def safe_run(cmd):
    try:
        return run(cmd)
    except:
        return ""

def grade():
    results = {}

    # 1️⃣ Deployment UID preserved
    old_uid = Path("/grader/original_uid").read_text().strip()
    new_uid = safe_run("kubectl get deploy ingress-controller -o jsonpath='{.metadata.uid}'").strip("'")
    results["uid_preserved"] = (old_uid == new_uid)

    # 2️⃣ Container image unchanged
    image = safe_run("kubectl get deploy ingress-controller -o jsonpath='{.spec.template.spec.containers[0].image}'").strip("'")
    results["image_correct"] = (image == "nginx:alpine")

    # 3️⃣ Memory limit unchanged
    memory = safe_run("kubectl get deploy ingress-controller -o jsonpath='{.spec.template.spec.containers[0].resources.limits.memory}'").strip("'")
    results["memory_correct"] = (memory == "128Mi")

    # 4️⃣ keepalive_timeout fixed (strict → natural partial failures)
    config = safe_run("kubectl get configmap ingress-nginx-config -o jsonpath='{.data.nginx.conf}'").strip("'")
    results["timeout_fixed"] = bool(re.search(r"keepalive_timeout 65;", config))

    # 5️⃣ Pod restarted
    old_pod_uid = Path("/grader/original_pod_uid").read_text().strip()
    new_pod_uid = safe_run("kubectl get pods -l app=ingress-controller -o jsonpath='{.items[0].metadata.uid}'").strip("'")
    results["pod_restarted"] = (old_pod_uid != new_pod_uid)

    # 6️⃣ Deployment ready
    ready = safe_run("kubectl get deploy ingress-controller -o jsonpath='{.status.readyReplicas}'").strip("'")
    results["deployment_ready"] = (ready == "1")

    total = len(results)
    passed = sum(1 for v in results.values() if v)
    score = passed / total

    feedback = "\n".join(
        f"{k}: {'PASS' if v else 'FAIL'}" for k, v in results.items()
    )

    return {
        "score": score,
        "subscores": results,
        "weights": {k: 1/total for k in results},
        "feedback": feedback
    }

if __name__ == "__main__":
    import json
    print(json.dumps(grade()))