#!/usr/bin/env python3
import subprocess
import json
import re
from pathlib import Path

def run(cmd):
    return subprocess.check_output(cmd, shell=True, text=True).strip()

def safe_run(cmd):
    try:
        return run(cmd)
    except:
        return ""

results = {}

# 1️⃣ Deployment UID preserved (not deleted/recreated)
old_uid = Path("/grader/original_uid").read_text().strip()
new_uid = safe_run("kubectl get deploy ingress-controller -o jsonpath='{.metadata.uid}'").strip("'")
results["uid_preserved"] = (old_uid == new_uid)

# 2️⃣ Container image unchanged
image = safe_run("kubectl get deploy ingress-controller -o jsonpath='{.spec.template.spec.containers[0].image}'").strip("'")
results["image_correct"] = (image == "nginx:alpine")

# 3️⃣ Memory limit unchanged
memory = safe_run("kubectl get deploy ingress-controller -o jsonpath='{.spec.template.spec.containers[0].resources.limits.memory}'").strip("'")
results["memory_correct"] = (memory == "128Mi")

# 4️⃣ keepalive_timeout fixed to 65 seconds
config = safe_run("kubectl get configmap ingress-nginx-config -o jsonpath='{.data.nginx.conf}'").strip("'")
results["timeout_fixed"] = bool(re.search(r"keepalive_timeout\s+65\s*;", config))

# 5️⃣ Pod restarted (UID changed)
old_pod_uid = Path("/grader/original_pod_uid").read_text().strip()
new_pod_uid = safe_run("kubectl get pods -l app=ingress-controller -o jsonpath='{.items[0].metadata.uid}'").strip("'")
results["pod_restarted"] = (old_pod_uid != new_pod_uid)

# 6️⃣ Deployment ready
ready = safe_run("kubectl get deploy ingress-controller -o jsonpath='{.status.readyReplicas}'").strip("'")
results["deployment_ready"] = (ready == "1")

# -----------------------------
# Stricter Quality Checks
# -----------------------------

# 7️⃣ ConfigMap formatting strict (often fails if spacing differs)
results["formatting_strict"] = bool(re.search(r"keepalive_timeout 65;", config))

# 8️⃣ Worker connections explicitly present (good config hygiene)
results["worker_connections_set"] = bool(re.search(r"worker_connections\s+\d+;", config))

# 9️⃣ ConfigMap updated (resource version changed)
rv = safe_run("kubectl get configmap ingress-nginx-config -o jsonpath='{.metadata.resourceVersion}'").strip("'")
results["config_updated"] = rv.isdigit() and int(rv) > 1

# 🔟 Pod has exactly 1 container (strict structure check)
container_count = safe_run("kubectl get deploy ingress-controller -o jsonpath='{.spec.template.spec.containers[*].name}'")
results["single_container"] = (len(container_count.split()) == 1)

# -----------------------------
# Scoring
# -----------------------------

total = len(results)
passed = sum(1 for v in results.values() if v)
score = passed / total

feedback_lines = []
for k, v in results.items():
    feedback_lines.append(f"{k}: {'PASS' if v else 'FAIL'}")

print(json.dumps({
    "score": score,
    "subscores": results,
    "weights": {k: 1/total for k in results},
    "feedback": "\n".join(feedback_lines)
}))