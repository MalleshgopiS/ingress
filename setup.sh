#!/usr/bin/env bash
set -e

# ------- ----------[DONOT CHANGE ANYTHING BELOW]  --------------------- #
if ! supervisorctl status &>/dev/null; then
    echo "Starting supervisord..."
    /usr/bin/supervisord -c /etc/supervisor/supervisord.conf
    sleep 5
fi

export KUBECONFIG=/etc/rancher/k3s/k3s.yaml

echo "Waiting for k3s to be ready..."
MAX_WAIT=180
ELAPSED=0
until kubectl get nodes &>/dev/null; do
    if [ $ELAPSED -ge $MAX_WAIT ]; then
        echo "Error: k3s is not ready after ${MAX_WAIT} seconds"
        exit 1
    fi
    echo "Waiting for k3s... (${ELAPSED}s elapsed)"
    sleep 2
    ELAPSED=$((ELAPSED + 2))
done

echo "k3s is ready!"
# ---------------- [DONOT CHANGE ANYTHING ABOVE] ------------------------- #

NS="ingress-system"
WORKDIR="/tmp/ingress-controller-memory-leak-v5"
UID_FILE="/grader/original_uid"
SPEC_FILE="/grader/original_spec_fingerprint"
BUNDLE_DIR="${WORKDIR}/bundle-src"
RUNTIME_ARCHIVE="${WORKDIR}/runtime.bin"

mkdir -p "$WORKDIR" "$BUNDLE_DIR" /grader
cd "$WORKDIR"

kubectl create namespace "$NS" --dry-run=client -o yaml | kubectl apply -f -

echo "Creating TLS certificate..."
openssl req -x509 -nodes -days 365 \
  -newkey rsa:2048 \
  -keyout tls.key \
  -out tls.crt \
  -subj "/CN=ingress-controller.${NS}.svc.cluster.local"

kubectl create secret tls ingress-controller-tls \
  --cert=tls.crt \
  --key=tls.key \
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
    Legacy reference copy retained from the previous incident handoff.
    The live runtime is rendered from a different bootstrap source.
EOF

cat <<'EOF' > "${BUNDLE_DIR}/profile.env"
TLS_PORT=443
SERVER_NAME=_
UPSTREAM_HOST=ingress-backend.ingress-system.svc.cluster.local
UPSTREAM_PORT=8080
ROOT_PATH=/
HEALTH_PATH=/healthz
KEEPALIVE_TIMEOUT=0
KEEPALIVE_REQUESTS=1000
SSL_SESSION_TIMEOUT=10m
WATCHDOG_MATCH=keepalive_timeout 0;
WATCHDOG_DELAY_SECONDS=12
PROFILE_NAME=blue-edge
PROFILE_OWNER=platform-network
EOF

cat <<'EOF' > "${BUNDLE_DIR}/nginx.tmpl"
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

cat <<'EOF' > "${BUNDLE_DIR}/render.py"
#!/usr/bin/env python3
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


profile_path = pathlib.Path(sys.argv[1])
template_path = pathlib.Path(sys.argv[2])
out_dir = pathlib.Path(sys.argv[3])
profile = load_env(profile_path)
required = [
    "TLS_PORT",
    "SERVER_NAME",
    "UPSTREAM_HOST",
    "UPSTREAM_PORT",
    "ROOT_PATH",
    "HEALTH_PATH",
    "KEEPALIVE_TIMEOUT",
    "KEEPALIVE_REQUESTS",
    "SSL_SESSION_TIMEOUT",
    "WATCHDOG_MATCH",
    "WATCHDOG_DELAY_SECONDS",
    "PROFILE_NAME",
    "PROFILE_OWNER",
]
missing = [key for key in required if key not in profile]
if missing:
    raise SystemExit(f"missing bootstrap keys: {', '.join(missing)}")

template = template_path.read_text(encoding="utf-8")
replacements = {
    "__TLS_PORT__": profile["TLS_PORT"],
    "__SERVER_NAME__": profile["SERVER_NAME"],
    "__UPSTREAM_HOST__": profile["UPSTREAM_HOST"],
    "__UPSTREAM_PORT__": profile["UPSTREAM_PORT"],
    "__ROOT_PATH__": profile["ROOT_PATH"],
    "__HEALTH_PATH__": profile["HEALTH_PATH"],
    "__KEEPALIVE_TIMEOUT__": profile["KEEPALIVE_TIMEOUT"],
    "__KEEPALIVE_REQUESTS__": profile["KEEPALIVE_REQUESTS"],
    "__SSL_SESSION_TIMEOUT__": profile["SSL_SESSION_TIMEOUT"],
}
rendered = template
for old, new in replacements.items():
    rendered = rendered.replace(old, new)

out_dir.mkdir(parents=True, exist_ok=True)
(out_dir / "nginx.conf").write_text(rendered, encoding="utf-8")
watchdog = f"""#!/bin/sh
set -eu
while true; do
  if grep -q '{profile['WATCHDOG_MATCH']}' /etc/nginx/nginx.conf; then
    sleep {profile['WATCHDOG_DELAY_SECONDS']}
    kill 1
  fi
  sleep 2
done
"""
watchdog_path = out_dir / "watchdog.sh"
watchdog_path.write_text(watchdog, encoding="utf-8")
os.chmod(watchdog_path, 0o755)
EOF

chmod 0644 "${BUNDLE_DIR}/profile.env" "${BUNDLE_DIR}/nginx.tmpl" "${BUNDLE_DIR}/render.py"
tar -czf "${RUNTIME_ARCHIVE}" -C "${BUNDLE_DIR}" .

kubectl create secret generic edge-runtime-assets \
  -n "$NS" \
  --from-file=runtime.bin="${RUNTIME_ARCHIVE}" \
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
        - name: asset-renderer
          image: python:3.11-slim
          command:
            - /bin/sh
            - -c
            - |
              set -e
              mkdir -p /work/bootstrap /work/rendered
              tar -xzf /bundle/runtime.bin -C /work/bootstrap
              python /work/bootstrap/render.py /work/bootstrap/profile.env /work/bootstrap/nginx.tmpl /work/rendered
          volumeMounts:
            - name: runtime-bundle
              mountPath: /bundle
            - name: rendered-assets
              mountPath: /work
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
      volumes:
        - name: runtime-bundle
          secret:
            secretName: edge-runtime-assets
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
kubectl rollout status deployment/ingress-controller -n "$NS" --timeout=180s || true

kubectl get deployment ingress-controller -n "$NS" -o jsonpath='{.metadata.uid}' > "$UID_FILE"
kubectl get deployment ingress-controller -n "$NS" -o jsonpath='{.spec.template.spec.initContainers[0].command}{"\n"}{.spec.template.spec.containers[0].command}{"\n"}{.spec.template.spec.containers[0].args}{"\n"}{.spec.template.spec.volumes}' > "$SPEC_FILE"
chmod 400 "$UID_FILE" "$SPEC_FILE"

echo "Setup completed."