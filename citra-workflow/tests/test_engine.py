# Copyright (c) 2026 Trustedwear Tech Private Limited (https://citra-ai.com)
# Author: Rohit Kumar Chandan
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may not
# use this file except in compliance with the License. You may obtain a copy of
# the License at http://www.apache.org/licenses/LICENSE-2.0

"""
Unit Tests for Workflow Engine — Variables, Interpolation, Security
===================================================================

Tests:
  - interpolate_variables()     (pure)
  - sql_parameterize()          (pure)
  - sanitize_remote_path()      (pure)
  - SetVariableNode.execute()   (async, no mocks)
  - WebhookTriggerNode.execute() validation (async)
  - Executor variable merging   (async, lightweight mock)
"""

import pytest
import asyncio
import os
import sys

SERVICE_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if SERVICE_ROOT not in sys.path:
    sys.path.insert(0, SERVICE_ROOT)

os.environ["DISABLE_AUTH"] = "true"

from citra_workflow.nodes import (
    interpolate_variables,
    sql_parameterize,
    sanitize_remote_path,
    NodeContext,
)


# ── interpolate_variables ──────────────────────────────────────────────

class TestInterpolateVariables:
    def test_basic_replacement(self):
        assert interpolate_variables("Hello {{name}}", {"name": "Alice"}) == "Hello Alice"

    def test_multiple_replacements(self):
        result = interpolate_variables("{{a}} + {{b}} = {{c}}", {"a": "1", "b": "2", "c": "3"})
        assert result == "1 + 2 = 3"

    def test_unresolved_left_intact(self):
        assert interpolate_variables("{{known}} {{unknown}}", {"known": "ok"}) == "ok {{unknown}}"

    def test_empty_text(self):
        assert interpolate_variables("", {"x": "1"}) == ""

    def test_none_text(self):
        assert interpolate_variables(None, {"x": "1"}) is None

    def test_empty_variables(self):
        assert interpolate_variables("{{x}}", {}) == "{{x}}"

    def test_none_variables(self):
        assert interpolate_variables("{{x}}", None) == "{{x}}"

    def test_numeric_value_cast(self):
        assert interpolate_variables("id={{id}}", {"id": 42}) == "id=42"

    def test_no_placeholders(self):
        assert interpolate_variables("plain text", {"x": "1"}) == "plain text"


# ── sql_parameterize ──────────────────────────────────────────────────

class TestSqlParameterize:
    def test_basic_parameterization(self):
        query, params = sql_parameterize(
            "SELECT * FROM users WHERE id = {{user_id}}",
            {"user_id": 42},
        )
        assert query == "SELECT * FROM users WHERE id = :user_id"
        assert params == {"user_id": 42}

    def test_multiple_params(self):
        query, params = sql_parameterize(
            "SELECT * FROM t WHERE a = {{x}} AND b = {{y}}",
            {"x": "foo", "y": 10},
        )
        assert query == "SELECT * FROM t WHERE a = :x AND b = :y"
        assert params == {"x": "foo", "y": 10}

    def test_unresolved_placeholder(self):
        query, params = sql_parameterize(
            "SELECT * FROM t WHERE a = {{known}} AND b = {{unknown}}",
            {"known": 1},
        )
        assert ":known" in query
        assert "{{unknown}}" in query
        assert params == {"known": 1}

    def test_empty_query(self):
        query, params = sql_parameterize("", {"x": 1})
        assert query == ""
        assert params == {}

    def test_no_variables(self):
        query, params = sql_parameterize("SELECT 1", {})
        assert query == "SELECT 1"
        assert params == {}

    def test_injection_blocked(self):
        """Values go through bind params, not string concat — SQL injection impossible."""
        query, params = sql_parameterize(
            "SELECT * FROM users WHERE name = {{name}}",
            {"name": "'; DROP TABLE users; --"},
        )
        assert query == "SELECT * FROM users WHERE name = :name"
        assert params["name"] == "'; DROP TABLE users; --"
        assert "DROP" not in query


# ── sanitize_remote_path ─────────────────────────────────────────────

class TestSanitizeRemotePath:
    def test_normal_path(self):
        assert sanitize_remote_path("/data/reports/file.csv") == "/data/reports/file.csv"

    def test_traversal_blocked(self):
        with pytest.raises(ValueError, match="Path traversal"):
            sanitize_remote_path("../../etc/passwd")

    def test_mid_path_traversal_blocked(self):
        with pytest.raises(ValueError, match="Path traversal"):
            sanitize_remote_path("/data/../../../etc/shadow")

    def test_relative_path_ok(self):
        result = sanitize_remote_path("reports/monthly/file.csv")
        assert result == "reports/monthly/file.csv"

    def test_empty_path(self):
        assert sanitize_remote_path("") == ""

    def test_normalizes_redundant_slashes(self):
        result = sanitize_remote_path("/data//reports///file.csv")
        assert "//" not in result

    def test_dot_segments_collapsed(self):
        result = sanitize_remote_path("/data/./reports/file.csv")
        assert result == "/data/reports/file.csv"


# ── SetVariableNode ──────────────────────────────────────────────────

class TestSetVariableNode:
    @pytest.fixture
    def ctx(self):
        return NodeContext(
            node_id="set-1",
            node_config={
                "assignments": [
                    {"name": "greeting", "value": "Hello {{name}}"},
                    {"name": "static_val", "value": "constant"},
                ]
            },
            input_data={"some": "data"},
            variables={"name": "Alice"},
            user_id="u1",
            execution_id="e1",
            environment="test",
        )

    @pytest.mark.asyncio
    async def test_sets_variables(self, ctx):
        from citra_workflow.nodes.logic import SetVariableNode
        node = SetVariableNode()
        result = await node.execute(ctx)
        # Nodes return the universal envelope: {items, meta: {...}}
        assert result["meta"]["variables_set"]["greeting"] == "Hello Alice"
        assert result["meta"]["variables_set"]["static_val"] == "constant"

    @pytest.mark.asyncio
    async def test_mutates_shared_dict(self, ctx):
        from citra_workflow.nodes.logic import SetVariableNode
        node = SetVariableNode()
        await node.execute(ctx)
        assert ctx.variables["greeting"] == "Hello Alice"
        assert ctx.variables["static_val"] == "constant"

    @pytest.mark.asyncio
    async def test_empty_assignments(self):
        from citra_workflow.nodes.logic import SetVariableNode
        ctx = NodeContext(
            node_id="set-2",
            node_config={"assignments": []},
            input_data={},
            variables={},
            user_id="u1",
            execution_id="e1",
            environment="test",
        )
        node = SetVariableNode()
        result = await node.execute(ctx)
        assert result["meta"]["variables_set"] == {}

    @pytest.mark.asyncio
    async def test_passes_through_input_data(self, ctx):
        from citra_workflow.nodes.logic import SetVariableNode
        node = SetVariableNode()
        result = await node.execute(ctx)
        # `data` is now nested under meta in the universal envelope
        assert result["meta"]["data"] == {"some": "data"}


# ── WebhookTriggerNode Validation ────────────────────────────────────

class TestWebhookTriggerValidation:
    @pytest.mark.asyncio
    async def test_valid_payload(self):
        from citra_workflow.nodes.triggers import WebhookTriggerNode
        ctx = NodeContext(
            node_id="wh-1",
            node_config={
                "input_schema": [
                    {"name": "user_id", "type": "number", "required": True},
                    {"name": "action", "type": "string", "required": False, "default": "create"},
                ]
            },
            input_data={"user_id": "42"},
            variables={},
            user_id="u1",
            execution_id="e1",
            environment="test",
        )
        node = WebhookTriggerNode()
        result = await node.execute(ctx)
        assert result["meta"]["variables"]["user_id"] == 42.0
        assert result["meta"]["variables"]["action"] == "create"

    @pytest.mark.asyncio
    async def test_missing_required_raises(self):
        from citra_workflow.nodes.triggers import WebhookTriggerNode
        ctx = NodeContext(
            node_id="wh-2",
            node_config={
                "input_schema": [
                    {"name": "user_id", "type": "number", "required": True},
                ]
            },
            input_data={},
            variables={},
            user_id="u1",
            execution_id="e1",
            environment="test",
        )
        node = WebhookTriggerNode()
        with pytest.raises(ValueError, match="Webhook validation failed"):
            await node.execute(ctx)

    @pytest.mark.asyncio
    async def test_no_schema_passes_through(self):
        from citra_workflow.nodes.triggers import WebhookTriggerNode
        ctx = NodeContext(
            node_id="wh-3",
            node_config={},
            input_data={"foo": "bar"},
            variables={},
            user_id="u1",
            execution_id="e1",
            environment="test",
        )
        node = WebhookTriggerNode()
        result = await node.execute(ctx)
        assert result["meta"]["variables"]["foo"] == "bar"


# ── Executor Variable Merging ────────────────────────────────────────

class TestExecutorVariableMerging:
    """Verify the executor merges 'variables' from node output_data back into
    the shared variables dict so downstream nodes see them."""

    @pytest.mark.asyncio
    async def test_trigger_variables_merged(self):
        from unittest.mock import AsyncMock, patch, MagicMock
        from citra_workflow.executor import WorkflowExecutor
        from citra_workflow.models import (
            WorkflowDefinition, NodeDefinition, EdgeDefinition,
            NodeExecutionResult, NodeExecutionStatus,
        )

        # Minimal 2-node workflow: trigger -> dummy
        workflow = WorkflowDefinition(
            workflow_id="wf-1",
            user_id="u1",
            name="test",
            nodes=[
                NodeDefinition(id="trigger", type="manual_trigger", label="Manual", config={}),
                NodeDefinition(id="dummy", type="start_node", label="Dummy", config={}),
            ],
            edges=[EdgeDefinition(source="trigger", target="dummy")],
        )

        call_log = {}

        # Mock get_node to return fake nodes. Nodes must emit the universal
        # envelope {items, meta} — the executor reads variables from
        # output_data["meta"]["variables"].
        async def fake_trigger_run(ctx):
            return NodeExecutionResult(
                node_id=ctx.node_id,
                status=NodeExecutionStatus.COMPLETED,
                output_data={
                    "items": [],
                    "meta": {
                        "triggered": True,
                        "variables": {"from_trigger": "hello"},
                    },
                },
            )

        async def fake_dummy_run(ctx):
            # Capture the variables visible to this node
            call_log["dummy_variables"] = dict(ctx.variables)
            return NodeExecutionResult(
                node_id=ctx.node_id,
                status=NodeExecutionStatus.COMPLETED,
                output_data={"done": True},
            )

        trigger_mock = MagicMock()
        trigger_mock.run = fake_trigger_run
        dummy_mock = MagicMock()
        dummy_mock.run = fake_dummy_run

        def fake_get_node(node_type):
            if node_type.value == "manual_trigger":
                return trigger_mock
            return dummy_mock

        executor = WorkflowExecutor.__new__(WorkflowExecutor)
        executor._update_progress = MagicMock()
        executor._save_execution = AsyncMock()
        executor._notify_failure = AsyncMock()

        with patch("citra_workflow.executor.get_node", side_effect=fake_get_node):
            result = await executor.execute(workflow)

        # The dummy node should see the variable emitted by trigger
        assert call_log["dummy_variables"].get("from_trigger") == "hello"
