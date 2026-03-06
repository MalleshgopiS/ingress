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
WORKDIR="/tmp/ingress-controller-memory-leak-v4"
UID_FILE="/grader/original_uid"

mkdir -p "$WORKDIR" /grader
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

echo "Creating backend and configuration bundles..."
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
    KEEPALIVE_TIMEOUT=0
    SSL_SESSION_TIMEOUT=10m
  notes.txt: |
    Legacy bundle kept for rollback reference.
---
apiVersion: v1
kind: ConfigMap
metadata:
  name: gateway-template-bundle
data:
  nginx.tmpl: |
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
---
apiVersion: v1
kind: ConfigMap
metadata:
  name: gateway-bootstrap-profile
data:
  gateway.env: |
    TLS_PORT=443
    SERVER_NAME=_
    UPSTREAM_HOST=ingress-backend.ingress-system.svc.cluster.local
    UPSTREAM_PORT=8080
    ROOT_PATH=/
    HEALTH_PATH=/healthz
    GATEWAY_IDLE_SECONDS=0
    KEEPALIVE_REQUESTS=1000
    SSL_SESSION_TIMEOUT=10m
    PROFILE_NAME=blue-edge
    PROFILE_OWNER=platform-network
---
apiVersion: v1
kind: ConfigMap
metadata:
  name: gateway-renderer
data:
  render.sh: |
    #!/bin/sh
    set -eu
    . /profile/gateway.env

    : "${TLS_PORT:?missing TLS_PORT}"
    : "${SERVER_NAME:?missing SERVER_NAME}"
    : "${UPSTREAM_HOST:?missing UPSTREAM_HOST}"
    : "${UPSTREAM_PORT:?missing UPSTREAM_PORT}"
    : "${ROOT_PATH:?missing ROOT_PATH}"
    : "${HEALTH_PATH:?missing HEALTH_PATH}"
    : "${GATEWAY_IDLE_SECONDS:?missing GATEWAY_IDLE_SECONDS}"
    : "${KEEPALIVE_REQUESTS:?missing KEEPALIVE_REQUESTS}"
    : "${SSL_SESSION_TIMEOUT:?missing SSL_SESSION_TIMEOUT}"
    : "${PROFILE_NAME:?missing PROFILE_NAME}"
    : "${PROFILE_OWNER:?missing PROFILE_OWNER}"

    sed \
      -e "s|__TLS_PORT__|${TLS_PORT}|g" \
      -e "s|__SERVER_NAME__|${SERVER_NAME}|g" \
      -e "s|__UPSTREAM_HOST__|${UPSTREAM_HOST}|g" \
      -e "s|__UPSTREAM_PORT__|${UPSTREAM_PORT}|g" \
      -e "s|__ROOT_PATH__|${ROOT_PATH}|g" \
      -e "s|__HEALTH_PATH__|${HEALTH_PATH}|g" \
      -e "s|__KEEPALIVE_TIMEOUT__|${GATEWAY_IDLE_SECONDS}|g" \
      -e "s|__KEEPALIVE_REQUESTS__|${KEEPALIVE_REQUESTS}|g" \
      -e "s|__SSL_SESSION_TIMEOUT__|${SSL_SESSION_TIMEOUT}|g" \
      /templates/nginx.tmpl > /rendered/nginx.conf
EOF

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
        - name: render-config
          image: nginx:alpine
          command:
            - /bin/sh
            - /renderer/render.sh
          volumeMounts:
            - name: template-bundle
              mountPath: /templates
            - name: bootstrap-profile
              mountPath: /profile
            - name: renderer
              mountPath: /renderer
            - name: rendered-config
              mountPath: /rendered
      containers:
        - name: nginx
          image: nginx:alpine
          command:
            - /bin/sh
            - -c
          args:
            - |
              set -e
              cat >/watchdog.sh <<'EOS'
              while true; do
                if grep -q 'keepalive_timeout 0;' /etc/nginx/nginx.conf; then
                  sleep 12
                  kill 1
                fi
                sleep 2
              done
              EOS
              /bin/sh /watchdog.sh &
              exec nginx -g 'daemon off;'
          ports:
            - containerPort: 443
          resources:
            limits:
              memory: "128Mi"
          volumeMounts:
            - name: rendered-config
              mountPath: /etc/nginx/nginx.conf
              subPath: nginx.conf
            - name: tls
              mountPath: /etc/tls
      volumes:
        - name: template-bundle
          configMap:
            name: gateway-template-bundle
        - name: bootstrap-profile
          configMap:
            name: gateway-bootstrap-profile
        - name: renderer
          configMap:
            name: gateway-renderer
            defaultMode: 0555
        - name: rendered-config
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
chmod 400 "$UID_FILE"

echo "Setup completed."