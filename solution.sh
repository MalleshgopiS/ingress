#!/usr/bin/env bash
set -euo pipefail

NS="ingress-system"
WORKDIR="$(mktemp -d)"
PROFILE_FILE="${WORKDIR}/gateway.env"
PATCHED_FILE="${WORKDIR}/gateway-profile-patch.yaml"

kubectl get configmap gateway-bootstrap-profile -n "${NS}" -o jsonpath='{.data.gateway\.env}' > "${PROFILE_FILE}"

python3 - <<'PY' "${PROFILE_FILE}"
from pathlib import Path
import sys

path = Path(sys.argv[1])
text = path.read_text(encoding="utf-8")
old = "GATEWAY_IDLE_SECONDS=0"
new = "GATEWAY_IDLE_SECONDS=65"
if old not in text:
    raise SystemExit("expected GATEWAY_IDLE_SECONDS=0 in gateway.env")
path.write_text(text.replace(old, new, 1), encoding="utf-8")
PY

{
  printf 'apiVersion: v1\n'
  printf 'kind: ConfigMap\n'
  printf 'metadata:\n'
  printf '  name: gateway-bootstrap-profile\n'
  printf '  namespace: %s\n' "${NS}"
  printf 'data:\n'
  printf '  gateway.env: |-\n'
  sed 's/^/    /' "${PROFILE_FILE}"
} > "${PATCHED_FILE}"

kubectl apply -f "${PATCHED_FILE}"
kubectl rollout restart deployment/ingress-controller -n "${NS}"
kubectl rollout status deployment/ingress-controller -n "${NS}" --timeout=180s