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

NS="aurora-ingress"
WORKDIR="/tmp/aurora-ingress-task"

mkdir -p "$WORKDIR"
cd "$WORKDIR"

kubectl create namespace "$NS" --dry-run=client -o yaml | kubectl apply -f -

echo "Creating TLS material..."
openssl req -x509 -nodes -days 365 \
  -newkey rsa:2048 \
  -keyout tls.key \
  -out tls.crt \
  -subj "/CN=edge-gateway.${NS}.svc.cluster.local"

kubectl create secret tls edge-gateway-tls \
  --cert=tls.crt \
  --key=tls.key \
  -n "$NS" \
  --dry-run=client \
  -o yaml | kubectl apply -f -

echo "Creating backend content..."
cat <<'EOF' | kubectl apply -n "$NS" -f -
apiVersion: v1
kind: ConfigMap
metadata:
  name: bleater-ui-content
data:
  index.html: |
    <!doctype html>
    <html>
      <head>
        <meta charset="utf-8">
        <title>Bleater Dashboard</title>
      </head>
      <body>
        <h1>Bleater Dashboard</h1>
        <p>Static dashboard shell is healthy.</p>
        <script src="/assets/app.js"></script>
      </body>
    </html>
---
apiVersion: v1
kind: ConfigMap
metadata:
  name: bleater-assets-content
data:
  app.js: |
    window.appLoaded = true;
    console.log("Bleater Dashboard assets loaded");
---
apiVersion: v1
kind: ConfigMap
metadata:
  name: bleater-api-config
data:
  nginx.conf: |
    events {}

    http {
      server {
        listen 9090;
        default_type application/json;

        location = /health {
          add_header Content-Type application/json always;
          return 200 '{"status":"ok","service":"bleater-api"}';
        }

        location / {
          return 404;
        }
      }
    }
---
apiVersion: v1
kind: ConfigMap
metadata:
  name: edge-gateway-config
data:
  nginx.conf: |
    events {}

    http {
      keepalive_timeout 0;

      server {
        listen 443 ssl;
        ssl_certificate /etc/tls/tls.crt;
        ssl_certificate_key /etc/tls/tls.key;

        location = / {
          proxy_pass http://bleater-ui.aurora-ingress.svc.cluster.local:80;
        }

        location /assets/ {
          proxy_pass http://bleater-assets.aurora-ingress.svc.cluster.local:80;
        }

        location = /api/health {
          proxy_pass http://bleater-api.aurora-ingress.svc.cluster.local:8080/health;
        }
      }
    }
EOF

echo "Creating workloads..."
cat <<'EOF' | kubectl apply -n "$NS" -f -
apiVersion: apps/v1
kind: Deployment
metadata:
  name: bleater-ui
spec:
  replicas: 1
  selector:
    matchLabels:
      app: bleater-ui
  template:
    metadata:
      labels:
        app: bleater-ui
    spec:
      containers:
        - name: nginx
          image: nginx:alpine
          ports:
            - containerPort: 80
          resources:
            limits:
              memory: "96Mi"
          volumeMounts:
            - name: ui-content
              mountPath: /usr/share/nginx/html
      volumes:
        - name: ui-content
          configMap:
            name: bleater-ui-content
---
apiVersion: v1
kind: Service
metadata:
  name: bleater-ui
spec:
  selector:
    app: bleater-ui
  ports:
    - port: 8080
      targetPort: 80
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: bleater-assets
spec:
  replicas: 1
  selector:
    matchLabels:
      app: bleater-assets
  template:
    metadata:
      labels:
        app: bleater-assets
    spec:
      containers:
        - name: nginx
          image: nginx:alpine
          ports:
            - containerPort: 80
          resources:
            limits:
              memory: "96Mi"
          volumeMounts:
            - name: assets-content
              mountPath: /usr/share/nginx/html/assets
      volumes:
        - name: assets-content
          configMap:
            name: bleater-assets-content
---
apiVersion: v1
kind: Service
metadata:
  name: bleater-assets
spec:
  selector:
    app: bleater-assets
  ports:
    - port: 8081
      targetPort: 80
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: bleater-api
spec:
  replicas: 1
  selector:
    matchLabels:
      app: bleater-api
  template:
    metadata:
      labels:
        app: bleater-api
    spec:
      containers:
        - name: nginx
          image: nginx:alpine
          ports:
            - containerPort: 9090
          resources:
            limits:
              memory: "96Mi"
          volumeMounts:
            - name: api-config
              mountPath: /etc/nginx/nginx.conf
              subPath: nginx.conf
      volumes:
        - name: api-config
          configMap:
            name: bleater-api-config
---
apiVersion: v1
kind: Service
metadata:
  name: bleater-api
spec:
  selector:
    app: bleater-api
  ports:
    - port: 9090
      targetPort: 9090
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: edge-gateway
spec:
  replicas: 1
  selector:
    matchLabels:
      app: edge-gateway
  template:
    metadata:
      labels:
        app: edge-gateway
    spec:
      containers:
        - name: nginx
          image: nginx:alpine
          ports:
            - containerPort: 443
          resources:
            limits:
              memory: "128Mi"
          volumeMounts:
            - name: gateway-config
              mountPath: /etc/nginx/nginx.conf
              subPath: nginx.conf
            - name: tls
              mountPath: /etc/tls
      volumes:
        - name: gateway-config
          configMap:
            name: edge-gateway-config
        - name: tls
          secret:
            secretName: edge-gateway-tls
---
apiVersion: v1
kind: Service
metadata:
  name: edge-gateway
spec:
  selector:
    app: edge-gateway
  ports:
    - port: 443
      targetPort: 443
EOF

kubectl rollout status deployment/bleater-ui -n "$NS" --timeout=180s
kubectl rollout status deployment/bleater-assets -n "$NS" --timeout=180s
kubectl rollout status deployment/bleater-api -n "$NS" --timeout=180s
kubectl rollout status deployment/edge-gateway -n "$NS" --timeout=180s

echo "Setup completed."