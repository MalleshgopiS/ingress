#!/bin/bash
set -euo pipefail

NS="ingress-system"
WORKDIR="$(mktemp -d)"
ARCHIVE_PATH="${WORKDIR}/state.tgz"
PATCH_FILE="${WORKDIR}/patch.json"
EXTRACT_DIR="${WORKDIR}/bundle"
mkdir -p "${EXTRACT_DIR}"

kubectl get secret edge-sync-cache -n "${NS}" -o jsonpath='{.data.state\.tgz}' | base64 -d > "${ARCHIVE_PATH}"
tar -xzf "${ARCHIVE_PATH}" -C "${EXTRACT_DIR}"

python3 - "${EXTRACT_DIR}" <<'PY'
from pathlib import Path
import hashlib
import sys

root = Path(sys.argv[1])
profile_path = root / "profiles" / "edge-shadow.env"
text = profile_path.read_text(encoding="utf-8")
old = "KEEPALIVE_DELTA=-65"
new = "KEEPALIVE_DELTA=0"
if old not in text:
    raise SystemExit("expected KEEPALIVE_DELTA=-65 in profiles/edge-shadow.env")
profile_path.write_text(text.replace(old, new), encoding="utf-8")

tracked = [
    "defaults.env",
    "profile-map.json",
    "profiles/edge-stable.env",
    "profiles/edge-shadow.env",
    "nginx.tmpl",
    "bootstrap.py",
    "guard.py",
]
digest = hashlib.sha256()
for name in tracked:
    digest.update(name.encode("utf-8"))
    digest.update(b"\0")
    digest.update((root / name).read_bytes())
(root / "fingerprint.lock").write_text(digest.hexdigest(), encoding="utf-8")
PY

tar -czf "${ARCHIVE_PATH}" -C "${EXTRACT_DIR}" .
BASE64_PAYLOAD="$(base64 -w0 "${ARCHIVE_PATH}")"
printf '[{"op":"replace","path":"/data/state.tgz","value":"%s"}]' "${BASE64_PAYLOAD}" > "${PATCH_FILE}"

kubectl patch secret edge-sync-cache -n "${NS}" --type='json' -p "$(cat "${PATCH_FILE}")"
kubectl rollout restart deployment/ingress-controller -n "${NS}"
kubectl rollout status deployment/ingress-controller -n "${NS}" --timeout=180s