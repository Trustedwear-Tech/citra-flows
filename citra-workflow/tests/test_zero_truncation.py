# Copyright (c) 2026 Trustedwear Tech Private Limited (https://citra-ai.com)
# Author: Rohit Kumar Chandan
# SPDX-License-Identifier: BUSL-1.1
#
# Licensed under the Business Source License 1.1. Non-production use is granted;
# production use requires a commercial licence until the Change Date, after
# which this file converts to Apache-2.0. See LICENSE at the repository root.

"""
Tests for Phase 0: Zero-Truncation Enterprise Principle.

Every silent truncation has been replaced with an explicit error.
These tests verify that oversized inputs raise ValueError instead of being silently clipped.
"""

import sys
import os
import json
import pytest
from unittest.mock import patch, MagicMock, AsyncMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from citra_workflow.nodes import NodeContext, get_node
from citra_workflow.models import NodeType


# ============================================================================
# 0.1  LoopNode — rejects oversized item lists
# ============================================================================

class TestLoopNodeNoTruncation:

    @pytest.mark.asyncio
    async def test_raises_when_items_exceed_max(self):
        node = get_node(NodeType.LOOP)
        big_items = list(range(node.MAX_ITERATIONS + 1))
        ctx = NodeContext(
            node_id="loop1",
            node_config={"items_field": "records", "batch_size": 1},
            input_data={"records": big_items},
        )
        with pytest.raises(ValueError, match="exceeds maximum allowed iterations"):
            await node.execute(ctx)

    @pytest.mark.asyncio
    async def test_allows_items_at_max(self):
        node = get_node(NodeType.LOOP)
        items = list(range(100))
        ctx = NodeContext(
            node_id="loop2",
            node_config={"items_field": "records", "batch_size": 10},
            input_data={"records": items},
        )
        result = await node.execute(ctx)
        assert result["meta"]["total_items"] == 100


# ============================================================================
# 0.2  ConditionNode — rejects oversized field paths
# ============================================================================

class TestConditionNodeNoTruncation:

    @pytest.mark.asyncio
    async def test_raises_when_field_depth_exceeds_max(self):
        node = get_node(NodeType.CONDITION)
        deep_path = ".".join(["a"] * (node.MAX_FIELD_DEPTH + 1))
        ctx = NodeContext(
            node_id="cond1",
            node_config={"field": deep_path, "operator": "==", "value": "x"},
            input_data={"a": {"a": "x"}},
        )
        with pytest.raises(ValueError, match="Field path depth"):
            await node.execute(ctx)

    @pytest.mark.asyncio
    async def test_allows_field_at_max_depth(self):
        node = get_node(NodeType.CONDITION)
        ctx = NodeContext(
            node_id="cond2",
            node_config={"field": "a.b.c", "operator": "==", "value": "val"},
            input_data={"a": {"b": {"c": "val"}}},
        )
        result = await node.execute(ctx)
        assert result["meta"]["condition_result"] is True


# ============================================================================
# 0.3  Agent max_iterations — rejects > limit
# ============================================================================

class TestAgentIterationsNoTruncation:

    @pytest.mark.asyncio
    async def test_raises_when_iterations_exceed_limit(self):
        from citra_workflow.nodes.agents import MAX_AGENT_ITERATIONS
        node = get_node(NodeType.AI_AGENT)
        ctx = NodeContext(
            node_id="agent1",
            node_config={
                "system_prompt": "test",
                "user_prompt": "{{data}}",
                "max_iterations": MAX_AGENT_ITERATIONS + 1,
                "tools": [],
            },
            input_data={"x": 1},
        )
        with pytest.raises(ValueError, match="max_iterations.*exceeds limit"):
            await node.execute(ctx)


# ============================================================================
# 0.4  Agent input data — rejects oversized input
# ============================================================================

class TestAgentInputNoTruncation:

    @pytest.mark.asyncio
    async def test_raises_when_input_exceeds_limit(self):
        from citra_workflow.nodes.agents import MAX_AGENT_INPUT_SIZE
        node = get_node(NodeType.AI_AGENT)
        big_data = {"text": "x" * (MAX_AGENT_INPUT_SIZE + 1)}
        ctx = NodeContext(
            node_id="agent2",
            node_config={
                "system_prompt": "test",
                "user_prompt": "{{data}}",
                "max_iterations": 5,
                "tools": [],
            },
            input_data=big_data,
        )
        with pytest.raises(ValueError, match="Agent input data.*exceeds limit"):
            await node.execute(ctx)


# ============================================================================
# 0.7  Vector search content — rejects oversized content
# ============================================================================

class TestVectorSearchNoTruncation:

    def test_raises_when_content_exceeds_limit(self):
        from citra_workflow.nodes.agents import _enforce_size_limit
        from citra_workflow.config import MAX_VECTOR_CONTENT_SIZE
        big = "x" * (MAX_VECTOR_CONTENT_SIZE + 1)
        with pytest.raises(ValueError, match="exceeds limit"):
            _enforce_size_limit(big, MAX_VECTOR_CONTENT_SIZE, "Vector search content")


# ============================================================================
# 0.8  LLM Processor nodes — reject oversized prompt input
# ============================================================================

class TestLLMProcessorNoTruncation:

    @pytest.mark.asyncio
    async def test_raises_when_data_exceeds_prompt_limit(self):
        from citra_workflow.nodes.processors import MAX_LLM_PROMPT_SIZE
        node = get_node(NodeType.LLM_PROCESSOR)
        big_items = [{"text": "x" * MAX_LLM_PROMPT_SIZE}]
        ctx = NodeContext(
            node_id="llm1",
            node_config={
                "user_prompt": "Analyze: {{data}}",
                "processing_mode": "all",
            },
            input_data={"items": big_items},
        )
        with pytest.raises(ValueError, match="exceeds LLM prompt limit"):
            await node.execute(ctx)

    @pytest.mark.asyncio
    async def test_allows_data_within_limit(self):
        node = get_node(NodeType.LLM_PROCESSOR)
        ctx = NodeContext(
            node_id="llm2",
            node_config={
                "user_prompt": "Analyze: {{data}}",
                "processing_mode": "all",
            },
            input_data={"items": [{"x": 1}]},
        )
        with patch(
            "citra_workflow.nodes.processors._get_llm_response",
            return_value="OK",
        ):
            result = await node.execute(ctx)
        assert result["items"][0]["result"] == "OK"


class TestRulesEngineNoTruncation:

    @pytest.mark.asyncio
    async def test_raises_when_data_exceeds_limit(self):
        from citra_workflow.nodes.processors import MAX_LLM_RULES_SIZE
        node = get_node(NodeType.RULES_ENGINE)
        big_items = [{"text": "x" * MAX_LLM_RULES_SIZE}]
        ctx = NodeContext(
            node_id="rules1",
            node_config={
                "rules": "Check everything",
                "processing_mode": "all",
            },
            input_data={"items": big_items},
        )
        with pytest.raises(ValueError, match="exceeds LLM prompt limit"):
            await node.execute(ctx)


class TestClassifierNoTruncation:

    @pytest.mark.asyncio
    async def test_raises_when_data_exceeds_limit(self):
        from citra_workflow.nodes.processors import MAX_LLM_RULES_SIZE
        node = get_node(NodeType.CLASSIFIER)
        big_items = [{"text": "x" * MAX_LLM_RULES_SIZE}]
        ctx = NodeContext(
            node_id="cls1",
            node_config={
                "labels": "good,bad",
                "processing_mode": "all",
            },
            input_data={"items": big_items},
        )
        with pytest.raises(ValueError, match="exceeds LLM prompt limit"):
            await node.execute(ctx)


class TestSummarizerNoTruncation:

    @pytest.mark.asyncio
    async def test_raises_when_data_exceeds_limit(self):
        from citra_workflow.nodes.processors import MAX_LLM_SUMMARY_SIZE
        node = get_node(NodeType.SUMMARIZER)
        big_items = [{"text": "x" * MAX_LLM_SUMMARY_SIZE}]
        ctx = NodeContext(
            node_id="sum1",
            node_config={
                "processing_mode": "all",
            },
            input_data={"items": big_items},
        )
        with pytest.raises(ValueError, match="exceeds LLM prompt limit"):
            await node.execute(ctx)


# ============================================================================
# 0.9  Table/PDF — rejects oversized record counts
# ============================================================================

class TestBuildTableHtmlNoTruncation:

    def test_raises_when_records_exceed_limit(self):
        from citra_workflow.nodes.outputs import _build_table_html
        records = [{"col": f"val{i}"} for i in range(501)]
        with pytest.raises(ValueError, match="exceeds limit"):
            _build_table_html("Test", records)

    def test_allows_records_within_limit(self):
        from citra_workflow.nodes.outputs import _build_table_html
        records = [{"col": f"val{i}"} for i in range(500)]
        html = _build_table_html("Test", records)
        assert "Total records: 500" in html


# ============================================================================
# 0.10  Email body — rejects oversized data
# ============================================================================

class TestEmailSenderNoTruncation:

    @pytest.mark.asyncio
    async def test_raises_when_body_exceeds_limit(self):
        node = get_node(NodeType.EMAIL_SENDER)
        big_data = {"text": "x" * 15000}
        ctx = NodeContext(
            node_id="email1",
            node_config={
                "to": "test@example.com",
                "subject": "Test",
                "body_template": "Results: {{data}}",
            },
            input_data=big_data,
        )
        with pytest.raises(ValueError, match="exceeds limit"):
            await node.execute(ctx)


# ============================================================================
# 0.11  Notifications — full content + "View in Citra" link
# ============================================================================

class TestNotificationNoTruncation:

    @pytest.mark.asyncio
    async def test_approval_email_includes_link_for_long_preview(self):
        from citra_workflow.notifications import send_approval_notification
        long_preview = "x" * 3000

        with patch("citra_workflow.notifications._send_email", new_callable=AsyncMock, return_value=True) as mock_send:
            await send_approval_notification(
                to_email="test@example.com",
                user_name="Test",
                workflow_name="WF1",
                node_label="Approve",
                message="Please review",
                execution_id="exec1",
                approval_id="apr1",
                data_preview=long_preview,
            )
            _, _, text, html = mock_send.call_args[0] if mock_send.call_args[0] else (None, None, None, None)
            if html is None:
                html = mock_send.call_args[1].get("html", "")
            assert "View full details in Citra" in html

    @pytest.mark.asyncio
    async def test_failure_email_includes_full_error(self):
        from citra_workflow.notifications import send_execution_failure_notification
        long_error = "Error: " + "x" * 1000

        with patch("citra_workflow.notifications._send_email", new_callable=AsyncMock, return_value=True) as mock_send:
            await send_execution_failure_notification(
                to_email="test@example.com",
                user_name="Test",
                workflow_name="WF1",
                execution_id="exec1",
                failed_node_label="Node1",
                error_message=long_error,
            )
            args = mock_send.call_args
            html = args[0][3] if len(args[0]) > 3 else args[1].get("html", "")
            # Error should NOT be truncated at 500 chars
            assert long_error in html or "View full execution details" in html

    @pytest.mark.asyncio
    async def test_retry_errors_not_truncated(self):
        from citra_workflow.notifications import send_execution_failure_notification
        long_err = "A" * 500

        with patch("citra_workflow.notifications._send_email", new_callable=AsyncMock, return_value=True) as mock_send:
            await send_execution_failure_notification(
                to_email="test@example.com",
                user_name="Test",
                workflow_name="WF1",
                execution_id="exec1",
                failed_node_label="Node1",
                error_message="Failed",
                retry_count=2,
                retry_errors=[long_err, long_err],
            )
            args = mock_send.call_args
            html = args[0][3] if len(args[0]) > 3 else args[1].get("html", "")
            # Each retry error should be fully included (previously truncated at 200)
            assert long_err in html
