import re
import subprocess
import time
from pathlib import Path

NS = "ingress-system"
SERVICE = "ingress-controller"
DEPLOYMENT = "ingress-controller"
ACTIVE_PROFILE_CONFIGMAP = "gateway-bootstrap-profile"
ACTIVE_PROFILE_KEY = "gateway.env"
EXPECTED_IMAGE = "nginx:alpine"
EXPECTED_MEMORY = "128Mi"
ORIGINAL_UID_FILE = Path("/grader/original_uid")


class GradeResult:
    def __init__(self, score, subscores, weights, feedback):
        self.score = score
        self.subscores = subscores
        self.weights = weights
        self.feedback = feedback


def run(cmd):
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    return result.returncode, result.stdout.strip(), result.stderr.strip()


def jsonpath(resource, path):
    _, stdout, _ = run(f"kubectl get {resource} -n {NS} -o jsonpath='{path}'")
    return stdout


def service_ip():
    return jsonpath(f"svc/{SERVICE}", "{.spec.clusterIP}")


def https_get(path):
    ip = service_ip()
    if not ip:
        return False, ""
    code, stdout, _ = run(f"curl -k -sS --http1.1 --max-time 5 https://{ip}{path}")
    return code == 0, stdout


def stable_endpoint(path, expected, attempts=5, delay=5):
    """Return True only if an HTTPS endpoint serves the expected payload repeatedly."""
    for _ in range(attempts):
        ok, body = https_get(path)
        if not ok or expected not in body:
            return False
        time.sleep(delay)
    return True


def deployment_ready():
    ready = jsonpath(f"deployment/{DEPLOYMENT}", "{.status.readyReplicas}")
    unavailable = jsonpath(f"deployment/{DEPLOYMENT}", "{.status.unavailableReplicas}")
    return ready == "1" and unavailable in ["", "0", "<no value>"]


def guardrails_ok():
    """Reject fixes that replace the deployment or mutate protected runtime guardrails."""
    if not ORIGINAL_UID_FILE.exists():
        return False, "missing original deployment UID"

    original_uid = ORIGINAL_UID_FILE.read_text(encoding="utf-8").strip()
    current_uid = jsonpath(f"deployment/{DEPLOYMENT}", "{.metadata.uid}")
    image = jsonpath(f"deployment/{DEPLOYMENT}", "{.spec.template.spec.containers[0].image}")
    memory = jsonpath(
        f"deployment/{DEPLOYMENT}",
        "{.spec.template.spec.containers[0].resources.limits.memory}",
    )

    if original_uid != current_uid:
        return False, "deployment UID changed"
    if image != EXPECTED_IMAGE:
        return False, f"image changed to {image!r}"
    if memory != EXPECTED_MEMORY:
        return False, f"memory limit changed to {memory!r}"
    return True, "guardrails preserved"


def active_profile_text():
    return jsonpath(
        f"configmap/{ACTIVE_PROFILE_CONFIGMAP}",
        "{.data.gateway\\.env}",
    )


def bootstrap_bundle_preserved():
    """Ensure the active bootstrap profile still contains the full runtime bundle."""
    text = active_profile_text()
    required_lines = [
        "TLS_PORT=443",
        "SERVER_NAME=_",
        "UPSTREAM_HOST=ingress-backend.ingress-system.svc.cluster.local",
        "UPSTREAM_PORT=8080",
        "ROOT_PATH=/",
        "HEALTH_PATH=/healthz",
        "KEEPALIVE_REQUESTS=1000",
        "SSL_SESSION_TIMEOUT=10m",
        "PROFILE_NAME=blue-edge",
        "PROFILE_OWNER=platform-network",
    ]
    return all(line in text for line in required_lines)


def runtime_fix_applied():
    """Verify the active bootstrap profile and rendered nginx config both contain the real fix."""
    profile = active_profile_text()
    revision = jsonpath(
        f"deployment/{DEPLOYMENT}",
        "{.metadata.annotations.deployment\\.kubernetes\\.io/revision}",
    )
    pod = jsonpath("pods -l app=ingress-controller", "{.items[0].metadata.name}")
    if not pod:
        return False
    _, rendered_conf, _ = run(
        f"kubectl exec -n {NS} {pod} -- cat /etc/nginx/nginx.conf"
    )
    return (
        "GATEWAY_IDLE_SECONDS=65" in profile
        and bool(re.search(r"keepalive_timeout\s+65;", rendered_conf))
        and revision not in ["", "1"]
        and deployment_ready()
    )


def controller_stable_after_idle():
    """Ensure the controller does not restart during an idle window and still serves HTTPS."""
    pod = jsonpath("pods -l app=ingress-controller", "{.items[0].metadata.name}")
    if not pod:
        return False

    before = jsonpath(f"pod/{pod}", "{.status.containerStatuses[0].restartCount}")
    time.sleep(18)
    after_pod = jsonpath("pods -l app=ingress-controller", "{.items[0].metadata.name}")
    after = jsonpath(f"pod/{after_pod}", "{.status.containerStatuses[0].restartCount}") if after_pod else ""

    if pod != after_pod or before == "" or after == "" or before != after:
        return False

    ok_root, root_body = https_get("/")
    ok_health, health_body = https_get("/healthz")
    return (
        ok_root
        and "Ingress Controller Running" in root_body
        and ok_health
        and '"status":"ok"' in health_body
    )


def grade(context=None):
    guardrails_passed, guardrail_feedback = guardrails_ok()
    if not guardrails_passed:
        zero_checks = {
            "root_https_stable": False,
            "health_https_stable": False,
            "controller_stable_after_idle": False,
            "runtime_fix_applied": False,
            "bootstrap_bundle_preserved": False,
        }
        weights = {name: 0.20 for name in zero_checks}
        return GradeResult(0.0, zero_checks, weights, guardrail_feedback)

    checks = {
        "root_https_stable": stable_endpoint("/", "Ingress Controller Running"),
        "health_https_stable": stable_endpoint("/healthz", '"status":"ok"'),
        "controller_stable_after_idle": controller_stable_after_idle(),
        "runtime_fix_applied": runtime_fix_applied(),
        "bootstrap_bundle_preserved": bootstrap_bundle_preserved(),
    }
    weights = {name: 0.20 for name in checks}
    score = sum(weights[name] for name, passed in checks.items() if passed)
    feedback = guardrail_feedback + " | " + " | ".join(
        f"{name}: {'PASS' if passed else 'FAIL'}" for name, passed in checks.items()
    )
    return GradeResult(score, checks, weights, feedback)