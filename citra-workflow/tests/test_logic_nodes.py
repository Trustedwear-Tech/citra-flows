# Copyright (c) 2026 Trustedwear Tech Private Limited (https://citra-ai.com)
# Author: Rohit Kumar Chandan
# SPDX-License-Identifier: BUSL-1.1
#
# Licensed under the Business Source License 1.1. Non-production use is granted;
# production use requires a commercial licence until the Change Date, after
# which this file converts to Apache-2.0. See LICENSE at the repository root.

"""
Unit tests for logic nodes — Condition, Switch, Loop, Delay, Approval, ParallelSplit, MergeWait.
"""

import sys
import os
import pytest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from citra_workflow.nodes import NodeContext, get_node
from citra_workflow.models import NodeType


class TestConditionNode:

    @pytest.mark.asyncio
    async def test_equals_true(self):
        node = get_node(NodeType.CONDITION)
        ctx = NodeContext(
            node_id="c1", node_config={"field": "status", "operator": "==", "value": "active"},
            input_data={"status": "active"},
        )
        result = await node.execute(ctx)
        assert result["meta"]["condition_result"] is True
        assert result["meta"]["branch"] == "true"

    @pytest.mark.asyncio
    async def test_equals_false(self):
        node = get_node(NodeType.CONDITION)
        ctx = NodeContext(
            node_id="c2", node_config={"field": "status", "operator": "==", "value": "active"},
            input_data={"status": "inactive"},
        )
        result = await node.execute(ctx)
        assert result["meta"]["condition_result"] is False
        assert result["meta"]["branch"] == "false"

    @pytest.mark.asyncio
    async def test_contains(self):
        node = get_node(NodeType.CONDITION)
        ctx = NodeContext(
            node_id="c3", node_config={"field": "name", "operator": "contains", "value": "john"},
            input_data={"name": "John Smith"},
        )
        result = await node.execute(ctx)
        assert result["meta"]["condition_result"] is True

    @pytest.mark.asyncio
    async def test_empty_operator_none(self):
        node = get_node(NodeType.CONDITION)
        ctx = NodeContext(
            node_id="c4", node_config={"field": "email", "operator": "empty"},
            input_data={"email": None},
        )
        result = await node.execute(ctx)
        assert result["meta"]["condition_result"] is True

    @pytest.mark.asyncio
    async def test_empty_operator_string(self):
        node = get_node(NodeType.CONDITION)
        ctx = NodeContext(
            node_id="c5", node_config={"field": "email", "operator": "empty"},
            input_data={"email": ""},
        )
        result = await node.execute(ctx)
        assert result["meta"]["condition_result"] is True

    @pytest.mark.asyncio
    async def test_greater_than(self):
        node = get_node(NodeType.CONDITION)
        ctx = NodeContext(
            node_id="c6", node_config={"field": "count", "operator": ">", "value": "10"},
            input_data={"count": 15},
        )
        result = await node.execute(ctx)
        assert result["meta"]["condition_result"] is True

    @pytest.mark.asyncio
    async def test_less_than_false(self):
        node = get_node(NodeType.CONDITION)
        ctx = NodeContext(
            node_id="c7", node_config={"field": "count", "operator": "<", "value": "10"},
            input_data={"count": 15},
        )
        result = await node.execute(ctx)
        assert result["meta"]["condition_result"] is False

    @pytest.mark.asyncio
    async def test_nested_field_dot_notation(self):
        node = get_node(NodeType.CONDITION)
        ctx = NodeContext(
            node_id="c8", node_config={"field": "data.count", "operator": ">", "value": "5"},
            input_data={"data": {"count": 10}},
        )
        result = await node.execute(ctx)
        assert result["meta"]["condition_result"] is True

    @pytest.mark.asyncio
    async def test_not_empty(self):
        node = get_node(NodeType.CONDITION)
        ctx = NodeContext(
            node_id="c9", node_config={"field": "name", "operator": "not_empty"},
            input_data={"name": "Alice"},
        )
        result = await node.execute(ctx)
        assert result["meta"]["condition_result"] is True

    @pytest.mark.asyncio
    async def test_preserves_original_data(self):
        node = get_node(NodeType.CONDITION)
        input_items = [{"status": "active", "count": 42}]
        ctx = NodeContext(
            node_id="c10", node_config={"field": "items.0.status", "operator": "==", "value": "active"},
            input_data={"items": input_items, "meta": {}},
        )
        result = await node.execute(ctx)
        assert result["items"] == input_items


class TestSwitchRouterNode:

    @pytest.mark.asyncio
    async def test_exact_match(self):
        node = get_node(NodeType.SWITCH_ROUTER)
        ctx = NodeContext(
            node_id="sw1",
            node_config={
                "field": "category",
                "routes": [
                    {"label": "A", "value": "alpha"},
                    {"label": "B", "value": "beta"},
                    {"label": "Default", "value": "__default__"},
                ],
            },
            input_data={"category": "alpha"},
        )
        result = await node.execute(ctx)
        assert result["meta"]["matched_route"] == 0
        assert result["meta"]["matched_label"] == "A"

    @pytest.mark.asyncio
    async def test_default_fallback(self):
        node = get_node(NodeType.SWITCH_ROUTER)
        ctx = NodeContext(
            node_id="sw2",
            node_config={
                "field": "category",
                "routes": [
                    {"label": "A", "value": "alpha"},
                    {"label": "Default", "value": "__default__"},
                ],
            },
            input_data={"category": "unknown"},
        )
        result = await node.execute(ctx)
        assert result["meta"]["matched_route"] == 1  # default index
        assert result["meta"]["matched_label"] == "Default"

    @pytest.mark.asyncio
    async def test_nested_field(self):
        node = get_node(NodeType.SWITCH_ROUTER)
        ctx = NodeContext(
            node_id="sw3",
            node_config={
                "field": "meta.type",
                "routes": [
                    {"label": "Type A", "value": "a"},
                    {"label": "Default", "value": "__default__"},
                ],
            },
            input_data={"meta": {"type": "a"}},
        )
        result = await node.execute(ctx)
        assert result["meta"]["matched_route"] == 0


class TestLoopNode:

    @pytest.mark.asyncio
    async def test_creates_batches(self):
        node = get_node(NodeType.LOOP)
        items = list(range(10))
        ctx = NodeContext(
            node_id="l1",
            node_config={"items_field": "records", "batch_size": 3},
            input_data={"records": items},
        )
        result = await node.execute(ctx)
        assert result["meta"]["total_items"] == 10
        assert result["meta"]["total_batches"] == 4  # ceil(10/3)
        assert len(result["meta"]["batches"]) == 4
        assert result["meta"]["batches"][0] == [0, 1, 2]
        assert result["meta"]["batches"][-1] == [9]

    @pytest.mark.asyncio
    async def test_batch_size_1(self):
        node = get_node(NodeType.LOOP)
        ctx = NodeContext(
            node_id="l2",
            node_config={"items_field": "records", "batch_size": 1},
            input_data={"records": ["a", "b", "c"]},
        )
        result = await node.execute(ctx)
        assert result["meta"]["total_batches"] == 3
        assert result["meta"]["batches"] == [["a"], ["b"], ["c"]]

    @pytest.mark.asyncio
    async def test_empty_items(self):
        node = get_node(NodeType.LOOP)
        ctx = NodeContext(
            node_id="l3",
            node_config={"items_field": "records"},
            input_data={"records": []},
        )
        result = await node.execute(ctx)
        assert result["meta"]["total_items"] == 0
        assert result["meta"]["total_batches"] == 0


class TestDelayNode:

    @pytest.mark.asyncio
    async def test_caps_at_3600(self):
        node = get_node(NodeType.DELAY)
        ctx = NodeContext(
            node_id="d1",
            node_config={"seconds": 9999},
            input_data={"data": "test"},
        )
        with patch("asyncio.sleep", return_value=None) as mock_sleep:
            result = await node.execute(ctx)
        assert result["meta"]["delayed_seconds"] == 3600
        mock_sleep.assert_awaited_once_with(3600)

    @pytest.mark.asyncio
    async def test_normal_delay(self):
        node = get_node(NodeType.DELAY)
        ctx = NodeContext(
            node_id="d2",
            node_config={"seconds": 5},
            input_data={"data": "test"},
        )
        with patch("asyncio.sleep", return_value=None) as mock_sleep:
            result = await node.execute(ctx)
        assert result["meta"]["delayed_seconds"] == 5
        mock_sleep.assert_awaited_once_with(5)

    @pytest.mark.asyncio
    async def test_preserves_input_data(self):
        node = get_node(NodeType.DELAY)
        input_data = {"records": [1, 2, 3]}
        ctx = NodeContext(
            node_id="d3",
            node_config={"seconds": 1},
            input_data=input_data,
        )
        with patch("asyncio.sleep", return_value=None):
            result = await node.execute(ctx)
        assert result["items"] == ctx.items


class TestHumanApprovalNode:

    @pytest.mark.asyncio
    async def test_returns_waiting_status(self):
        node = get_node(NodeType.HUMAN_APPROVAL)
        ctx = NodeContext(
            node_id="ha1",
            node_config={"message": "Please review", "timeout_hours": 48},
            input_data={"records": [{"name": "item1"}]},
        )
        result = await node.execute(ctx)
        assert result["meta"]["status"] == "waiting_approval"
        assert result["meta"]["message"] == "Please review"
        assert result["meta"]["timeout_hours"] == 48


class TestParallelSplitNode:

    @pytest.mark.asyncio
    async def test_passes_data_through(self):
        node = get_node(NodeType.PARALLEL_SPLIT)
        input_data = {"records": [1, 2, 3]}
        ctx = NodeContext(node_id="ps1", node_config={}, input_data=input_data)
        result = await node.execute(ctx)
        assert result["items"] == ctx.items
        assert result["meta"]["split"] is True


class TestMergeWaitNode:

    @pytest.mark.asyncio
    async def test_collects_list_branches(self):
        node = get_node(NodeType.MERGE_WAIT)
        branches = [{"result": "a"}, {"result": "b"}, {"result": "c"}]
        ctx = NodeContext(node_id="mw1", node_config={}, input_data=branches)
        result = await node.execute(ctx)
        assert result["items"] == branches
        assert result["meta"]["branch_count"] == 3

    @pytest.mark.asyncio
    async def test_wraps_single_input(self):
        node = get_node(NodeType.MERGE_WAIT)
        ctx = NodeContext(node_id="mw2", node_config={}, input_data={"result": "a"})
        result = await node.execute(ctx)
        assert result["meta"]["branch_count"] == 1
