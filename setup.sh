#!/usr/bin/env bash
set -e

# ------- ----------[DONOT CHANGE ANYTHING BELOW]  --------------------- #
# Start supervisord if not already running (manages k3s, dockerd, dnsmasq)
if ! supervisorctl status &>/dev/null; then
    echo "Starting supervisord..."
    /usr/bin/supervisord -c /etc/supervisor/supervisord.conf
    sleep 5
fi

# Set kubeconfig for k3s
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml

# Wait for k3s to be ready (k3s can take 30-60 seconds to start)
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
WORKDIR="/tmp/ingress-controller-memory-leak-v3"
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

echo "Creating backend and ingress configuration..."
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
    The previous responder updated ssl-session-timeout already.
    Gateway instability persisted after that change.
---
apiVersion: v1
kind: ConfigMap
metadata:
  name: ingress-controller-template
data:
  nginx.tmpl: |
    events {}

    http {
      keepalive_timeout __KEEPALIVE_TIMEOUT__;
      ssl_session_timeout __SSL_SESSION_TIMEOUT__;

      upstream gateway_backend {
        server ingress-backend.ingress-system.svc.cluster.local:8080;
      }

      server {
        listen 443 ssl;
        ssl_certificate /etc/tls/tls.crt;
        ssl_certificate_key /etc/tls/tls.key;

        location = / {
          proxy_pass http://gateway_backend/;
        }

        location = /healthz {
          proxy_pass http://gateway_backend/healthz;
        }
      }
    }
---
apiVersion: v1
kind: ConfigMap
metadata:
  name: ingress-controller-runtime
data:
  controller.env: |
    KEEPALIVE_TIMEOUT=0
    SSL_SESSION_TIMEOUT=10m
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
      containers:
        - name: nginx
          image: nginx:alpine
          command:
            - /bin/sh
            - -c
          args:
            - |
              set -e
              . /runtime/controller.env
              sed \
                -e "s/__KEEPALIVE_TIMEOUT__/${KEEPALIVE_TIMEOUT}/" \
                -e "s/__SSL_SESSION_TIMEOUT__/${SSL_SESSION_TIMEOUT}/" \
                /templates/nginx.tmpl > /etc/nginx/nginx.conf
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
            - name: template-config
              mountPath: /templates
            - name: runtime-config
              mountPath: /runtime
            - name: tls
              mountPath: /etc/tls
      volumes:
        - name: template-config
          configMap:
            name: ingress-controller-template
        - name: runtime-config
          configMap:
            name: ingress-controller-runtime
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
