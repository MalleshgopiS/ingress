import base64
import io
import re
import subprocess
import tarfile
import time
from pathlib import Path

NS = "ingress-system"
SERVICE = "ingress-controller"
DEPLOYMENT = "ingress-controller"
ACTIVE_SECRET = "edge-runtime-assets"
EXPECTED_IMAGE = "nginx:alpine"
EXPECTED_MEMORY = "128Mi"
ORIGINAL_UID_FILE = Path("/grader/original_uid")
ORIGINAL_SPEC_FILE = Path("/grader/original_spec_fingerprint")


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
    if not ORIGINAL_UID_FILE.exists() or not ORIGINAL_SPEC_FILE.exists():
        return False, "missing original deployment fingerprints"

    original_uid = ORIGINAL_UID_FILE.read_text(encoding="utf-8").strip()
    original_spec = ORIGINAL_SPEC_FILE.read_text(encoding="utf-8").strip()
    current_uid = jsonpath(f"deployment/{DEPLOYMENT}", "{.metadata.uid}")
    current_spec = jsonpath(
        f"deployment/{DEPLOYMENT}",
        "{.spec.template.spec.initContainers[0].command}{\"\\n\"}{.spec.template.spec.containers[0].command}{\"\\n\"}{.spec.template.spec.containers[0].args}{\"\\n\"}{.spec.template.spec.volumes}",
    )
    image = jsonpath(f"deployment/{DEPLOYMENT}", "{.spec.template.spec.containers[0].image}")
    memory = jsonpath(
        f"deployment/{DEPLOYMENT}",
        "{.spec.template.spec.containers[0].resources.limits.memory}",
    )

    if original_uid != current_uid:
        return False, "deployment UID changed"
    if current_spec != original_spec:
        return False, "deployment runtime spec changed"
    if image != EXPECTED_IMAGE:
        return False, f"image changed to {image!r}"
    if memory != EXPECTED_MEMORY:
        return False, f"memory limit changed to {memory!r}"
    return True, "guardrails preserved"


def decoded_bundle_members():
    secret_payload = jsonpath(f"secret/{ACTIVE_SECRET}", "{.data.runtime\\.bin}")
    if not secret_payload:
        return {}
    data = base64.b64decode(secret_payload)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        members = {}
        for member in archive.getmembers():
            if not member.isfile():
                continue
            extracted = archive.extractfile(member)
            if extracted is None:
                continue
            members[Path(member.name).name] = extracted.read().decode("utf-8")
    return members


def bundle_integrity_preserved():
    members = decoded_bundle_members()
    if set(members) != {"profile.env", "nginx.tmpl", "render.py"}:
        return False
    profile = members.get("profile.env", "")
    required_lines = [
        "TLS_PORT=443",
        "SERVER_NAME=_",
        "UPSTREAM_HOST=ingress-backend.ingress-system.svc.cluster.local",
        "UPSTREAM_PORT=8080",
        "ROOT_PATH=/",
        "HEALTH_PATH=/healthz",
        "KEEPALIVE_REQUESTS=1000",
        "SSL_SESSION_TIMEOUT=10m",
        "WATCHDOG_MATCH=keepalive_timeout 0;",
        "WATCHDOG_DELAY_SECONDS=12",
        "PROFILE_NAME=blue-edge",
        "PROFILE_OWNER=platform-network",
    ]
    return all(line in profile for line in required_lines)


def active_bundle_fixed():
    members = decoded_bundle_members()
    profile = members.get("profile.env", "")
    return "KEEPALIVE_TIMEOUT=65" in profile and "KEEPALIVE_TIMEOUT=0" not in profile


def rendered_runtime_fixed():
    pod = jsonpath("pods -l app=ingress-controller", "{.items[0].metadata.name}")
    if not pod:
        return False
    _, rendered_conf, _ = run(
        f"kubectl exec -n {NS} {pod} -- cat /etc/nginx/nginx.conf"
    )
    _, watchdog, _ = run(f"kubectl exec -n {NS} {pod} -- cat /watchdog.sh")
    revision = jsonpath(
        f"deployment/{DEPLOYMENT}",
        "{.metadata.annotations.deployment\\.kubernetes\\.io/revision}",
    )
    return (
        bool(re.search(r"keepalive_timeout\s+65;", rendered_conf))
        and "keepalive_timeout 0;" not in watchdog
        and revision not in ["", "1"]
        and deployment_ready()
    )


def controller_stable_after_idle():
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
            "rendered_runtime_fixed": False,
            "active_bundle_fixed": False,
            "bundle_integrity_preserved": False,
        }
        weights = {
            "root_https_stable": 0.15,
            "health_https_stable": 0.15,
            "controller_stable_after_idle": 0.15,
            "rendered_runtime_fixed": 0.10,
            "active_bundle_fixed": 0.25,
            "bundle_integrity_preserved": 0.20,
        }
        return GradeResult(0.0, zero_checks, weights, guardrail_feedback)

    checks = {
        "root_https_stable": stable_endpoint("/", "Ingress Controller Running"),
        "health_https_stable": stable_endpoint("/healthz", '"status":"ok"'),
        "controller_stable_after_idle": controller_stable_after_idle(),
        "rendered_runtime_fixed": rendered_runtime_fixed(),
        "active_bundle_fixed": active_bundle_fixed(),
        "bundle_integrity_preserved": bundle_integrity_preserved(),
    }
    weights = {
        "root_https_stable": 0.15,
        "health_https_stable": 0.15,
        "controller_stable_after_idle": 0.15,
        "rendered_runtime_fixed": 0.10,
        "active_bundle_fixed": 0.25,
        "bundle_integrity_preserved": 0.20,
    }
    score = sum(weights[name] for name, passed in checks.items() if passed)
    feedback = guardrail_feedback + " | " + " | ".join(
        f"{name}: {'PASS' if passed else 'FAIL'}" for name, passed in checks.items()
    )
    return GradeResult(score, checks, weights, feedback)