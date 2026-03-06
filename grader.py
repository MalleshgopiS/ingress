#!/usr/bin/env python3
import base64
import hashlib
import io
import json
import re
import subprocess
import tarfile
import time
from pathlib import Path

NS = "ingress-system"
DEPLOYMENT = "ingress-controller"
SERVICE = "ingress-controller"
EXPECTED_IMAGE = "nginx:alpine"
EXPECTED_MEMORY = "128Mi"
SOURCE_SECRET = "edge-sync-cache"
ORIGINAL_UID_FILE = Path("/grader/original_uid")
ORIGINAL_SPEC_FILE = Path("/grader/original_spec_fingerprint")

TRACKED_BUNDLE_FILES = [
    "defaults.env",
    "profile-map.json",
    "profiles/edge-stable.env",
    "profiles/edge-shadow.env",
    "nginx.tmpl",
    "bootstrap.py",
    "guard.py",
]
EXPECTED_BUNDLE_FILES = set(TRACKED_BUNDLE_FILES + ["fingerprint.lock"])


def run(command: list[str]) -> str:
    """Run a command and return trimmed stdout, or an empty string on failure."""
    try:
        completed = subprocess.run(command, capture_output=True, text=True, check=True)
    except subprocess.CalledProcessError:
        return ""
    return completed.stdout.strip()


def jsonpath(resource: str, expression: str) -> str:
    """Read a Kubernetes resource field through kubectl jsonpath."""
    return run(["kubectl", "get", resource, "-n", NS, "-o", f"jsonpath={expression}"])


def deployment_ready() -> bool:
    """Return True only when the ingress-controller deployment is fully ready."""
    desired = jsonpath(f"deployment/{DEPLOYMENT}", "{.spec.replicas}")
    ready = jsonpath(f"deployment/{DEPLOYMENT}", "{.status.readyReplicas}")
    available = jsonpath(f"deployment/{DEPLOYMENT}", "{.status.availableReplicas}")
    return bool(desired) and desired == ready == available


def guardrails_ok() -> tuple[bool, str]:
    """Reject fixes that recreate the deployment or mutate protected runtime guardrails."""
    if not ORIGINAL_UID_FILE.exists() or not ORIGINAL_SPEC_FILE.exists():
        return False, "missing grader fingerprint files"

    original_uid = ORIGINAL_UID_FILE.read_text(encoding="utf-8").strip()
    original_spec = ORIGINAL_SPEC_FILE.read_text(encoding="utf-8").strip()
    current_uid = jsonpath(f"deployment/{DEPLOYMENT}", "{.metadata.uid}")
    current_spec = jsonpath(
        f"deployment/{DEPLOYMENT}",
        "{.spec.template.spec.initContainers[*].name}{\"\\n\"}{.spec.template.spec.initContainers[*].command}{\"\\n\"}{.spec.template.spec.initContainers[*].args}{\"\\n\"}{.spec.template.spec.containers[*].name}{\"\\n\"}{.spec.template.spec.containers[*].command}{\"\\n\"}{.spec.template.spec.containers[*].args}{\"\\n\"}{.spec.template.spec.volumes}",
    )
    image = jsonpath(f"deployment/{DEPLOYMENT}", "{.spec.template.spec.containers[0].image}")
    memory = jsonpath(
        f"deployment/{DEPLOYMENT}",
        "{.spec.template.spec.containers[0].resources.limits.memory}",
    )

    if current_uid != original_uid:
        return False, "deployment UID changed"
    if current_spec != original_spec:
        return False, "deployment spec changed"
    if image != EXPECTED_IMAGE:
        return False, "image changed"
    if memory != EXPECTED_MEMORY:
        return False, "memory limit changed"
    return True, "guardrails preserved"


def service_ip() -> str:
    """Return the ClusterIP for the ingress-controller service."""
    return jsonpath(f"service/{SERVICE}", "{.spec.clusterIP}")


def stable_endpoint(path: str, needle: str, attempts: int = 5, delay: int = 5) -> bool:
    """Require the HTTPS endpoint to serve the expected payload repeatedly."""
    ip = service_ip()
    if not ip:
        return False
    for _ in range(attempts):
        body = run(["curl", "-sk", "--max-time", "5", f"https://{ip}{path}"])
        if needle not in body:
            return False
        time.sleep(delay)
    return True


def current_pod_name() -> str:
    """Return the current ingress-controller pod name."""
    return run([
        "kubectl",
        "get",
        "pods",
        "-n",
        NS,
        "-l",
        "app=ingress-controller",
        "-o",
        "jsonpath={.items[0].metadata.name}",
    ])


def current_restart_count() -> int:
    """Return the current ingress-controller restart count."""
    value = run([
        "kubectl",
        "get",
        "pods",
        "-n",
        NS,
        "-l",
        "app=ingress-controller",
        "-o",
        "jsonpath={.items[0].status.containerStatuses[0].restartCount}",
    ])
    return int(value) if value.isdigit() else -1


def controller_stable_after_idle() -> bool:
    """Verify that the controller stays on the same pod without new restarts after an idle window."""
    if not deployment_ready():
        return False
    pod_before = current_pod_name()
    restarts_before = current_restart_count()
    if not pod_before or restarts_before < 0:
        return False
    time.sleep(18)
    pod_after = current_pod_name()
    restarts_after = current_restart_count()
    return pod_before == pod_after and restarts_before == restarts_after and deployment_ready()


def normalize_member_name(name: str) -> str:
    """Normalize tar member names so archives created from '.' compare consistently."""
    while name.startswith("./"):
        name = name[2:]
    return name.strip("/")


def source_bundle_members() -> dict[str, str]:
    """Decode the live source bundle secret into a text map keyed by normalized path."""
    encoded = jsonpath(f"secret/{SOURCE_SECRET}", "{.data.state\\.tgz}")
    if not encoded:
        return {}
    try:
        raw = base64.b64decode(encoded)
    except Exception:
        return {}

    members: dict[str, str] = {}
    try:
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as archive:
            for member in archive.getmembers():
                if not member.isfile():
                    continue
                normalized = normalize_member_name(member.name)
                if not normalized:
                    continue
                extracted = archive.extractfile(member)
                if extracted is None:
                    continue
                members[normalized] = extracted.read().decode("utf-8")
    except tarfile.TarError:
        return {}
    return members


def parse_env(text: str) -> dict[str, str]:
    """Parse a simple KEY=VALUE env file into a dictionary."""
    data: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        data[key] = value
    return data


def source_lock_valid(members: dict[str, str]) -> bool:
    """Recompute the source-bundle checksum and compare it with fingerprint.lock."""
    if not EXPECTED_BUNDLE_FILES.issubset(set(members)):
        return False
    digest = hashlib.sha256()
    for name in TRACKED_BUNDLE_FILES:
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(members[name].encode("utf-8"))
    return digest.hexdigest() == members["fingerprint.lock"].strip()


def selected_profile_name(members: dict[str, str]) -> str:
    """Resolve the active profile selected by the source bundle."""
    try:
        defaults = parse_env(members["defaults.env"])
        map_name = defaults["PROFILE_MAP"]
        slot = defaults["PROFILE_SLOT"]
        mapping = json.loads(members[map_name])
        return str(mapping[slot])
    except Exception:
        return ""


def computed_keepalive_timeout(members: dict[str, str]) -> int | None:
    """Compute the keepalive timeout produced by the selected source profile."""
    try:
        defaults = parse_env(members["defaults.env"])
        selected = selected_profile_name(members)
        profile = parse_env(members[f"profiles/{selected}.env"])
        return int(defaults["KEEPALIVE_BASE"]) + int(profile["KEEPALIVE_DELTA"])
    except Exception:
        return None


def source_bundle_integrity_preserved() -> bool:
    """Ensure the source bundle keeps its expected structure, metadata, and checksum."""
    members = source_bundle_members()
    if set(members) != EXPECTED_BUNDLE_FILES:
        return False
    if not source_lock_valid(members):
        return False

    defaults = parse_env(members["defaults.env"])
    stable = parse_env(members["profiles/edge-stable.env"])
    shadow = parse_env(members["profiles/edge-shadow.env"])
    try:
        mapping = json.loads(members["profile-map.json"])
    except Exception:
        return False

    required_defaults = {
        "TLS_PORT": "443",
        "UPSTREAM_HOST": "ingress-backend.ingress-system.svc.cluster.local",
        "UPSTREAM_PORT": "8080",
        "PROFILE_SLOT": "current",
        "PROFILE_MAP": "profile-map.json",
        "KEEPALIVE_BASE": "65",
        "KEEPALIVE_REQUESTS": "1000",
        "SSL_SESSION_TIMEOUT": "10m",
        "WATCHDOG_MATCH": "keepalive_timeout 0;",
        "WATCHDOG_DELAY_SECONDS": "8",
        "PROFILE_OWNER": "platform-network",
    }
    for key, value in required_defaults.items():
        if defaults.get(key) != value:
            return False

    if mapping.get("fallback") != "edge-stable":
        return False
    if stable.get("PROFILE_VARIANT") != "edge-stable":
        return False
    if shadow.get("PROFILE_VARIANT") != "edge-shadow":
        return False
    return True


def source_bundle_fixed() -> bool:
    """Verify that the selected source profile now computes the healthy keepalive timeout."""
    members = source_bundle_members()
    selected = selected_profile_name(members)
    if selected not in {"edge-stable", "edge-shadow"}:
        return False
    return computed_keepalive_timeout(members) == 65


def rendered_runtime_fixed() -> bool:
    """Require the running nginx configuration to render the healthy keepalive timeout."""
    rendered_conf = run(
        [
            "kubectl",
            "exec",
            "-n",
            NS,
            f"deployment/{DEPLOYMENT}",
            "-c",
            "nginx",
            "--",
            "cat",
            "/etc/nginx/nginx.conf",
        ]
    )
    if not rendered_conf:
        return False
    return bool(re.search(r"keepalive_timeout\s+65;", rendered_conf)) and "keepalive_timeout 0;" not in rendered_conf


def grade(context=None) -> dict:
    """Grade the task using functional HTTPS checks, stability, and source-bundle validation."""
    weights = {
        "root_https_stable": 0.10,
        "health_https_stable": 0.10,
        "controller_stable_after_idle": 0.20,
        "rendered_runtime_fixed": 0.10,
        "source_bundle_fixed": 0.30,
        "source_bundle_integrity_preserved": 0.20,
    }

    ok, reason = guardrails_ok()
    if not ok:
        subscores = {key: False for key in weights}
        return {
            "score": 0.0,
            "subscores": subscores,
            "weights": weights,
            "feedback": reason,
        }

    subscores = {
        "root_https_stable": stable_endpoint("/", "Ingress Controller Running"),
        "health_https_stable": stable_endpoint("/healthz", '"status":"ok"'),
        "controller_stable_after_idle": controller_stable_after_idle(),
        "rendered_runtime_fixed": rendered_runtime_fixed(),
        "source_bundle_fixed": source_bundle_fixed(),
        "source_bundle_integrity_preserved": source_bundle_integrity_preserved(),
    }
    score = sum(weights[name] for name, passed in subscores.items() if passed)
    feedback = " | ".join([reason] + [f"{name}: {'PASS' if passed else 'FAIL'}" for name, passed in subscores.items()])
    return {
        "score": round(score, 4),
        "subscores": subscores,
        "weights": weights,
        "feedback": feedback,
    }


if __name__ == "__main__":
    print(json.dumps(grade()))