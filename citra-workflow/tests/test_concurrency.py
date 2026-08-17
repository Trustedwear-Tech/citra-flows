# Copyright (c) 2026 Trustedwear Tech Private Limited (https://citra-ai.com)
# Author: Rohit Kumar Chandan
# SPDX-License-Identifier: BUSL-1.1
#
# Licensed under the Business Source License 1.1. Non-production use is granted;
# production use requires a commercial licence until the Change Date, after
# which this file converts to Apache-2.0. See LICENSE at the repository root.

"""
Unit tests for concurrency-based execution gating.
Verifies acquire/release slot logic, ConcurrencyLimitError, and capacity endpoint.
"""

import sys
import os
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from citra_workflow.executor import (
    WorkflowExecutor, ConcurrencyLimitError,
    MAX_CONCURRENT_EXECUTIONS, _CONCURRENCY_KEY, _CONCURRENCY_TTL,
)
from citra_workflow.models import (
    ExecutionStatus, NodeExecutionStatus, NodeType,
    WorkflowDefinition, NodeExecutionResult,
)


# ============================================================================
# Helpers
# ============================================================================

def _make_run_result(node_id, output_data=None, status=NodeExecutionStatus.COMPLETED):
    return NodeExecutionResult(
        node_id=node_id,
        status=status,
        output_data=output_data or {},
        started_at=datetime.utcnow(),
        completed_at=datetime.utcnow(),
        duration_ms=5,
    )


def _make_node_instance(output=None):
    inst = AsyncMock()
    async def _run(ctx):
        return _make_run_result(ctx.node_id, output or {"ok": True})
    inst.run = AsyncMock(side_effect=_run)
    return inst


# ============================================================================
# TestAcquireRelease
# ============================================================================

class TestAcquireRelease:
    """Test _acquire_slot / _release_slot Lua-based concurrency gate."""

    def test_acquire_slot_success(self, mock_executor):
        """When eval returns 1, slot is acquired."""
        mock_executor.cache.eval.return_value = 1
        assert mock_executor._acquire_slot() is True
        mock_executor.cache.eval.assert_called_once()

    def test_acquire_slot_at_capacity(self, mock_executor):
        """When eval returns 0, slot is denied."""
        mock_executor.cache.eval.return_value = 0
        assert mock_executor._acquire_slot() is False

    def test_acquire_slot_redis_error_failopen(self, mock_executor):
        """If Redis is down, fail-open: allow execution."""
        mock_executor.cache.eval.side_effect = Exception("Redis down")
        assert mock_executor._acquire_slot() is True

    def test_release_slot_success(self, mock_executor):
        """Release should call eval without error."""
        mock_executor.cache.eval.return_value = 5
        mock_executor._release_slot()
        assert mock_executor.cache.eval.call_count == 1

    def test_release_slot_redis_error_no_raise(self, mock_executor):
        """Release should not raise even if Redis fails."""
        mock_executor.cache.eval.side_effect = Exception("Redis down")
        mock_executor._release_slot()  # should not raise


# ============================================================================
# TestConcurrencyInExecute
# ============================================================================

class TestConcurrencyInExecute:
    """Test that execute() acquires and releases concurrency slots."""

    @pytest.mark.asyncio
    async def test_execute_acquires_and_releases_slot(self, mock_executor, sample_linear_workflow):
        """Successful execution should acquire before run and release after."""
        call_order = []
        original_eval = mock_executor.cache.eval

        def tracking_eval(*args, **kwargs):
            # First call is acquire, second is release
            call_order.append("eval")
            return 1

        mock_executor.cache.eval = MagicMock(side_effect=tracking_eval)

        with patch("citra_workflow.executor.get_node",
                   return_value=_make_node_instance()):
            result = await mock_executor.execute(sample_linear_workflow)

        assert result.status == ExecutionStatus.COMPLETED
        # At least 2 eval calls: 1 acquire + 1 release
        assert mock_executor.cache.eval.call_count >= 2

    @pytest.mark.asyncio
    async def test_execute_raises_concurrency_limit(self, mock_executor, sample_linear_workflow):
        """When at capacity, execute() raises ConcurrencyLimitError."""
        mock_executor.cache.eval.return_value = 0  # capacity full

        with pytest.raises(ConcurrencyLimitError, match="at capacity"):
            await mock_executor.execute(sample_linear_workflow)

    @pytest.mark.asyncio
    async def test_execute_releases_slot_on_failure(self, mock_executor, sample_linear_workflow):
        """Slot should be released even when execution fails."""
        eval_calls = []

        def tracking_eval(*args, **kwargs):
            eval_calls.append(args)
            return 1

        mock_executor.cache.eval = MagicMock(side_effect=tracking_eval)

        def failing_node(node_type):
            inst = AsyncMock()
            async def _run(ctx):
                return _make_run_result(ctx.node_id, status=NodeExecutionStatus.FAILED)
            inst.run = AsyncMock(side_effect=_run)
            return inst

        with patch("citra_workflow.executor.get_node", side_effect=failing_node):
            result = await mock_executor.execute(sample_linear_workflow)

        assert result.status == ExecutionStatus.FAILED
        # Should have release call (acquire + release = 2)
        assert len(eval_calls) >= 2

    @pytest.mark.asyncio
    async def test_execute_skip_concurrency_when_disabled(self, mock_executor, sample_linear_workflow):
        """enforce_concurrency=False should skip the concurrency gate entirely."""
        mock_executor.cache.eval.return_value = 0  # would deny if checked

        with patch("citra_workflow.executor.get_node",
                   return_value=_make_node_instance()):
            result = await mock_executor.execute(
                sample_linear_workflow, enforce_concurrency=False
            )

        assert result.status == ExecutionStatus.COMPLETED
        # eval should not be called for acquire/release
        # (only cache.set via _update_progress may be called)
        assert mock_executor.cache.eval.call_count == 0


# ============================================================================
# TestGetCapacity
# ============================================================================

class TestGetCapacity:
    """Test the get_capacity() method."""

    def test_capacity_with_running_executions(self, mock_executor):
        """Should report correct running/available counts."""
        mock_executor.cache.get.return_value = "5"
        result = mock_executor.get_capacity()

        assert result["max_concurrent"] == MAX_CONCURRENT_EXECUTIONS
        assert result["running"] == 5
        assert result["available"] == MAX_CONCURRENT_EXECUTIONS - 5

    def test_capacity_with_no_executions(self, mock_executor):
        """When key doesn't exist, running=0."""
        mock_executor.cache.get.return_value = None
        result = mock_executor.get_capacity()

        assert result["running"] == 0
        assert result["available"] == MAX_CONCURRENT_EXECUTIONS

    def test_capacity_redis_error(self, mock_executor):
        """When Redis fails, return 0 running."""
        mock_executor.cache.get.side_effect = Exception("Redis down")
        result = mock_executor.get_capacity()

        assert result["running"] == 0
        assert result["available"] == MAX_CONCURRENT_EXECUTIONS
