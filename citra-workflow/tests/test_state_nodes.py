# Copyright (c) 2026 Trustedwear Tech Private Limited (https://citra-ai.com)
# Author: Rohit Kumar Chandan
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may not
# use this file except in compliance with the License. You may obtain a copy of
# the License at http://www.apache.org/licenses/LICENSE-2.0

"""Smoke tests for WorkflowStateGetNode + WorkflowStateSetNode.

Mongo is mocked end-to-end so the tests don't require a live cluster.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from citra_workflow.models import NodeType  # noqa: E402
from citra_workflow.nodes import (  # noqa: E402
    NodeContext,
    _NODE_REGISTRY,
    get_node,
)
from citra_workflow.nodes.state import (  # noqa: E402
    WorkflowStateGetNode,
    WorkflowStateSetNode,
    _resolve_workflow_id,
)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_state_nodes_are_registered():
    assert NodeType.WORKFLOW_STATE_GET in _NODE_REGISTRY
    assert NodeType.WORKFLOW_STATE_SET in _NODE_REGISTRY
    assert isinstance(get_node(NodeType.WORKFLOW_STATE_GET), WorkflowStateGetNode)
    assert isinstance(get_node(NodeType.WORKFLOW_STATE_SET), WorkflowStateSetNode)


# ---------------------------------------------------------------------------
# _resolve_workflow_id
# ---------------------------------------------------------------------------


def _ctx(workflow_context=None, execution_id="") -> NodeContext:
    return NodeContext(
        node_id="n1",
        node_config={},
        input_data={"items": []},
        variables={},
        user_id="u1",
        execution_id=execution_id,
        environment="test",
        workflow_context=workflow_context,
    )


def test_resolve_workflow_id_uses_explicit_override():
    ctx = _ctx(workflow_context={"workflow_id": "wf_other"}, execution_id="exec_1")
    assert _resolve_workflow_id(ctx, override="wf_explicit") == "wf_explicit"


def test_resolve_workflow_id_uses_workflow_context():
    ctx = _ctx(workflow_context={"workflow_id": "wf_from_ctx"}, execution_id="exec_1")
    assert _resolve_workflow_id(ctx) == "wf_from_ctx"


def test_resolve_workflow_id_falls_back_to_execution_id():
    ctx = _ctx(workflow_context=None, execution_id="wf_abc:run_42")
    assert _resolve_workflow_id(ctx) == "wf_abc"


def test_resolve_workflow_id_raises_with_nothing():
    ctx = _ctx(workflow_context=None, execution_id="")
    with pytest.raises(ValueError, match="could not resolve a workflow_id"):
        _resolve_workflow_id(ctx)


# ---------------------------------------------------------------------------
# WorkflowStateGetNode.execute()
# ---------------------------------------------------------------------------


def _make_get_ctx(config: Dict[str, Any], execution_id: str = "wf_test:exec_1") -> NodeContext:
    return NodeContext(
        node_id="get-node",
        node_config=config,
        input_data={"items": [{"keep": "me"}]},
        variables={},
        user_id="u1",
        execution_id=execution_id,
        environment="test",
        workflow_context={"workflow_id": "wf_test"},
    )


@pytest.mark.asyncio
async def test_get_returns_existing_value():
    node = WorkflowStateGetNode()
    ctx = _make_get_ctx({"key": "last_run_at", "default_value": "2024-01-01"})

    fake_doc = {"workflow_id": "wf_test", "key": "last_run_at", "value": "2025-09-15T00:00:00Z"}
    fake_coll = MagicMock()
    fake_coll.find_one = AsyncMock(return_value=fake_doc)
    fake_coll.create_index = AsyncMock(return_value=None)

    with patch("citra_workflow.nodes.state._coll", new=AsyncMock(return_value=fake_coll)):
        result = await node.execute(ctx)

    assert result["meta"]["value"] == "2025-09-15T00:00:00Z"
    assert result["meta"]["from_default"] is False
    # ctx.variables mutated for downstream nodes:
    assert ctx.variables["last_run_at"] == "2025-09-15T00:00:00Z"
    # Items pass through:
    assert result["items"] == [{"keep": "me"}]


@pytest.mark.asyncio
async def test_get_returns_default_when_missing():
    node = WorkflowStateGetNode()
    ctx = _make_get_ctx({"key": "last_run_at", "default_value": "2024-01-01T00:00:00Z"})

    fake_coll = MagicMock()
    fake_coll.find_one = AsyncMock(return_value=None)
    fake_coll.create_index = AsyncMock(return_value=None)

    with patch("citra_workflow.nodes.state._coll", new=AsyncMock(return_value=fake_coll)):
        result = await node.execute(ctx)

    assert result["meta"]["value"] == "2024-01-01T00:00:00Z"
    assert result["meta"]["from_default"] is True
    assert ctx.variables["last_run_at"] == "2024-01-01T00:00:00Z"


@pytest.mark.asyncio
async def test_get_missing_key_raises():
    node = WorkflowStateGetNode()
    ctx = _make_get_ctx({"key": ""})
    with pytest.raises(ValueError, match="State Key"):
        await node.execute(ctx)


@pytest.mark.asyncio
async def test_get_swallows_mongo_failure_and_uses_default():
    node = WorkflowStateGetNode()
    ctx = _make_get_ctx({"key": "x", "default_value": "fallback"})

    async def boom():
        raise RuntimeError("mongo down")

    with patch("citra_workflow.nodes.state._coll", new=AsyncMock(side_effect=RuntimeError("mongo down"))):
        result = await node.execute(ctx)

    assert result["meta"]["value"] == "fallback"
    assert ctx.variables["x"] == "fallback"


# ---------------------------------------------------------------------------
# WorkflowStateSetNode.execute()
# ---------------------------------------------------------------------------


def _make_set_ctx(
    config: Dict[str, Any],
    variables: Dict[str, Any] = None,
) -> NodeContext:
    return NodeContext(
        node_id="set-node",
        node_config=config,
        input_data={"items": [{"x": 1}]},
        variables=variables or {},
        user_id="u1",
        execution_id="exec-99",
        environment="test",
        workflow_context={"workflow_id": "wf_test"},
    )


@pytest.mark.asyncio
async def test_set_writes_literal_string():
    node = WorkflowStateSetNode()
    ctx = _make_set_ctx({"key": "last_run_at", "value": "2025-12-01T10:00:00Z"})

    fake_coll = MagicMock()
    fake_coll.update_one = AsyncMock(return_value=None)
    fake_coll.create_index = AsyncMock(return_value=None)

    with patch("citra_workflow.nodes.state._coll", new=AsyncMock(return_value=fake_coll)):
        result = await node.execute(ctx)

    fake_coll.update_one.assert_awaited_once()
    args, kwargs = fake_coll.update_one.call_args
    assert kwargs.get("upsert") is True
    update_doc = args[1]
    assert update_doc["$set"]["value"] == "2025-12-01T10:00:00Z"
    assert result["meta"]["value"] == "2025-12-01T10:00:00Z"


@pytest.mark.asyncio
async def test_set_interpolates_variable():
    node = WorkflowStateSetNode()
    ctx = _make_set_ctx(
        {"key": "high_id", "value": "{{var.high_id}}"},
        variables={"high_id": "12345"},
    )

    fake_coll = MagicMock()
    fake_coll.update_one = AsyncMock(return_value=None)
    fake_coll.create_index = AsyncMock(return_value=None)

    with patch("citra_workflow.nodes.state._coll", new=AsyncMock(return_value=fake_coll)):
        result = await node.execute(ctx)

    args, _ = fake_coll.update_one.call_args
    # JSON parses "12345" → int 12345
    assert args[1]["$set"]["value"] == 12345
    assert result["meta"]["value"] == 12345


@pytest.mark.asyncio
async def test_set_falls_back_to_string_when_value_not_json():
    node = WorkflowStateSetNode()
    ctx = _make_set_ctx({"key": "label", "value": "any-string"})

    fake_coll = MagicMock()
    fake_coll.update_one = AsyncMock(return_value=None)
    fake_coll.create_index = AsyncMock(return_value=None)

    with patch("citra_workflow.nodes.state._coll", new=AsyncMock(return_value=fake_coll)):
        result = await node.execute(ctx)

    args, _ = fake_coll.update_one.call_args
    assert args[1]["$set"]["value"] == "any-string"
    assert result["meta"]["value"] == "any-string"


@pytest.mark.asyncio
async def test_set_missing_value_raises():
    node = WorkflowStateSetNode()
    ctx = _make_set_ctx({"key": "k", "value": ""})
    with pytest.raises(ValueError, match="'Value' is required"):
        await node.execute(ctx)
