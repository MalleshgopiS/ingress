import subprocess
import time

NS = "aurora-ingress"
SERVICE = "ingress-controller"
DEPLOYMENT = "ingress-controller"
CONFIGMAP = "ingress-controller-config"
EXPECTED_IMAGE = "nginx:alpine"
EXPECTED_MEMORY = "128Mi"


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


def repeated_success(path, expected, attempts=4, delay=4):
    for _ in range(attempts):
        ok, body = https_get(path)
        if not ok or expected not in body:
            return False
        time.sleep(delay)
    return True


def current_pod_name():
    return jsonpath("pods -l app=ingress-controller", "{.items[0].metadata.name}")


def pod_restart_count(pod_name):
    if not pod_name:
        return ""
    return jsonpath(f"pod/{pod_name}", "{.status.containerStatuses[0].restartCount}")


def deployment_ready():
    ready = jsonpath(f"deployment/{DEPLOYMENT}", "{.status.readyReplicas}")
    unavailable = jsonpath(f"deployment/{DEPLOYMENT}", "{.status.unavailableReplicas}")
    return ready == "1" and unavailable in ["", "0", "<no value>"]


def supported_fix_applied():
    config = jsonpath(f"configmap/{CONFIGMAP}", "{.data.nginx\\.conf}")
    image = jsonpath(f"deployment/{DEPLOYMENT}", "{.spec.template.spec.containers[0].image}")
    memory = jsonpath(
        f"deployment/{DEPLOYMENT}",
        "{.spec.template.spec.containers[0].resources.limits.memory}",
    )
    revision = jsonpath(
        f"deployment/{DEPLOYMENT}",
        "{.metadata.annotations.deployment\\.kubernetes\\.io/revision}",
    )
    return (
        "keepalive_timeout 65;" in config
        and image == EXPECTED_IMAGE
        and memory == EXPECTED_MEMORY
        and revision not in ["", "1"]
        and deployment_ready()
    )


def stable_after_idle_window():
    pod = current_pod_name()
    before = pod_restart_count(pod)
    if not pod or before == "":
        return False
    time.sleep(14)
    after_pod = current_pod_name()
    after = pod_restart_count(after_pod)
    if pod != after_pod or before != after:
        return False
    ok_root, body_root = https_get("/")
    ok_health, body_health = https_get("/healthz")
    return (
        ok_root
        and "Ingress Controller Running" in body_root
        and ok_health
        and '"status":"ok"' in body_health
    )


def grade(context=None):
    checks = [
        "root_https_stable",
        "health_https_stable",
        "controller_stable_after_idle",
        "supported_fix_applied",
    ]
    weights = {name: 1.0 / len(checks) for name in checks}

    results = {
        "root_https_stable": repeated_success("/", "Ingress Controller Running"),
        "health_https_stable": repeated_success("/healthz", '"status":"ok"'),
        "controller_stable_after_idle": stable_after_idle_window(),
        "supported_fix_applied": supported_fix_applied(),
    }

    score = sum(weights[name] for name, passed in results.items() if passed)
    feedback = "\n".join(
        f"{name}: {'PASS' if passed else 'FAIL'} (w={weights[name]:.2f})"
        for name, passed in results.items()
    )
    return GradeResult(score, results, weights, feedback)
