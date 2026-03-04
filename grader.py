import subprocess
import json


def run(cmd):
    return subprocess.check_output(cmd, shell=True).decode().strip()


def grade():

    results = {}

    uid = run(
        "kubectl -n ingress-system get deployment ingress-controller -o jsonpath='{.metadata.uid}'"
    )
    results["uid_preserved"] = bool(uid)

    image = run(
        "kubectl -n ingress-system get deployment ingress-controller -o jsonpath='{.spec.template.spec.containers[0].image}'"
    )
    results["image_preserved"] = image == "nginx:alpine"

    memory = run(
        "kubectl -n ingress-system get deployment ingress-controller -o jsonpath='{.spec.template.spec.containers[0].resources.limits.memory}'"
    )
    results["memory_preserved"] = memory == "128Mi"

    config = run(
        "kubectl -n ingress-system get configmap ingress-nginx-config -o json"
    )
    config = json.loads(config)

    results["valid_timeout"] = "keepalive_timeout 65;" in config["data"]["nginx.conf"]

    ready = run(
        "kubectl -n ingress-system get deployment ingress-controller -o jsonpath='{.status.readyReplicas}'"
    )
    results["deployment_ready"] = ready == "1"

    svc = run(
        "kubectl -n ingress-system get svc ingress-controller -o jsonpath='{.spec.clusterIP}'"
    )

    http = run(
        f"kubectl run curl --rm -i --tty --restart=Never --image=curlimages/curl -- curl -s http://{svc} || true"
    )

    results["nginx_serving"] = "Ingress Controller Running" in http

    score = sum(results.values()) / len(results)

    return {
        "subscores": results,
        "score": score,
    }


if __name__ == "__main__":
    print(json.dumps(grade(), indent=2))