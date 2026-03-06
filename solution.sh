#!/usr/bin/env bash
set -euo pipefail

NS="ingress-system"
WORKDIR="$(mktemp -d)"
RUNTIME_BIN="${WORKDIR}/runtime.bin"
BUNDLE_DIR="${WORKDIR}/bundle"
PATCH_FILE="${WORKDIR}/secret-patch.json"

kubectl get secret edge-runtime-assets -n "${NS}" -o jsonpath='{.data.runtime\.bin}' | base64 -d > "${RUNTIME_BIN}"
mkdir -p "${BUNDLE_DIR}"
tar -xzf "${RUNTIME_BIN}" -C "${BUNDLE_DIR}"

python3 - <<'PY' "${BUNDLE_DIR}"
from pathlib import Path
import hashlib
import sys

root = Path(sys.argv[1])
profile_path = root / "profile.env"
text = profile_path.read_text(encoding="utf-8")
old = "KEEPALIVE_TIMEOUT=0"
new = "KEEPALIVE_TIMEOUT=65"
if old not in text:
    raise SystemExit("expected KEEPALIVE_TIMEOUT=0 in profile.env")
profile_path.write_text(text.replace(old, new, 1), encoding="utf-8")

tracked = ["profile.env", "nginx.tmpl", "render.py", "verify.py"]
digest = hashlib.sha256()
for name in tracked:
    digest.update(name.encode("utf-8"))
    digest.update(b"\0")
    digest.update((root / name).read_bytes())
(root / "bundle.lock").write_text(digest.hexdigest(), encoding="utf-8")
PY

tar -czf "${RUNTIME_BIN}" -C "${BUNDLE_DIR}" .
BASE64_PAYLOAD="$(base64 -w0 "${RUNTIME_BIN}")"
printf '[{"op":"replace","path":"/data/runtime.bin","value":"%s"}]' "${BASE64_PAYLOAD}" > "${PATCH_FILE}"

kubectl patch secret edge-runtime-assets -n "${NS}" --type='json' -p "$(cat "${PATCH_FILE}")"
kubectl rollout restart deployment/ingress-controller -n "${NS}"
kubectl rollout status deployment/ingress-controller -n "${NS}" --timeout=180s