#!/usr/bin/env python3
# Copyright (c) 2026 Trustedwear Tech Private Limited (https://citra-ai.com)
# Author: Rohit Kumar Chandan
# SPDX-License-Identifier: BUSL-1.1
#
# Licensed under the Business Source License 1.1. Non-production use is granted;
# production use requires a commercial licence until the Change Date, after
# which this file converts to Apache-2.0. See LICENSE at the repository root.

"""Verify a running Citra Flows stack, end to end.

    make smoke          # or:  python scripts/smoke_test.py

Signs in, authors a small workflow through the API, runs it, and asserts the
run reaches `completed`. That last step is the one that matters: the API only
ENQUEUES a run, so a stack whose worker is missing or misconfigured answers
every health check happily and leaves runs in `queued` forever. Nothing short
of watching a run finish proves the install works.

Standard library only — no jq, no bash, no pip install.
Exit code 0 = the install is good.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TIMEOUT = 30
RUN_DEADLINE = 120  # seconds to wait for a run to reach a terminal state

# A Windows console defaults to cp1252, which cannot encode most non-ASCII —
# printing one raises UnicodeEncodeError and the script dies before it has
# checked anything. Output below is ASCII-only; this is the belt to that
# braces, since a failure message may quote arbitrary server text.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):  # pragma: no cover - old/odd streams
        pass

GREEN, RED, YELLOW, DIM, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"
if os.name == "nt" and not os.environ.get("WT_SESSION"):
    # Old consoles render the escapes literally, which is worse than no colour.
    GREEN = RED = YELLOW = DIM = RESET = ""

_failures: list[str] = []


def ok(msg: str) -> None:
    print(f"  {GREEN}PASS{RESET}  {msg}")


def fail(msg: str) -> None:
    print(f"  {RED}FAIL{RESET}  {msg}")
    _failures.append(msg)


def info(msg: str) -> None:
    print(f"  {DIM}{msg}{RESET}")


def load_env() -> dict:
    """Read .env, falling back to the shipped template."""
    path = REPO_ROOT / ".env"
    if not path.exists():
        path = REPO_ROOT / ".env.example"
        print(f"{YELLOW}No .env found; reading defaults from {path.name}{RESET}")
    env = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        env[key.strip()] = value.strip()
    return env


def request(method: str, url: str, token: str | None = None, body: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(raw)
        except ValueError:
            return exc.code, {"detail": raw[:400]}
    except urllib.error.URLError as exc:
        return None, {"detail": str(exc.reason)}


def main() -> int:
    env = load_env()
    port = env.get("FLOWS_API_PORT", "9200")
    base = os.environ.get("FLOWS_API_BASE", f"http://localhost:{port}")
    email = env.get("ADMIN_EMAIL", "admin@citra-ai.com")
    password = env.get("ADMIN_PASSWORD", "change-me-locally")

    print(f"\nCitra Flows smoke test -> {base}\n")

    # ── 1. the API is up ──────────────────────────────────────────────────
    status, _ = request("GET", f"{base}/health")
    if status != 200:
        fail(f"API not reachable at {base} (is the stack up? `make ps`)")
        return report()
    ok("API is up")

    # ── 2. sign in ────────────────────────────────────────────────────────
    status, payload = request("POST", f"{base}/api/auth/login",
                              body={"email": email, "password": password})
    if status != 200 or not (payload or {}).get("token"):
        fail(f"sign-in failed for {email} (HTTP {status}): {(payload or {}).get('detail')}")
        info("The account comes from ADMIN_EMAIL / ADMIN_PASSWORD in .env, seeded by")
        info("the citra-user-service-init container. After changing them, re-run")
        info("`docker compose -f docker-compose.quickstart.yml up -d` to re-seed.")
        return report()
    token = payload["token"]
    ok(f"signed in as {email}")

    # ── 3. the node palette loaded ────────────────────────────────────────
    status, payload = request("GET", f"{base}/api/workflows/node-schemas", token)
    # The endpoint answers {"schemas": [...]}; tolerate a bare list too.
    schemas = payload.get("schemas") if isinstance(payload, dict) else payload
    count = len(schemas) if isinstance(schemas, list) else 0
    if status != 200 or count < 40:
        fail(f"node palette looks wrong (HTTP {status}, {count} node types)")
    else:
        ok(f"node palette: {count} node types")

    # ── 4. author a workflow ──────────────────────────────────────────────
    definition = {
        "name": "Smoke Test",
        "description": "Created by scripts/smoke_test.py; safe to delete.",
        "nodes": [
            {"id": "trigger", "type": "manual_trigger", "label": "Start",
             "position": {"x": 80, "y": 120}, "config": {}},
            {"id": "setvar", "type": "set_variable", "label": "Set a variable",
             "position": {"x": 380, "y": 120},
             "config": {"assignments": [{"name": "smoke", "value": "ok"}]}},
        ],
        "edges": [{"id": "e1", "source": "trigger", "target": "setvar"}],
    }
    status, created = request("POST", f"{base}/api/workflows", token, definition)
    workflow_id = (created or {}).get("workflow_id") or (created or {}).get("id")
    if status not in (200, 201) or not workflow_id:
        fail(f"could not create a workflow (HTTP {status}): {(created or {}).get('detail')}")
        return report()
    ok(f"authored a 2-node workflow ({workflow_id[:8]}...)")

    try:
        # ── 5. run it ─────────────────────────────────────────────────────
        status, run = request("POST", f"{base}/api/workflows/{workflow_id}/execute",
                              token, {"variables": {}, "environment": "test"})
        execution_id = (run or {}).get("execution_id")
        if status not in (200, 202) or not execution_id:
            fail(f"could not start a run (HTTP {status}): {(run or {}).get('detail')}")
            return report()
        ok(f"run submitted ({execution_id[:8]}...)")

        # ── 6. wait for it to FINISH — the part that catches a dead worker ─
        deadline = time.time() + RUN_DEADLINE
        state, detail = "queued", None
        while time.time() < deadline:
            status, detail = request(
                "GET", f"{base}/api/workflows/executions/{execution_id}", token)
            state = ((detail or {}).get("execution") or detail or {}).get("status", "unknown")
            if state in ("completed", "failed", "cancelled"):
                break
            time.sleep(2)

        execution = (detail or {}).get("execution") or detail or {}
        if state == "completed":
            nodes_run = len(execution.get("node_results") or {})
            ok(f"run completed ({nodes_run} nodes executed)")
        elif state == "queued":
            fail("run never left 'queued' — nothing is consuming the job queue")
            info("The worker executes runs; the API only enqueues them.")
            info("Check it is alive:  make logs SERVICE=citra-worker")
        else:
            fail(f"run ended '{state}': {execution.get('error')}")
    finally:
        request("DELETE", f"{base}/api/workflows/{workflow_id}", token)

    return report()


def report() -> int:
    print()
    if _failures:
        print(f"{RED}Smoke test FAILED{RESET} — {len(_failures)} check(s) did not pass.\n")
        return 1
    print(f"{GREEN}Smoke test passed — your Citra Flows install works.{RESET}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
