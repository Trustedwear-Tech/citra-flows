# Copyright (c) 2026 Trustedwear Tech Private Limited (https://citra-ai.com)
# Author: Rohit Kumar Chandan
# SPDX-License-Identifier: BUSL-1.1
#
# Licensed under the Business Source License 1.1. Non-production use is granted;
# production use requires a commercial licence until the Change Date, after
# which this file converts to Apache-2.0. See LICENSE at the repository root.

"""
Unit tests for trigger nodes — ManualTrigger, StartNode, ScheduledTrigger, WebhookTrigger.
"""

import sys
import os
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from citra_workflow.nodes import NodeContext, get_node
from citra_workflow.models import NodeType


class TestManualTrigger:

    @pytest.mark.asyncio
    async def test_returns_correct_shape(self):
        node = get_node(NodeType.MANUAL_TRIGGER)
        ctx = NodeContext(node_id="t1", node_config={}, variables={"foo": "bar"})
        result = await node.execute(ctx)

        assert result["meta"]["triggered"] is True
        assert result["meta"]["trigger_type"] == "manual"
        assert result["meta"]["variables"]["foo"] == "bar"


class TestStartNode:

    @pytest.mark.asyncio
    async def test_validates_input_schema(self):
        node = get_node(NodeType.START_NODE)
        ctx = NodeContext(
            node_id="s1",
            node_config={
                "input_schema": [
                    {"name": "company", "type": "string", "required": True},
                    {"name": "revenue", "type": "number", "required": True},
                ],
            },
            input_data={},
            variables={},
        )
        result = await node.execute(ctx)
        assert result["meta"].get("valid") is False
        assert len(result["meta"].get("errors", [])) == 2

    @pytest.mark.asyncio
    async def test_coerces_number_type(self):
        node = get_node(NodeType.START_NODE)
        ctx = NodeContext(
            node_id="s2",
            node_config={
                "input_schema": [
                    {"name": "count", "type": "number", "required": True},
                ],
            },
            input_data={"count": "42"},
            variables={},
        )
        result = await node.execute(ctx)
        assert result["meta"]["triggered"] is True
        assert result["items"][0]["count"] == 42.0

    @pytest.mark.asyncio
    async def test_coerces_boolean_type(self):
        node = get_node(NodeType.START_NODE)
        ctx = NodeContext(
            node_id="s3",
            node_config={
                "input_schema": [
                    {"name": "active", "type": "boolean", "required": False},
                ],
            },
            input_data={"active": "yes"},
            variables={},
        )
        result = await node.execute(ctx)
        assert result["items"][0]["active"] is True

    @pytest.mark.asyncio
    async def test_default_value_used(self):
        node = get_node(NodeType.START_NODE)
        ctx = NodeContext(
            node_id="s4",
            node_config={
                "input_schema": [
                    {"name": "limit", "type": "number", "required": False, "default": 100},
                ],
            },
            input_data={},
            variables={},
        )
        result = await node.execute(ctx)
        assert result["items"][0]["limit"] == 100.0


class TestScheduledTrigger:

    @pytest.mark.asyncio
    async def test_includes_cron(self):
        node = get_node(NodeType.SCHEDULED_TRIGGER)
        ctx = NodeContext(
            node_id="st1",
            node_config={"cron_expression": "0 9 * * 1", "timezone": "US/Eastern"},
            variables={},
        )
        result = await node.execute(ctx)
        assert result["meta"]["triggered"] is True
        assert result["meta"]["trigger_type"] == "scheduled"
        assert result["meta"]["cron"] == "0 9 * * 1"


class TestWebhookTrigger:

    @pytest.mark.asyncio
    async def test_passes_payload(self):
        payload = {"event": "order.created", "data": {"id": 123}}
        node = get_node(NodeType.WEBHOOK_TRIGGER)
        ctx = NodeContext(
            node_id="wh1",
            node_config={},
            input_data=payload,
            variables={"token": "abc"},
        )
        result = await node.execute(ctx)
        assert result["meta"]["triggered"] is True
        assert result["meta"]["trigger_type"] == "webhook"
        assert result["meta"]["payload"] == payload
        assert result["meta"]["variables"]["token"] == "abc"
