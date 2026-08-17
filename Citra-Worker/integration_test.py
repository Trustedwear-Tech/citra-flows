# Copyright (c) 2026 Trustedwear Tech Private Limited (https://citra-ai.com)
# Author: Rohit Kumar Chandan
# SPDX-License-Identifier: BUSL-1.1
#
# Licensed under the Business Source License 1.1. Non-production use is granted;
# production use requires a commercial licence until the Change Date, after
# which this file converts to Apache-2.0. See LICENSE at the repository root.

"""
End-to-end integration test for Citra-Worker.

Spawns a real worker subprocess against the configured Redis + Mongo
(loaded from `.env` via env_loader's fallback chain), then exercises
the producer-side enqueue path (the same code Citra-Service uses) and
verifies jobs round-trip through the queue.

What this proves:
  * .env loading works in subprocess
  * Producer's `enqueue()` writes to Redis correctly
  * Worker's `consume_one()` BLPOPs and dispatches
  * Handler runs and `mark_done()` writes the result
  * Producer's `get_status()` reads it back
  * Wire format is compatible across processes

Run from Citra-Worker directory::

    python integration_test.py

Add `--keep-worker` to leave the subprocess running afterwards for
manual poking.
"""
from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
CITRA_SERVICE = HERE.parent / "Citra-Service"
CITRA_WORKFLOW = HERE.parent / "citra-workflow"
CITRA_SVC_UTILS = HERE.parent / "citra-common" / "citra-service-utils"


def _setup_paths() -> None:
    """Make Citra-Service + shared packages importable for the test process."""
    for p in (CITRA_SERVICE, CITRA_WORKFLOW, CITRA_SVC_UTILS, HERE):
        sp = str(p)
        if sp not in sys.path:
            sys.path.insert(0, sp)


def _print_step(n: int, msg: str) -> None:
    print(f"\n=== Step {n}: {msg} ===")


def _assert(cond: bool, msg: str) -> None:
    if not cond:
        print(f"  FAIL: {msg}")
        sys.exit(1)
    print(f"  PASS: {msg}")


def _start_pump(proc: subprocess.Popen) -> "queue.Queue[str]":
    """Pump worker stdout into a queue from a background thread.

    `subprocess.PIPE.readline()` blocks on Windows when the child is
    alive but quiet, so the main test thread can never poll it
    safely. The pump thread reads to EOF and pushes lines into a
    thread-safe queue the main thread drains non-blockingly.
    """
    q: "queue.Queue[str]" = queue.Queue()

    def _pump() -> None:
        try:
            for line in iter(proc.stdout.readline, ""):
                if not line:
                    break
                q.put(line)
        finally:
            q.put("")  # sentinel: pipe closed

    t = threading.Thread(target=_pump, daemon=True)
    t.start()
    return q


def _spawn_worker() -> subprocess.Popen:
    """Spawn `python -m worker` as a subprocess.

    Inherits the parent's PYTHONPATH (which we've set to include
    Citra-Service + shared packages) so the worker's deferred imports
    can resolve.
    """
    pythonpath_parts = [
        str(CITRA_SERVICE),
        str(CITRA_WORKFLOW),
        str(CITRA_SVC_UTILS),
        str(HERE),
        os.environ.get("PYTHONPATH", ""),
    ]
    pythonpath = os.pathsep.join(p for p in pythonpath_parts if p)

    env = {
        **os.environ,
        "PYTHONPATH": pythonpath,
        "PYTHONUNBUFFERED": "1",
        "CITRA_WORKER_CONCURRENCY": "1",
        # Don't need the workflow scheduler for this test — disable it to
        # keep the subprocess focused on the queue path.
        "CITRA_WORKER_RUN_SCHEDULER": "false",
        "LOG_LEVEL": "INFO",
    }

    proc = subprocess.Popen(
        [sys.executable, "-u", "-m", "worker"],
        cwd=str(HERE),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",     # worker emits emoji log lines — force utf-8 decode
        errors="replace",     #   so the pump thread can't choke on stray bytes
        bufsize=1,
    )
    return proc


def _wait_for_worker_ready(proc: subprocess.Popen, q: "queue.Queue[str]", timeout: float = 10.0) -> str:
    """Drain pumped stdout until we see the 'watching queues' line."""
    deadline = time.time() + timeout
    captured: list[str] = []
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(
                f"worker exited early with code {proc.returncode}\n"
                f"--- worker output ---\n{''.join(captured)}"
            )
        try:
            line = q.get(timeout=0.2)
        except queue.Empty:
            continue
        if not line:  # sentinel = pipe closed
            raise RuntimeError(
                f"worker pipe closed before ready\n"
                f"--- captured ---\n{''.join(captured)}"
            )
        captured.append(line)
        sys.stdout.write(f"  [worker] {line.rstrip()}\n")
        if "watching queues" in line:
            return "".join(captured)
    raise TimeoutError(
        f"worker did not become ready within {timeout}s\n"
        f"--- captured ---\n{''.join(captured)}"
    )


def _drain_worker_output(q: "queue.Queue[str]", max_lines: int = 100) -> None:
    """Drain whatever the pump has buffered (non-blocking)."""
    for _ in range(max_lines):
        try:
            line = q.get_nowait()
        except queue.Empty:
            return
        if not line:  # sentinel
            return
        sys.stdout.write(f"  [worker] {line.rstrip()}\n")


def main() -> int:
    # Forwarded worker logs contain emojis; default Windows console is cp1252.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass

    _setup_paths()

    # Use the same env loader the worker uses (resolves .env fallback chain).
    from env_loader import load_env
    load_env()

    # Verify prerequisites are populated.
    _print_step(0, "Prerequisites")
    redis_host = os.getenv("REDIS_HOST")
    mongo_conn = os.getenv("MONGODB_CONN_STRING")
    print(f"  REDIS_HOST   = {(redis_host or '<unset>')[:60]}")
    print(f"  MONGODB_CONN = {'set' if mongo_conn else '<unset>'}")
    _assert(bool(redis_host), "REDIS_HOST is set (env loaded)")
    _assert(bool(mongo_conn), "MONGODB_CONN_STRING is set (env loaded)")

    # Quick Redis ping using producer-side client to confirm connectivity.
    # Producer path moved from services.worker_queue (deleted with the
    # carve-out) to the shared citra_queue package — see commit c31c4551.
    _print_step(1, "Producer can reach Redis")
    from citra_queue.queue import _sync_redis as _producer_redis
    try:
        rc = _producer_redis()
        rc.ping()
    except Exception as exc:  # noqa: BLE001
        print(f"  FAIL: Redis unreachable: {exc}")
        return 1
    print("  PASS: Redis pong")

    # Spawn the worker.
    _print_step(2, "Spawn worker subprocess")
    worker = _spawn_worker()
    pump_q = _start_pump(worker)
    try:
        _wait_for_worker_ready(worker, pump_q, timeout=15.0)
        print("  PASS: worker is consuming")
    except Exception as exc:  # noqa: BLE001
        print(f"  FAIL: {exc}")
        worker.terminate()
        return 1

    # Enqueue a ping job using the producer code path. The producer is now
    # citra_queue (shared lib), same module the worker consumes.
    _print_step(3, "Enqueue ping job from producer")
    from citra_queue import enqueue
    from citra_queue.queue import get_status

    payload = {"msg": "integration-test", "n": 42}
    job_id = enqueue(
        "ping",
        payload,
        tenant_id="itest-tenant",
        request_id="itest-request",
    )
    print(f"  job_id = {job_id}")

    # Poll until done. Worker BLPOPs in 5s windows so the first iteration
    # may take up to 5 s.
    _print_step(4, "Poll for result")
    deadline = time.time() + 20.0
    final = None
    while time.time() < deadline:
        st = get_status(job_id)
        if st:
            status = st.get("status")
            if status in ("done", "failed"):
                final = st
                print(f"  status = {status} ({st.get('finished_at', '?')})")
                break
            print(f"  status = {status}, retries = {st.get('retries', '?')}")
        time.sleep(0.5)

    _drain_worker_output(pump_q, max_lines=30)

    _assert(final is not None, "job reached terminal state")
    _assert(final.get("status") == "done", f"job done (status={final.get('status')})")

    result = final.get("result") or {}
    if isinstance(result, str):
        result = json.loads(result)
    print(f"  result = {result}")
    _assert(result.get("ok") is True, "result.ok is True")
    _assert(result.get("echo") == payload, "result.echo matches payload")
    _assert(result.get("job_id") == job_id, "result.job_id matches")
    _assert(result.get("tenant_id") == "itest-tenant", "tenant_id propagated")
    _assert(result.get("request_id") == "itest-request", "request_id propagated")

    # Test JobPermanentFailure path with workflow.run + bad payload.
    _print_step(5, "Enqueue workflow.run with missing fields -> permanent failure")
    job_id2 = enqueue("workflow.run", {}, tenant_id="t", request_id="r")
    deadline = time.time() + 15.0
    final2 = None
    while time.time() < deadline:
        st = get_status(job_id2)
        if st and st.get("status") in ("done", "failed"):
            final2 = st
            break
        time.sleep(0.5)
    _drain_worker_output(pump_q, max_lines=20)
    _assert(final2 is not None, "permanent-failure job reached terminal state")
    _assert(final2.get("status") == "failed", "marked as failed (not retried)")
    last_err = final2.get("last_error") or ""
    _assert("workflow_id is required" in last_err, f"correct error message ({last_err[:80]})")

    # Test a workflow.run with a real lookup miss to prove Mongo round-trip.
    _print_step(6, "workflow.run on a non-existent workflow -> permanent failure (Mongo lookup ran)")
    job_id3 = enqueue(
        "workflow.run",
        {"workflow_id": "definitely-not-a-real-id", "user_id": "nobody@itest"},
        tenant_id="t",
        request_id="r",
    )
    deadline = time.time() + 15.0
    final3 = None
    while time.time() < deadline:
        st = get_status(job_id3)
        if st and st.get("status") in ("done", "failed"):
            final3 = st
            break
        time.sleep(0.5)
    _drain_worker_output(pump_q, max_lines=20)
    _assert(final3 is not None, "Mongo-lookup job reached terminal state")
    _assert(final3.get("status") == "failed", "marked as failed")
    err3 = final3.get("last_error") or ""
    _assert(
        "not found" in err3,
        f"Mongo lookup ran and returned 'not found' ({err3[:120]})",
    )

    # Static audit: for every node class registered in citra-workflow,
    # AST-extract every `import X` / `from X import Y` inside its execute()
    # body and verify the module resolves in the current venv. This catches
    # the class of bug that hid sqlalchemy / pymysql / boto3 — a node lazy-
    # imports a lib at execute() time, and the dep file forgot to list it.
    _print_step(7, "Per-node import audit (every node class's execute() body)")
    import ast
    import importlib.util
    NODE_FILES = [
        CITRA_WORKFLOW / "citra_workflow" / "nodes" / f
        for f in ("sources.py", "outputs.py", "agents.py", "processors.py",
                  "logic.py", "state.py", "triggers.py", "smart_apps.py")
    ]

    def _execute_imports(path: Path) -> dict[str, set[str]]:
        out: dict[str, set[str]] = {}
        if not path.exists():
            return out
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            return out
        for cls in [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]:
            for item in cls.body:
                if (isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
                        and item.name == "execute"):
                    imps: set[str] = set()
                    for node in ast.walk(item):
                        if isinstance(node, ast.Import):
                            for alias in node.names:
                                imps.add(alias.name.split(".")[0])
                        elif isinstance(node, ast.ImportFrom):
                            if node.level == 0 and node.module:
                                imps.add(node.module.split(".")[0])
                    if imps:
                        out[cls.name] = imps
        return out

    # mongodb_manager appears as a try/except fallback after citra_mongo —
    # never reached when citra_mongo is installed (which it always is).
    FALLBACK_OK = {"mongodb_manager"}

    classes_with_execute = 0
    missing_rows: list[tuple[str, list[str]]] = []
    for f in NODE_FILES:
        for cls_name, imps in _execute_imports(f).items():
            classes_with_execute += 1
            missing = sorted(
                n for n in imps
                if n not in FALLBACK_OK and importlib.util.find_spec(n) is None
            )
            if missing:
                missing_rows.append((cls_name, missing))
    print(f"  node classes audited: {classes_with_execute}")
    print(f"  classes missing deps: {len(missing_rows)}")
    for cls, miss in missing_rows:
        print(f"    {cls}: missing {miss}")
    _assert(not missing_rows, "every node's execute() imports resolve")

    # SQLAlchemy loads dialect drivers DYNAMICALLY by URL prefix, so AST
    # never sees `import pymysql` / `import psycopg2` / `import pyodbc`.
    # Verify each driver is wired in by asking SQLAlchemy to load it.
    _print_step(8, "SQLAlchemy can load every SQL dialect driver")
    import sqlalchemy
    for url, label in [
        ("postgresql+psycopg2://u:p@h/d", "psycopg2 (Postgres)"),
        ("mysql+pymysql://u:p@h/d", "pymysql (MySQL)"),
        ("mssql+pyodbc://u:p@h/d?driver=ODBC+Driver+17+for+SQL+Server", "pyodbc (MSSQL)"),
    ]:
        try:
            # create_engine doesn't connect; it just loads the dialect.
            sqlalchemy.create_engine(url)
            print(f"  PASS: {label}")
        except ModuleNotFoundError as exc:
            print(f"  FAIL: {label}: {exc}")
            return 1
        except Exception as exc:  # noqa: BLE001
            # Any non-ImportError means the dialect loaded (Mongo/network/etc.
            # are expected if the dialect happens to validate the URL).
            print(f"  PASS: {label} (dialect loaded; got {type(exc).__name__})")

    # Real workflow.run that actually completes — proves the full pipeline
    # (queue -> handler -> executor -> node -> Mongo write).
    _print_step(9, "Full workflow.run pipeline (manual_trigger -> set_variable)")
    import uuid as _uuid
    from citra_mongo import get_async_mongo_client, MONGODB_DATABASE
    import asyncio

    test_workflow_id = f"itest-{_uuid.uuid4().hex[:8]}"
    test_user = "integration-test@itest.local"
    test_execution_id = str(_uuid.uuid4())

    async def _setup_and_check():
        db = get_async_mongo_client()[MONGODB_DATABASE]
        await db["Workflows"].insert_one({
            "workflow_id": test_workflow_id,
            "user_id": test_user,
            "name": "Integration test (manual_trigger -> set_variable)",
            "description": "Auto-created + cleaned by integration_test.py",
            "status": "draft",
            "nodes": [
                {"id": "t1", "type": "manual_trigger", "label": "Start",
                 "position": {"x": 50, "y": 100}, "config": {}},
                {"id": "n1", "type": "set_variable", "label": "Set var",
                 "position": {"x": 250, "y": 100},
                 "config": {"assignments": [{"name": "k", "value": "v"}]}},
            ],
            "edges": [{"id": "e1", "source": "t1", "target": "n1"}],
        })
        return db

    async def _fetch_execution(db) -> dict | None:
        return await db["WorkflowExecutions"].find_one({"execution_id": test_execution_id})

    async def _cleanup(db) -> None:
        await db["Workflows"].delete_many({"workflow_id": test_workflow_id})
        await db["WorkflowExecutions"].delete_many({"workflow_id": test_workflow_id})

    db = asyncio.get_event_loop().run_until_complete(_setup_and_check())
    try:
        job_id4 = enqueue("workflow.run", {
            "workflow_id": test_workflow_id,
            "user_id": test_user,
            "trigger_data": {"trigger": "integration-test"},
            "environment": "test",
            "execution_id": test_execution_id,
        }, tenant_id=test_user, request_id=test_execution_id)
        print(f"  job_id = {job_id4}")

        deadline = time.time() + 30.0
        final4 = None
        while time.time() < deadline:
            st = get_status(job_id4)
            if st and st.get("status") in ("done", "failed"):
                final4 = st
                break
            time.sleep(0.5)
        _drain_worker_output(pump_q, max_lines=30)
        _assert(final4 is not None, "workflow.run job reached terminal state")
        _assert(final4.get("status") == "done", f"workflow.run done (status={final4.get('status')})")

        # Give the handler a moment to finalise the Mongo write.
        time.sleep(0.5)
        ex = asyncio.get_event_loop().run_until_complete(_fetch_execution(db))
        _assert(ex is not None, "WorkflowExecutions doc was written")
        _assert(ex.get("status") == "completed",
                f"workflow status=completed (got {ex.get('status')})")
        node_results = ex.get("node_results") or {}
        _assert("t1" in node_results and "n1" in node_results,
                f"both nodes in node_results (got {list(node_results.keys())})")
        n1 = node_results["n1"]
        n1_status = n1.get("status") if isinstance(n1, dict) else n1
        _assert(n1_status == "completed",
                f"set_variable node completed (got {n1_status})")
    finally:
        asyncio.get_event_loop().run_until_complete(_cleanup(db))

    # Cleanup.
    _print_step(10, "Shut down worker")
    if "--keep-worker" in sys.argv:
        print("  --keep-worker: leaving subprocess running. PID:", worker.pid)
    else:
        worker.terminate()
        try:
            worker.wait(timeout=10)
            print(f"  worker exited with code {worker.returncode}")
        except subprocess.TimeoutExpired:
            worker.kill()
            print("  worker did not exit on SIGTERM, killed")

    print("\n=== ALL INTEGRATION TESTS PASSED ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
