# Copyright (c) 2026 Trustedwear Tech Private Limited (https://citra-ai.com)
# Author: Rohit Kumar Chandan
# SPDX-License-Identifier: BUSL-1.1
#
# Licensed under the Business Source License 1.1. Non-production use is granted;
# production use requires a commercial licence until the Change Date, after
# which this file converts to Apache-2.0. See LICENSE at the repository root.

"""
Workflow handler — runs workflows out-of-process from Citra-Service.

Two job types served:

  workflow.run     — execute a workflow end-to-end. Triggered by:
                     (a) the scheduler (cron / interval triggers), and
                     (b) future enqueue() calls from Citra-Service's
                         manual /run endpoint once that's converted.

  workflow.resume  — resume a paused execution after approval. Used by
                     human-in-the-loop steps.

Today, Citra-Service's HTTP /run endpoint runs the executor in an
asyncio background task inside the request process. That works (it
doesn't block the event loop) but means workflow CPU competes with
chat request handling. Migrating to enqueue is a follow-up PR — this
handler is ready for it.
"""
from __future__ import annotations

import logging
from typing import Any, Dict

from citra_queue import JobPermanentFailure
from registry import JobContext, register

logger = logging.getLogger(__name__)


@register("workflow.run")
async def run_workflow(payload: Dict[str, Any], ctx: JobContext) -> Dict[str, Any]:
    """Execute a workflow.

    Expected payload::

        {
            "workflow_id":  "<uuid>",
            "user_id":      "<email>",
            "trigger_data": { ... },
            "environment":  "test" | "prod",
            "execution_id": "<uuid>",   # optional — generated if absent
            "jwt_token":    "<...>",    # optional — minted at trigger time
            "workflow_definition": { ... },  # optional — inline UNSAVED
                                # definition (assistant test runs). Test
                                # environment ONLY; skips the Mongo load.
        }

    Returns the execution result dict (status + node_results + error).
    """
    workflow_id = payload.get("workflow_id")
    user_id = payload.get("user_id") or ""
    inline_def = payload.get("workflow_definition")
    if not workflow_id and not inline_def:
        raise JobPermanentFailure("workflow_id is required")

    # Imports deferred — citra_workflow is heavy and we don't want to
    # load it at worker startup (it slows down imports for unrelated
    # handlers in the same registry).
    from citra_workflow.executor import WorkflowExecutor, ConcurrencyLimitError
    from citra_workflow.models import WorkflowDefinition

    if inline_def:
        # Unsaved working copy from the workflow AI assistant's test tool —
        # runs here so code_block nodes get the Docker sandbox (the
        # citra-workflow API container has no docker.sock). Inline
        # definitions bypass the saved Workflows collection, so they are
        # confined to the test environment: a prod run would execute an
        # unreviewed definition against prod connections.
        env = payload.get("environment") or "test"
        if env != "test":
            raise JobPermanentFailure(
                f"inline workflow_definition runs must use "
                f"environment='test', got '{env}'"
            )
        try:
            workflow = WorkflowDefinition(**inline_def)
        except Exception as exc:  # noqa: BLE001 — bad payload, retry won't help
            raise JobPermanentFailure(
                f"inline workflow_definition is not a valid workflow: {exc}"
            )
        workflow_id = workflow.workflow_id
    else:
        # Load the workflow definition. The Worker shares Mongo with
        # Citra-Service, so we read the same Workflows collection. We
        # deliberately do NOT filter by user_id: under org-only ownership,
        # any IT-role user in the workflow's org may trigger a run, so the
        # payload's user_id is the *triggering actor* — not an authorization
        # constraint on the load. Authorization happened at the trigger side.
        from citra_mongo import get_async_mongo_client, MONGODB_DATABASE
        db = get_async_mongo_client()[MONGODB_DATABASE]
        doc = await db["Workflows"].find_one({"workflow_id": workflow_id})
        if not doc:
            raise JobPermanentFailure(f"workflow {workflow_id} not found")
        doc.pop("_id", None)
        workflow = WorkflowDefinition(**doc)

    logger.info(
        f"▶️  workflow.run: id={workflow_id} user={user_id} "
        f"env={payload.get('environment') or 'test'} job={ctx.job_id}"
    )

    executor = WorkflowExecutor()
    try:
        execution = await executor.execute(
            workflow,
            trigger_data=payload.get("trigger_data") or {},
            execution_id=payload.get("execution_id"),
            environment=payload.get("environment") or "test",
            jwt_token=payload.get("jwt_token"),
        )
    except ConcurrencyLimitError as exc:
        # Capacity error — transient, let the worker retry.
        raise RuntimeError(f"concurrency limit hit: {exc}")

    status = execution.status.value if hasattr(execution.status, "value") else execution.status
    result = {
        "execution_id": getattr(execution, "execution_id", None),
        "workflow_id": workflow_id,
        "status": status,
    }
    if hasattr(execution, "error") and execution.error:
        result["error"] = execution.error
    return result


@register("workflow.resume")
async def resume_workflow(payload: Dict[str, Any], ctx: JobContext) -> Dict[str, Any]:
    """Resume a paused workflow after an approval decision.

    Expected payload::

        {
            "execution_id": "<uuid>",
            "workflow_id":  "<uuid>",
            "user_id":      "<email>",
            "approved":     true | false,
        }
    """
    execution_id = payload.get("execution_id")
    workflow_id = payload.get("workflow_id")
    user_id = payload.get("user_id") or ""
    approved = bool(payload.get("approved", False))
    if not (execution_id and workflow_id):
        raise JobPermanentFailure("execution_id, workflow_id required")

    from citra_workflow.executor import WorkflowExecutor
    from citra_workflow.models import WorkflowDefinition, WorkflowExecution

    from citra_mongo import get_async_mongo_client, MONGODB_DATABASE
    db = get_async_mongo_client()[MONGODB_DATABASE]

    # See workflow.run for why we don't filter by user_id under org-only
    # ownership — authorization happened at the trigger side.
    wf_doc = await db["Workflows"].find_one({"workflow_id": workflow_id})
    if not wf_doc:
        raise JobPermanentFailure(f"workflow {workflow_id} not found")
    wf_doc.pop("_id", None)
    workflow = WorkflowDefinition(**wf_doc)

    ex_doc = await db["WorkflowExecutions"].find_one({"execution_id": execution_id})
    if not ex_doc:
        raise JobPermanentFailure(f"execution {execution_id} not found")
    ex_doc.pop("_id", None)
    execution = WorkflowExecution(**ex_doc)

    logger.info(
        f"▶️  workflow.resume: execution={execution_id} approved={approved} job={ctx.job_id}"
    )

    executor = WorkflowExecutor()
    result = await executor.resume(execution, workflow, approved=approved)
    return {
        "execution_id": execution_id,
        "approved": approved,
        "status": result.status.value if hasattr(result.status, "value") else result.status,
    }


@register("ping")
async def ping(payload: Dict[str, Any], ctx: JobContext) -> Dict[str, Any]:
    """Liveness handler — verifies queue + worker round-trip."""
    return {
        "ok": True,
        "echo": payload,
        "job_id": ctx.job_id,
        "tenant_id": ctx.tenant_id,
        "request_id": ctx.request_id,
    }
