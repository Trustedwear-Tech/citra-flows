# Copyright (c) 2026 Trustedwear Tech Private Limited (https://citra-ai.com)
# Author: Rohit Kumar Chandan
# SPDX-License-Identifier: BUSL-1.1
#
# Licensed under the Business Source License 1.1. Non-production use is granted;
# production use requires a commercial licence until the Change Date, after
# which this file converts to Apache-2.0. See LICENSE at the repository root.

"""
Cross-run workflow state nodes.

Citra workflows are stateless across cron fires by default — each run gets
fresh ``WorkflowExecution.variables`` and the executor doesn't carry forward
anything else. Most workflows are happy with that, but a few patterns
genuinely need a watermark:

  • "Preprocess Claims" pulls only NEW claims since the last run — needs
    ``last_run_at``.
  • Incremental ETL workflows track ``high_watermark_id`` per source.
  • Refresh-from-History tracks the last successful sample-batch version.

These two nodes (``WORKFLOW_STATE_GET`` and ``WORKFLOW_STATE_SET``) write a
small Mongo document keyed by ``(workflow_id, key_name)``. Get reads it into
the workflow's variables (so subsequent nodes see ``{{var.last_run_at}}``);
Set writes the value at the end of the run.

Why a Mongo collection and not Redis: the state must survive scheduler
restarts, leadership changes, and full process recycles. Mongo is already
the canonical persistence layer for workflow definitions and executions.

Schema: ``citra.workflow_state`` collection
  ``{ workflow_id: str, key: str, value: any, updated_at: datetime,
      updated_by_execution: str | None }``
  Unique compound index on ``(workflow_id, key)``.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from ..models import NodeType, NodeCategory, NodeFieldSchema
from ..utils.template import interpolate_dotted
from . import BaseNode, NodeContext, register_node

logger = logging.getLogger(__name__)


_COLLECTION = "workflow_state"
_INDEXES_READY = False


async def _coll():
    """Resolve the Mongo collection used for workflow state.

    Lazy import keeps the workflow package usable in environments that don't
    have Mongo configured (unit tests, dry-runs).
    """
    from citra_mongo import MONGODB_DATABASE, get_async_mongo_client  # type: ignore
    return get_async_mongo_client()[MONGODB_DATABASE][_COLLECTION]


async def _ensure_indexes() -> None:
    global _INDEXES_READY
    if _INDEXES_READY:
        return
    try:
        coll = await _coll()
        await coll.create_index(
            [("workflow_id", 1), ("key", 1)],
            unique=True,
            name="uq_workflow_state",
        )
        _INDEXES_READY = True
    except Exception as exc:  # noqa: BLE001
        logger.warning("workflow_state index init failed: %s", exc)


def _resolve_workflow_id(ctx: NodeContext, override: str = "") -> str:
    """Workflow id used as the state partition key.

    Preference order:
      1. Explicit ``workflow_id`` config field on the node (override).
      2. ``ctx.workflow_context['workflow_id']`` set by the executor.
      3. ``ctx.execution_id.split(':')[0]`` as a soft fallback.
    """
    if override.strip():
        return override.strip()
    if isinstance(ctx.workflow_context, dict):
        wf_id = ctx.workflow_context.get("workflow_id")
        if isinstance(wf_id, str) and wf_id:
            return wf_id
    if ctx.execution_id:
        return ctx.execution_id.split(":")[0]
    raise ValueError(
        "workflow_state node could not resolve a workflow_id "
        "(set the 'Workflow ID Override' field or run inside a real workflow)"
    )


@register_node
class WorkflowStateGetNode(BaseNode):
    """Read a per-workflow state value into ctx.variables.

    Typical placement: right after the trigger, so all downstream nodes can
    reference ``{{var.<key>}}`` (e.g. ``{{var.last_run_at}}``) in their
    config templates.

    Output items: passes the upstream items through unchanged. The state
    value lands in ``ctx.variables[key]`` for downstream interpolation AND
    is exposed as ``meta.value`` on this node's output.
    """

    node_type = NodeType.WORKFLOW_STATE_GET
    category = NodeCategory.PROCESSOR
    label = "Workflow State (Get)"
    description = (
        "Read a per-workflow state value (e.g. 'last_run_at') into "
        "the workflow's variables for downstream nodes to reference."
    )
    icon = "📥"
    color = "#0ea5e9"

    @classmethod
    def get_fields(cls) -> List[NodeFieldSchema]:
        return [
            NodeFieldSchema(
                name="key", label="State Key", type="text", required=True,
                placeholder="last_run_at",
                help_text=(
                    "Name of the state slot. Becomes available downstream "
                    "as {{var.<key>}} in node configs."
                ),
            ),
            NodeFieldSchema(
                name="default_value", label="Default (when missing)", type="text",
                placeholder="2024-01-01T00:00:00Z",
                help_text=(
                    "Returned (and written into ctx.variables) when no "
                    "prior state exists. For watermarks, set to a safe "
                    "lookback so the first run captures recent items."
                ),
            ),
            NodeFieldSchema(
                name="workflow_id_override", label="Workflow ID Override (optional)", type="text",
                placeholder="",
                help_text=(
                    "Use the current workflow's id by default. Override only "
                    "when reading state set by a sibling workflow."
                ),
            ),
        ]

    async def execute(self, ctx: NodeContext) -> Any:
        key = (ctx.config.get("key") or "").strip()
        if not key:
            raise ValueError("'State Key' is required")

        default_value = ctx.config.get("default_value")
        # Allow simple {{var.x}} interpolation in the default so a previous
        # node can seed it.
        if isinstance(default_value, str):
            default_value = interpolate_dotted(default_value, variables=ctx.variables)

        wf_id = _resolve_workflow_id(
            ctx, override=ctx.config.get("workflow_id_override") or ""
        )

        await _ensure_indexes()
        try:
            coll = await _coll()
            doc = await coll.find_one({"workflow_id": wf_id, "key": key})
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "WorkflowStateGet: read failed (%s) — using default for key=%s",
                exc, key,
            )
            doc = None

        value = doc.get("value") if isinstance(doc, dict) else None
        if value is None:
            value = default_value

        # Mutate ctx.variables in place so subsequent nodes interpolate it.
        ctx.variables[key] = value

        logger.info(
            "WorkflowStateGet: workflow_id=%s key=%s -> %r%s",
            wf_id, key, value,
            "" if doc else " (default)",
        )
        return self._make_output(
            items=ctx.items,           # pass-through
            key=key,
            value=value,
            workflow_id=wf_id,
            from_default=doc is None,
        )


@register_node
class WorkflowStateSetNode(BaseNode):
    """Write a per-workflow state value (typically the new watermark).

    Place at the end of a workflow once side-effects have succeeded — never
    update the watermark before downstream writes complete, otherwise a
    crash mid-run loses the unprocessed batch.
    """

    node_type = NodeType.WORKFLOW_STATE_SET
    category = NodeCategory.OUTPUT
    label = "Workflow State (Set)"
    description = (
        "Write a per-workflow state value (e.g. mark this run's "
        "'last_run_at'). Typically placed at the end of the DAG so the "
        "watermark only advances after side-effects succeed."
    )
    icon = "📤"
    color = "#0ea5e9"

    @classmethod
    def get_fields(cls) -> List[NodeFieldSchema]:
        return [
            NodeFieldSchema(
                name="key", label="State Key", type="text", required=True,
                placeholder="last_run_at",
            ),
            NodeFieldSchema(
                name="value", label="Value (supports {{var.x}} interpolation)", type="textarea",
                required=True,
                placeholder="{{var.run_started_at}}",
                help_text=(
                    "JSON literal OR an interpolated string. Common "
                    "patterns: '{{var.run_started_at}}' for time "
                    "watermarks, '{{var.high_id}}' for id watermarks."
                ),
            ),
            NodeFieldSchema(
                name="workflow_id_override", label="Workflow ID Override (optional)", type="text",
                placeholder="",
            ),
        ]

    async def execute(self, ctx: NodeContext) -> Any:
        import json as _json

        key = (ctx.config.get("key") or "").strip()
        if not key:
            raise ValueError("'State Key' is required")

        raw_value = ctx.config.get("value")
        if raw_value is None or (isinstance(raw_value, str) and not raw_value.strip()):
            raise ValueError("'Value' is required")

        # Interpolate first.
        if isinstance(raw_value, str):
            interpolated = interpolate_dotted(raw_value, variables=ctx.variables)
            # Try JSON parse; fall back to the literal string.
            try:
                value = _json.loads(interpolated)
            except (_json.JSONDecodeError, ValueError):
                value = interpolated
        else:
            value = raw_value

        wf_id = _resolve_workflow_id(
            ctx, override=ctx.config.get("workflow_id_override") or ""
        )

        await _ensure_indexes()
        try:
            coll = await _coll()
            await coll.update_one(
                {"workflow_id": wf_id, "key": key},
                {
                    "$set": {
                        "value": value,
                        "updated_at": datetime.utcnow(),
                        "updated_by_execution": ctx.execution_id or None,
                    },
                    "$setOnInsert": {
                        "workflow_id": wf_id,
                        "key": key,
                    },
                },
                upsert=True,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "WorkflowStateSet: write FAILED workflow_id=%s key=%s: %s",
                wf_id, key, exc,
            )
            raise

        logger.info(
            "WorkflowStateSet: workflow_id=%s key=%s -> %r",
            wf_id, key, value,
        )
        return self._make_output(
            items=ctx.items,           # pass-through
            key=key,
            value=value,
            workflow_id=wf_id,
        )
