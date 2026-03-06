#!/bin/bash
set -euo pipefail

export KUBECONFIG=/etc/rancher/k3s/k3s.yaml

echo "Waiting for k3s to be ready..."
until kubectl get nodes >/dev/null 2>&1; do
  sleep 2
done
echo "k3s is ready!"

NS="ingress-system"
EXPECTED_IMAGE="nginx:alpine"
EXPECTED_MEMORY="128Mi"
ORIGINAL_UID_FILE="/grader/original_uid"
SPEC_FILE="/grader/original_spec_fingerprint"

mkdir -p /grader
chmod 700 /grader

WORKDIR="$(mktemp -d)"
ACTIVE_DIR="${WORKDIR}/active-bundle"
SOURCE_DIR="${WORKDIR}/source-bundle"
ACTIVE_ARCHIVE="${WORKDIR}/runtime.bin"
SOURCE_ARCHIVE="${WORKDIR}/state.tgz"
mkdir -p "${ACTIVE_DIR}/profiles" "${SOURCE_DIR}/profiles"

cleanup() {
  rm -rf "${WORKDIR}"
}
trap cleanup EXIT

kubectl create namespace "$NS" --dry-run=client -o yaml | kubectl apply -f -

echo "Creating TLS certificate..."
openssl req -x509 -nodes -days 365 \
  -newkey rsa:2048 \
  -keyout "${WORKDIR}/tls.key" \
  -out "${WORKDIR}/tls.crt" \
  -subj "/CN=ingress-controller.${NS}.svc.cluster.local"

kubectl create secret tls ingress-controller-tls \
  --cert="${WORKDIR}/tls.crt" \
  --key="${WORKDIR}/tls.key" \
  -n "$NS" \
  --dry-run=client \
  -o yaml | kubectl apply -f -

echo "Creating decoy configmaps..."
cat <<'EOF' | kubectl apply -n "$NS" -f -
apiVersion: v1
kind: ConfigMap
metadata:
  name: ingress-backend-config
data:
  nginx.conf: |
    events {}

    http {
      server {
        listen 8080;

        location = / {
          default_type text/html;
          return 200 '<!doctype html><html><body><h1>Ingress Controller Running</h1></body></html>';
        }

        location = /healthz {
          default_type application/json;
          return 200 '{"status":"ok","service":"ingress-gateway"}';
        }
      }
    }
---
apiVersion: v1
kind: ConfigMap
metadata:
  name: ingress-nginx-config
data:
  ssl-session-timeout: "10m"
  notes.txt: |
    The previous responder updated this ingress ConfigMap already.
    Gateway instability persisted after that change.
---
apiVersion: v1
kind: ConfigMap
metadata:
  name: ingress-controller-runtime
data:
  controller.env: |
    KEEPALIVE_TIMEOUT=65
    SSL_SESSION_TIMEOUT=10m
  notes.txt: |
    Handoff snapshot from the earlier repair attempt.
    It is not the live source used by the refresh workflow.
EOF

cat <<'EOF' > "${ACTIVE_DIR}/defaults.env"
TLS_PORT=443
SERVER_NAME=_
UPSTREAM_HOST=ingress-backend.ingress-system.svc.cluster.local
UPSTREAM_PORT=8080
ROOT_PATH=/
HEALTH_PATH=/healthz
PROFILE_SLOT=current
PROFILE_MAP=profile-map.json
KEEPALIVE_BASE=65
KEEPALIVE_REQUESTS=1000
SSL_SESSION_TIMEOUT=10m
WATCHDOG_MATCH=keepalive_timeout 0;
WATCHDOG_DELAY_SECONDS=8
PROFILE_NAME=edge-active
PROFILE_OWNER=platform-network
EOF

cat <<'EOF' > "${SOURCE_DIR}/defaults.env"
TLS_PORT=443
SERVER_NAME=_
UPSTREAM_HOST=ingress-backend.ingress-system.svc.cluster.local
UPSTREAM_PORT=8080
ROOT_PATH=/
HEALTH_PATH=/healthz
PROFILE_SLOT=current
PROFILE_MAP=profile-map.json
KEEPALIVE_BASE=65
KEEPALIVE_REQUESTS=1000
SSL_SESSION_TIMEOUT=10m
WATCHDOG_MATCH=keepalive_timeout 0;
WATCHDOG_DELAY_SECONDS=8
PROFILE_NAME=edge-origin
PROFILE_OWNER=platform-network
EOF

cat <<'EOF' > "${ACTIVE_DIR}/profile-map.json"
{"current": "edge-stable", "fallback": "edge-stable"}
EOF

cat <<'EOF' > "${SOURCE_DIR}/profile-map.json"
{"current": "edge-shadow", "fallback": "edge-stable"}
EOF

cat <<'EOF' > "${ACTIVE_DIR}/profiles/edge-stable.env"
KEEPALIVE_DELTA=0
PROFILE_VARIANT=edge-stable
EOF

cat <<'EOF' > "${ACTIVE_DIR}/profiles/edge-shadow.env"
KEEPALIVE_DELTA=0
PROFILE_VARIANT=edge-shadow
EOF

cat <<'EOF' > "${SOURCE_DIR}/profiles/edge-stable.env"
KEEPALIVE_DELTA=0
PROFILE_VARIANT=edge-stable
EOF

cat <<'EOF' > "${SOURCE_DIR}/profiles/edge-shadow.env"
KEEPALIVE_DELTA=-65
PROFILE_VARIANT=edge-shadow
EOF

cat <<'EOF' > "${ACTIVE_DIR}/nginx.tmpl"
events {}

http {
  keepalive_timeout __KEEPALIVE_TIMEOUT__;
  keepalive_requests __KEEPALIVE_REQUESTS__;
  ssl_session_timeout __SSL_SESSION_TIMEOUT__;

  upstream gateway_backend {
    server __UPSTREAM_HOST__:__UPSTREAM_PORT__;
  }

  server {
    listen __TLS_PORT__ ssl;
    server_name __SERVER_NAME__;
    ssl_certificate /etc/tls/tls.crt;
    ssl_certificate_key /etc/tls/tls.key;

    location = __ROOT_PATH__ {
      proxy_pass http://gateway_backend/;
    }

    location = __HEALTH_PATH__ {
      proxy_pass http://gateway_backend/healthz;
    }
  }
}
EOF
cp "${ACTIVE_DIR}/nginx.tmpl" "${SOURCE_DIR}/nginx.tmpl"

cat <<'EOF' > "${ACTIVE_DIR}/bootstrap.py"
#!/usr/bin/env python3
import json
import os
import pathlib
import sys


def load_env(path: pathlib.Path) -> dict[str, str]:
    data: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        key, value = line.split("=", 1)
        data[key] = value
    return data


def render_bundle(defaults_path: pathlib.Path, template_path: pathlib.Path, out_dir: pathlib.Path) -> None:
    root = defaults_path.parent
    defaults = load_env(defaults_path)
    slot = defaults.get("PROFILE_SLOT")
    map_name = defaults.get("PROFILE_MAP")
    if not slot or not map_name:
        raise SystemExit("missing profile selector metadata")

    profile_map = json.loads((root / map_name).read_text(encoding="utf-8"))
    selected_profile = profile_map.get(slot)
    if not selected_profile:
        raise SystemExit(f"missing selected profile for slot: {slot}")

    overlay = load_env(root / "profiles" / f"{selected_profile}.env")
    merged = dict(defaults)
    merged.update(overlay)
    merged["SELECTED_PROFILE"] = selected_profile

    required = [
        "TLS_PORT",
        "SERVER_NAME",
        "UPSTREAM_HOST",
        "UPSTREAM_PORT",
        "ROOT_PATH",
        "HEALTH_PATH",
        "KEEPALIVE_BASE",
        "KEEPALIVE_DELTA",
        "KEEPALIVE_REQUESTS",
        "SSL_SESSION_TIMEOUT",
        "WATCHDOG_MATCH",
        "WATCHDOG_DELAY_SECONDS",
        "PROFILE_NAME",
        "PROFILE_OWNER",
        "PROFILE_VARIANT",
    ]
    missing = [key for key in required if key not in merged]
    if missing:
        raise SystemExit(f"missing bundle keys: {', '.join(missing)}")

    idle_seconds = int(merged["KEEPALIVE_BASE"]) + int(merged["KEEPALIVE_DELTA"])
    if idle_seconds < 0:
        raise SystemExit("negative keepalive timeout is not allowed")

    template = template_path.read_text(encoding="utf-8")
    replacements = {
        "__TLS_PORT__": merged["TLS_PORT"],
        "__SERVER_NAME__": merged["SERVER_NAME"],
        "__UPSTREAM_HOST__": merged["UPSTREAM_HOST"],
        "__UPSTREAM_PORT__": merged["UPSTREAM_PORT"],
        "__ROOT_PATH__": merged["ROOT_PATH"],
        "__HEALTH_PATH__": merged["HEALTH_PATH"],
        "__KEEPALIVE_TIMEOUT__": str(idle_seconds),
        "__KEEPALIVE_REQUESTS__": merged["KEEPALIVE_REQUESTS"],
        "__SSL_SESSION_TIMEOUT__": merged["SSL_SESSION_TIMEOUT"],
    }
    rendered = template
    for old, new in replacements.items():
        rendered = rendered.replace(old, new)

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "nginx.conf").write_text(rendered, encoding="utf-8")
    watchdog = f"""#!/bin/sh
set -eu
while true; do
  if grep -q '{merged['WATCHDOG_MATCH']}' /etc/nginx/nginx.conf; then
    sleep {merged['WATCHDOG_DELAY_SECONDS']}
    kill 1
  fi
  sleep 2
done
"""
    watchdog_path = out_dir / "watchdog.sh"
    watchdog_path.write_text(watchdog, encoding="utf-8")
    os.chmod(watchdog_path, 0o755)


if __name__ == "__main__":
    render_bundle(pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2]), pathlib.Path(sys.argv[3]))
EOF
cp "${ACTIVE_DIR}/bootstrap.py" "${SOURCE_DIR}/bootstrap.py"

cat <<'EOF' > "${ACTIVE_DIR}/guard.py"
#!/usr/bin/env python3
import hashlib
import pathlib
import sys

TRACKED_FILES = [
    "defaults.env",
    "profile-map.json",
    "profiles/edge-stable.env",
    "profiles/edge-shadow.env",
    "nginx.tmpl",
    "bootstrap.py",
    "guard.py",
]


def bundle_digest(root: pathlib.Path) -> str:
    digest = hashlib.sha256()
    for name in TRACKED_FILES:
        path = root / name
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
    return digest.hexdigest()


root = pathlib.Path(sys.argv[1])
expected = (root / "fingerprint.lock").read_text(encoding="utf-8").strip()
actual = bundle_digest(root)
if actual != expected:
    raise SystemExit(f"bundle integrity mismatch: expected {expected}, got {actual}")
EOF
cp "${ACTIVE_DIR}/guard.py" "${SOURCE_DIR}/guard.py"

python3 - "${ACTIVE_DIR}" "${SOURCE_DIR}" <<'PY'
from pathlib import Path
import hashlib
import sys

tracked = [
    "defaults.env",
    "profile-map.json",
    "profiles/edge-stable.env",
    "profiles/edge-shadow.env",
    "nginx.tmpl",
    "bootstrap.py",
    "guard.py",
]
for raw_root in sys.argv[1:]:
    root = Path(raw_root)
    digest = hashlib.sha256()
    for name in tracked:
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update((root / name).read_bytes())
    (root / "fingerprint.lock").write_text(digest.hexdigest(), encoding="utf-8")
PY

chmod 0644 \
  "${ACTIVE_DIR}/defaults.env" \
  "${ACTIVE_DIR}/profile-map.json" \
  "${ACTIVE_DIR}/profiles/edge-stable.env" \
  "${ACTIVE_DIR}/profiles/edge-shadow.env" \
  "${ACTIVE_DIR}/nginx.tmpl" \
  "${ACTIVE_DIR}/bootstrap.py" \
  "${ACTIVE_DIR}/guard.py" \
  "${ACTIVE_DIR}/fingerprint.lock"

chmod 0644 \
  "${SOURCE_DIR}/defaults.env" \
  "${SOURCE_DIR}/profile-map.json" \
  "${SOURCE_DIR}/profiles/edge-stable.env" \
  "${SOURCE_DIR}/profiles/edge-shadow.env" \
  "${SOURCE_DIR}/nginx.tmpl" \
  "${SOURCE_DIR}/bootstrap.py" \
  "${SOURCE_DIR}/guard.py" \
  "${SOURCE_DIR}/fingerprint.lock"

tar -czf "${ACTIVE_ARCHIVE}" -C "${ACTIVE_DIR}" .
tar -czf "${SOURCE_ARCHIVE}" -C "${SOURCE_DIR}" .

kubectl create secret generic edge-runtime-assets \
  -n "$NS" \
  --from-file=runtime.bin="${ACTIVE_ARCHIVE}" \
  --dry-run=client \
  -o yaml | kubectl apply -f -

kubectl create secret generic edge-sync-cache \
  -n "$NS" \
  --from-file=state.tgz="${SOURCE_ARCHIVE}" \
  --dry-run=client \
  -o yaml | kubectl apply -f -

echo "Creating workloads..."
cat <<'EOF' | kubectl apply -n "$NS" -f -
apiVersion: apps/v1
kind: Deployment
metadata:
  name: ingress-backend
spec:
  replicas: 1
  selector:
    matchLabels:
      app: ingress-backend
  template:
    metadata:
      labels:
        app: ingress-backend
    spec:
      containers:
        - name: nginx
          image: nginx:alpine
          ports:
            - containerPort: 8080
          resources:
            limits:
              memory: "96Mi"
          volumeMounts:
            - name: backend-config
              mountPath: /etc/nginx/nginx.conf
              subPath: nginx.conf
      volumes:
        - name: backend-config
          configMap:
            name: ingress-backend-config
---
apiVersion: v1
kind: Service
metadata:
  name: ingress-backend
spec:
  selector:
    app: ingress-backend
  ports:
    - port: 8080
      targetPort: 8080
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: ingress-controller
spec:
  replicas: 1
  selector:
    matchLabels:
      app: ingress-controller
  template:
    metadata:
      labels:
        app: ingress-controller
    spec:
      initContainers:
        - name: seed-rendered-runtime
          image: python:3.11-slim
          command:
            - /bin/sh
            - -c
            - |
              set -e
              mkdir -p /shared/active /shared/rendered
              tar -xzf /active-bundle/runtime.bin -C /shared/active
              python /shared/active/guard.py /shared/active
              python /shared/active/bootstrap.py /shared/active/defaults.env /shared/active/nginx.tmpl /shared/rendered
          volumeMounts:
            - name: active-bundle
              mountPath: /active-bundle
            - name: rendered-assets
              mountPath: /shared
      containers:
        - name: nginx
          image: nginx:alpine
          command:
            - /bin/sh
            - -c
          args:
            - |
              set -e
              /bin/sh /watchdog.sh &
              exec nginx -g 'daemon off;' -c /etc/nginx/nginx.conf
          ports:
            - containerPort: 443
          resources:
            limits:
              memory: "128Mi"
          volumeMounts:
            - name: rendered-assets
              mountPath: /etc/nginx/nginx.conf
              subPath: rendered/nginx.conf
              readOnly: true
            - name: rendered-assets
              mountPath: /watchdog.sh
              subPath: rendered/watchdog.sh
              readOnly: true
            - name: tls
              mountPath: /etc/tls
              readOnly: true
        - name: runtime-sync
          image: python:3.11-slim
          command:
            - /bin/sh
            - -c
          args:
            - |
              set -e
              mkdir -p /shared/source /shared/rendered
              sleep 6
              while true; do
                rm -rf /shared/source/*
                tar -xzf /origin-cache/state.tgz -C /shared/source
                python /shared/source/guard.py /shared/source
                python /shared/source/bootstrap.py /shared/source/defaults.env /shared/source/nginx.tmpl /shared/rendered
                echo "synced rendered runtime from source cache"
                sleep 6
              done
          volumeMounts:
            - name: origin-cache
              mountPath: /origin-cache
              readOnly: true
            - name: rendered-assets
              mountPath: /shared
      volumes:
        - name: active-bundle
          secret:
            secretName: edge-runtime-assets
        - name: origin-cache
          secret:
            secretName: edge-sync-cache
        - name: rendered-assets
          emptyDir: {}
        - name: tls
          secret:
            secretName: ingress-controller-tls
---
apiVersion: v1
kind: Service
metadata:
  name: ingress-controller
spec:
  selector:
    app: ingress-controller
  ports:
    - port: 443
      targetPort: 443
EOF

kubectl rollout status deployment/ingress-backend -n "$NS" --timeout=180s
kubectl rollout status deployment/ingress-controller -n "$NS" --timeout=180s

kubectl get deployment ingress-controller -n "$NS" -o jsonpath='{.metadata.uid}' > "$ORIGINAL_UID_FILE"
kubectl get deployment ingress-controller -n "$NS" -o jsonpath='{.spec.template.spec.initContainers[*].name}{"\n"}{.spec.template.spec.initContainers[*].command}{"\n"}{.spec.template.spec.initContainers[*].args}{"\n"}{.spec.template.spec.containers[*].name}{"\n"}{.spec.template.spec.containers[*].command}{"\n"}{.spec.template.spec.containers[*].args}{"\n"}{.spec.template.spec.volumes}' > "$SPEC_FILE"
chmod 600 "$ORIGINAL_UID_FILE" "$SPEC_FILE"

echo "Setup completed."