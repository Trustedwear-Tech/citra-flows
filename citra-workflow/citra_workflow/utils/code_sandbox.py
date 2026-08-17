# Copyright (c) 2026 Trustedwear Tech Private Limited (https://citra-ai.com)
# Author: Rohit Kumar Chandan
# SPDX-License-Identifier: BUSL-1.1
#
# Licensed under the Business Source License 1.1. Non-production use is granted;
# production use requires a commercial licence until the Change Date, after
# which this file converts to Apache-2.0. See LICENSE at the repository root.

"""
code_sandbox.py — one-shot Docker sandbox for the ``code_block`` node.

The ``code_block`` workflow node runs author-supplied Python. That must
never run in-process (an exec() escape = RCE on the worker, which holds
Mongo creds, JWTs, the connection-encryption key). Instead the worker
launches a locked-down container directly against the local Docker daemon
— the quick-chat sandbox image is co-located on the worker box.

Isolation mirrors the chat sandbox (``Citra-Service/services/sandbox_pool``):
  - ``network_mode="none"``        — no network at all
  - ``mem_limit`` / ``cpu_count`` / ``pids_limit`` — resource caps
  - ``user="sandbox"``             — non-root inside the container
  - ``tmpfs /tmp`` (``noexec``)
  - a hard wall-clock timeout; the container is force-removed afterwards.

Deliberately a *minimal one-shot* runner — no warm pool. Workflow code
nodes are not latency-critical the way interactive chat is, and a one-shot
container per run keeps this module small and self-contained.
"""
from __future__ import annotations

import asyncio
import io
import json
import logging
import tarfile
import uuid
from typing import Any, Dict

logger = logging.getLogger(__name__)

# Marks the node's structured result line in the sandbox's stdout.
RESULT_MARKER = "___CITRA_CODE_RESULT___"


def _build_workspace_archive(script: str, data_json: str) -> bytes:
    """A tar with ``script.py`` at the root and ``input/data.json`` —
    unpacked into ``/workspace`` inside the container."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for name, content in (("script.py", script), ("input/data.json", data_json)):
            raw = content.encode("utf-8")
            info = tarfile.TarInfo(name=name)
            info.size = len(raw)
            info.mode = 0o644
            tar.addfile(info, io.BytesIO(raw))
    return buf.getvalue()


async def run_in_sandbox(
    *,
    script: str,
    input_payload: Dict[str, Any],
    image: str,
    timeout: int,
) -> Dict[str, Any]:
    """Run ``script`` in a one-shot locked-down container.

    ``input_payload`` is written to ``/workspace/input/data.json`` for the
    script to read. Returns ``{success, exit_code, stdout, stderr}``.

    Raises ``RuntimeError`` if Docker is unavailable or the image is
    missing — the caller MUST fail closed, never fall back to in-process
    execution.
    """
    try:
        import docker  # type: ignore
        from docker.errors import ImageNotFound  # type: ignore
    except Exception as exc:  # pragma: no cover - import-time env issue
        raise RuntimeError(
            f"Code sandbox unavailable — the Docker SDK is not installed "
            f"on the worker ({exc})."
        ) from exc

    try:
        client = docker.from_env()
    except Exception as exc:
        raise RuntimeError(
            f"Code sandbox unavailable — cannot reach the Docker daemon "
            f"on the worker ({exc})."
        ) from exc

    archive = _build_workspace_archive(script, json.dumps(input_payload, default=str))
    name = f"wf-code-{uuid.uuid4().hex[:10]}"
    container = None
    try:
        try:
            container = await asyncio.to_thread(
                client.containers.create,
                image=image,
                # Override the image ENTRYPOINT, not just CMD. The shared
                # quick-chat-sandbox image bakes in
                # ENTRYPOINT=["python","/workspace/script.py"] for its own
                # one-shot use. If we only pass ``command`` it is appended as
                # *args* to that entrypoint, so the container immediately runs
                # ``python /workspace/script.py …`` — before put_archive has
                # uploaded the script — exits code 2, and the subsequent
                # exec_run fails with "container is not running" (HTTP 409).
                # Forcing the entrypoint to a long sleep keeps the container
                # alive so we can upload the workspace and exec into it.
                entrypoint=["sleep", "infinity"],
                command=[],
                name=name,
                network_mode="none",
                mem_limit="512m",
                cpu_count=1,
                pids_limit=100,
                tmpfs={"/tmp": "size=64m,noexec"},
                user="sandbox",
                working_dir="/workspace",
                detach=True,
            )
        except ImageNotFound:
            raise RuntimeError(
                f"Code sandbox image '{image}' is not present on the "
                f"worker box. Build/pull it, or set WF_CODE_SANDBOX_IMAGE."
            )

        await asyncio.to_thread(container.start)
        await asyncio.to_thread(container.put_archive, "/workspace", archive)

        def _exec():
            return container.exec_run(
                cmd=["python", "/workspace/script.py"],
                user="sandbox",
                workdir="/workspace",
                demux=True,
            )

        try:
            res = await asyncio.wait_for(asyncio.to_thread(_exec), timeout=timeout)
        except asyncio.TimeoutError:
            raise RuntimeError(
                f"Code execution exceeded the {timeout}s sandbox timeout."
            )

        out, err = res.output if isinstance(res.output, tuple) else (res.output, None)
        stdout = (out or b"").decode("utf-8", "replace")
        stderr = (err or b"").decode("utf-8", "replace")
        return {
            "success": res.exit_code == 0,
            "exit_code": res.exit_code,
            "stdout": stdout[:100_000],
            "stderr": stderr[:10_000],
        }
    finally:
        if container is not None:
            try:
                await asyncio.to_thread(container.remove, force=True)
            except Exception:
                logger.warning("code_sandbox: failed to remove container %s", name)
