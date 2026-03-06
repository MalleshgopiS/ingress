import re
import subprocess
import time

NS = "aurora-ingress"
SERVICE = "edge-gateway"
CONFIGMAP = "edge-gateway-config"
DEPLOYMENT = "edge-gateway"


def run(cmd):
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    return result.returncode, result.stdout.strip(), result.stderr.strip()


def kubectl_jsonpath(resource, jsonpath):
    _, stdout, _ = run(
        f"kubectl get {resource} -n {NS} -o jsonpath='{jsonpath}'"
    )
    return stdout


class GradeResult:
    def __init__(self, score, subscores, weights, feedback):
        self.score = score
        self.subscores = subscores
        self.weights = weights
        self.feedback = feedback


def https_get(path, include_headers=False, retries=8, delay=2):
    svc_ip = kubectl_jsonpath(f"svc/{SERVICE}", "{.spec.clusterIP}")
    if not svc_ip:
        return ""

    header_flag = "-D -" if include_headers else ""
    for _ in range(retries):
        code, stdout, _ = run(
            f"curl -k -sS --http1.1 {header_flag} --max-time 5 https://{svc_ip}{path}"
        )
        if code == 0 and stdout:
            return stdout
        time.sleep(delay)
    return ""


def deployment_ready():
    ready = kubectl_jsonpath(f"deployment/{DEPLOYMENT}", "{.status.readyReplicas}")
    unavailable = kubectl_jsonpath(
        f"deployment/{DEPLOYMENT}", "{.status.unavailableReplicas}"
    )
    return ready == "1" and unavailable in ["", "0", "<no value>"]


def grade(context=None):
    checks = [
        "dashboard_https_ok",
        "assets_https_ok",
        "api_https_ok",
        "connection_handling_ok",
    ]
    weight_each = 1.0 / len(checks)
    weights = {check: weight_each for check in checks}

    config = kubectl_jsonpath(f"configmap/{CONFIGMAP}", "{.data.nginx\\.conf}")

    dashboard_body = https_get("/")
    assets_body = https_get("/assets/app.js")
    api_body = https_get("/api/health")
    headers = https_get("/api/health", include_headers=True)
    headers_lower = headers.lower()

    results = {
        "dashboard_https_ok": "Bleater Dashboard" in dashboard_body,
        "assets_https_ok": "window.appLoaded = true;" in assets_body,
        "api_https_ok": '"status":"ok"' in api_body and '"service":"bleater-api"' in api_body,
        "connection_handling_ok": bool(
            re.search(r"keepalive_timeout\s+65;", config)
        )
        and (
            "connection: keep-alive" in headers_lower
            or "keep-alive: timeout=65" in headers_lower
        )
        and deployment_ready(),
    }

    score = sum(weights[name] for name, passed in results.items() if passed)
    feedback = "\n".join(
        f"{name}: {'PASS' if passed else 'FAIL'} (w={weights[name]:.2f})"
        for name, passed in results.items()
    )
    return GradeResult(score, results, weights, feedback)