#!/usr/bin/env python3
import subprocess
import re
from pathlib import Path
from apex_arena.grading import Grade

def run(cmd):
    return subprocess.check_output(cmd, shell=True, text=True).strip()

def safe_run(cmd):
    try:
        return run(cmd)
    except:
        return ""

def grade(context=None):
    checks = {}

    # 1️⃣ Deployment UID preserved
    old_uid = Path("/grader/original_uid").read_text().strip()
    new_uid = safe_run("kubectl get deploy ingress-controller -o jsonpath='{.metadata.uid}'").strip("'")
    checks["uid_preserved"] = (old_uid == new_uid)

    # 2️⃣ Image unchanged
    image = safe_run("kubectl get deploy ingress-controller -o jsonpath='{.spec.template.spec.containers[0].image}'").strip("'")
    checks["image_correct"] = (image == "nginx:alpine")

    # 3️⃣ Memory unchanged
    memory = safe_run("kubectl get deploy ingress-controller -o jsonpath='{.spec.template.spec.containers[0].resources.limits.memory}'").strip("'")
    checks["memory_correct"] = (memory == "128Mi")

    # 4️⃣ Timeout fixed
    config = safe_run("kubectl get configmap ingress-nginx-config -o jsonpath='{.data.nginx.conf}'").strip("'")
    checks["timeout_fixed"] = bool(re.search(r"keepalive_timeout 65;", config))

    # 5️⃣ Pod restarted
    old_pod_uid = Path("/grader/original_pod_uid").read_text().strip()
    new_pod_uid = safe_run("kubectl get pods -l app=ingress-controller -o jsonpath='{.items[0].metadata.uid}'").strip("'")
    checks["pod_restarted"] = (old_pod_uid != new_pod_uid)

    # 6️⃣ Deployment ready
    ready = safe_run("kubectl get deploy ingress-controller -o jsonpath='{.status.readyReplicas}'").strip("'")
    checks["deployment_ready"] = (ready == "1")

    total = len(checks)
    passed = sum(checks.values())
    score = passed / total

    feedback = "\n".join(
        f"{k}: {'PASS' if v else 'FAIL'}" for k, v in checks.items()
    )

    return Grade(
        score=score,
        feedback=feedback
    )