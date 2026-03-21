#!/usr/bin/env python3

import base64
import datetime
import json
import re
import subprocess
import time
from apex_arena._types import GradingResult

NS     = "ingress-system"
DEPLOY = "ingress-controller"
SVC    = "ingress-controller-svc"


# ── Shell helper ─────────────────────────────────────────────────────────────

def run(cmd: str, timeout: int = 30):
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except Exception:
        return -1, "", "error"


# ── Config helpers ───────────────────────────────────────────────────────────

def _get_configmap_conf() -> str:
    _, out, _ = run(
        f"kubectl get configmap ingress-nginx-config -n {NS} "
        "-o jsonpath='{.data.nginx\\.conf}'"
    )
    return out


def _get_running_pod() -> str:
    _, out, _ = run(
        f"kubectl get pods -n {NS} -l app=ingress-controller "
        "--field-selector=status.phase=Running "
        "-o jsonpath='{.items[0].metadata.name}'"
    )
    return out.strip()


def _get_cluster_ip() -> str:
    _, out, _ = run(
        f"kubectl get svc {SVC} -n {NS} -o jsonpath='{{.spec.clusterIP}}'"
    )
    return out.strip()


def _get_restart_count() -> str:
    _, out, _ = run(
        f"kubectl get pod -n {NS} -l app=ingress-controller "
        "-o jsonpath='{{.items[0].status.containerStatuses[0].restartCount}}'"
    )
    return out.strip() or "0"


# ── RELAXED TLS VALIDATION (binary safe) ──────────────────────────────────────

def _cache_ok(text: str) -> bool:
    match = re.search(r"ssl_session_cache\s+([^;]+);", text or "")
    if not match:
        return False
    val = match.group(1).strip()

    return (
        "shared:SSL" in val and
        "builtin" not in val
    )


def _timeout_ok(text: str) -> bool:
    match = re.search(r"ssl_session_timeout\s+([^;]+);", text or "")
    if not match:
        return False
    val = match.group(1).strip()

    return not val.startswith("86400")


def _buffer_ok(text: str) -> bool:
    match = re.search(r"ssl_buffer_size\s+([^;]+);", text or "")
    if not match:
        return False
    val = match.group(1).strip()

    return val in ["4k", "8k", "16k"]


# ── Objective 1: TLS params corrected ────────────────────────────────────────

def _obj_tls_params_corrected():
    checks = {}

    for attempt in range(3):
        cfg = _get_configmap_conf()

        checks = {
            "cache_valid":   _cache_ok(cfg),
            "timeout_valid": _timeout_ok(cfg),
            "buffer_valid":  _buffer_ok(cfg),
        }

        if all(checks.values()):
            break

        time.sleep(5)

    score = 1.0 if all(checks.values()) else 0.0
    detail = ", ".join(f"{k}:{'✓' if v else '✗'}" for k, v in checks.items())
    return score, detail


# ── Objective 2: nginx live updated ──────────────────────────────────────────

def _obj_nginx_live_updated():
    pod = _get_running_pod()

    if not pod:
        return 0.0, "no pod"

    checks = {}

    for attempt in range(5):
        _, live, _ = run(f"kubectl exec -n {NS} {pod} -- nginx -T 2>/dev/null")
        _, out, err = run(f"kubectl exec -n {NS} {pod} -- nginx -t")

        syntax_ok = "syntax is ok" in (out + err).lower()

        checks = {
            "cache_live":   _cache_ok(live),
            "timeout_live": _timeout_ok(live),
            "buffer_live":  _buffer_ok(live),
            "syntax_ok":    syntax_ok,
            "config_loaded": len(live) > 100
        }

        if all(checks.values()):
            break

        time.sleep(8)

    score = 1.0 if all(checks.values()) else 0.0
    detail = ", ".join(f"{k}:{'✓' if v else '✗'}" for k, v in checks.items())
    return score, detail


# ── Objective 3: pod stability ───────────────────────────────────────────────

def _obj_pod_stable():
    pod = _get_running_pod()

    for _ in range(6):
        if pod:
            break
        time.sleep(5)
        pod = _get_running_pod()

    restart_before = _get_restart_count()
    time.sleep(20)
    restart_after = _get_restart_count()

    stable = (restart_before == restart_after) and bool(pod)

    score = 1.0 if stable else 0.0
    return score, f"stable={stable}"


# ── Objective 4: HTTPS operational ───────────────────────────────────────────

def _obj_https_operational():
    ip = _get_cluster_ip()

    if not ip:
        return 0.0, "no ip"

    https_ok = False

    for _ in range(5):
        _, body, _ = run(f"curl -k -s --max-time 5 https://{ip}/healthz")
        if "ok" in body.lower():
            https_ok = True
            break
        time.sleep(3)

    _, tls_out, tls_err = run(
        f"echo Q | openssl s_client -connect {ip}:443 2>&1 | head -10"
    )

    tls_ok = "connected" in (tls_out + tls_err).lower()

    score = 1.0 if https_ok and tls_ok else 0.0
    return score, f"https={https_ok}, tls={tls_ok}"


# ── Grade ───────────────────────────────────────────────────────────────────

OBJECTIVES = [
    ("tls_params_corrected", _obj_tls_params_corrected),
    ("nginx_live_updated",   _obj_nginx_live_updated),
    ("pod_stable",           _obj_pod_stable),
    ("https_operational",    _obj_https_operational),
]


def grade(transcript: str = None) -> GradingResult:
    try:
        subscores = {}
        feedback = []

        for name, fn in OBJECTIVES:
            score, msg = fn()
            subscores[name] = score
            feedback.append(f"{name}: {msg}")

        weights = {k: 0.25 for k in subscores}

        final = sum(subscores[k] * weights[k] for k in subscores)

        return GradingResult(
            score=final,
            subscores=subscores,
            weights=weights,
            feedback=" | ".join(feedback)
        )

    except Exception as e:
        return GradingResult(score=0.0, feedback=str(e))