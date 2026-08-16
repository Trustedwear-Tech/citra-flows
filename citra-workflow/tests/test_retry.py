"""
Unit tests for the node-level retry mechanism.
Validates retry logic in BaseNode.run(), backoff timing, retry fields in schema,
and retry info propagation through executor failure path.
"""

import sys
import os
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from citra_workflow.nodes import BaseNode, NodeContext
from citra_workflow.models import (
    NodeType, NodeCategory, NodeFieldSchema, NodeExecutionStatus, NodeExecutionResult,
    ExecutionStatus, WorkflowDefinition, NodeDefinition, EdgeDefinition,
)


# ============================================================================
# Concrete test node with controllable execute()
# ============================================================================

class _RetryTestNode(BaseNode):
    node_type = NodeType.MANUAL_TRIGGER
    category = NodeCategory.TRIGGER
    label = "Retry Test Node"
    description = "For retry tests"

    def __init__(self, execute_fn=None):
        self._execute_fn = execute_fn

    async def execute(self, ctx: NodeContext):
        if self._execute_fn:
            return self._execute_fn(ctx)
        return {"ok": True}


# ============================================================================
# Retry Schema Fields Tests
# ============================================================================

class TestRetrySchemaFields:

    def test_retry_fields_present_in_schema(self):
        """Every node schema should include max_retries, retry_delay_seconds, retry_backoff."""
        schema = _RetryTestNode.get_schema()
        field_names = [f.name for f in schema.fields]
        assert "max_retries" in field_names
        assert "retry_delay_seconds" in field_names
        assert "retry_backoff" in field_names

    def test_retry_fields_defaults(self):
        """Retry fields should have safe defaults (0 retries, 5s delay, fixed)."""
        schema = _RetryTestNode.get_schema()
        fields_by_name = {f.name: f for f in schema.fields}
        assert fields_by_name["max_retries"].default == 0
        assert fields_by_name["retry_delay_seconds"].default == 5
        assert fields_by_name["retry_backoff"].default == "fixed"

    def test_retry_fields_appended_after_node_fields(self):
        """Node-specific fields should come first, retry fields last."""

        class _FieldNode(_RetryTestNode):
            @classmethod
            def get_fields(cls):
                return [NodeFieldSchema(name="my_field", label="My Field", type="text")]

        schema = _FieldNode.get_schema()
        assert schema.fields[0].name == "my_field"
        assert schema.fields[-1].name == "retry_backoff"


# ============================================================================
# Retry Logic Tests
# ============================================================================

class TestRetryLogic:

    @pytest.mark.asyncio
    async def test_no_retry_by_default(self):
        """With max_retries=0 (default), a failing node should fail immediately with retry_count=0."""
        call_count = 0

        def _fail(ctx):
            nonlocal call_count
            call_count += 1
            raise RuntimeError("fail")

        node = _RetryTestNode(execute_fn=_fail)
        ctx = NodeContext(node_id="n1", node_config={}, input_data={})

        result = await node.run(ctx)

        assert result.status == NodeExecutionStatus.FAILED
        assert call_count == 1
        assert result.retry_count == 0
        assert len(result.retry_errors) == 1
        assert "fail" in result.retry_errors[0]

    @pytest.mark.asyncio
    async def test_retry_success_after_failures(self):
        """Node fails twice then succeeds on 3rd attempt → COMPLETED, retry_count=2."""
        call_count = 0

        def _fail_twice(ctx):
            nonlocal call_count
            call_count += 1
            if call_count <= 2:
                raise RuntimeError(f"fail #{call_count}")
            return {"result": "success"}

        node = _RetryTestNode(execute_fn=_fail_twice)
        ctx = NodeContext(
            node_id="n2",
            node_config={"max_retries": 3, "retry_delay_seconds": 0},
            input_data={},
        )

        result = await node.run(ctx)

        assert result.status == NodeExecutionStatus.COMPLETED
        assert result.output_data == {"result": "success"}
        assert result.retry_count == 2
        assert len(result.retry_errors) == 2
        assert "fail #1" in result.retry_errors[0]
        assert "fail #2" in result.retry_errors[1]
        assert call_count == 3

    @pytest.mark.asyncio
    async def test_retry_exhaustion(self):
        """Node fails all attempts → FAILED, retry_count=max_retries, all errors collected."""
        call_count = 0

        def _always_fail(ctx):
            nonlocal call_count
            call_count += 1
            raise RuntimeError(f"error #{call_count}")

        node = _RetryTestNode(execute_fn=_always_fail)
        ctx = NodeContext(
            node_id="n3",
            node_config={"max_retries": 2, "retry_delay_seconds": 0},
            input_data={},
        )

        result = await node.run(ctx)

        assert result.status == NodeExecutionStatus.FAILED
        assert result.retry_count == 2
        assert len(result.retry_errors) == 3  # 1 initial + 2 retries
        assert "error #3" in result.error  # Last error
        assert call_count == 3

    @pytest.mark.asyncio
    async def test_successful_node_has_zero_retries(self):
        """Node that succeeds on first try → retry_count=0, empty retry_errors."""
        node = _RetryTestNode(execute_fn=lambda ctx: {"ok": True})
        ctx = NodeContext(
            node_id="n4",
            node_config={"max_retries": 3, "retry_delay_seconds": 0},
            input_data={},
        )

        result = await node.run(ctx)

        assert result.status == NodeExecutionStatus.COMPLETED
        assert result.retry_count == 0
        assert result.retry_errors == []

    @pytest.mark.asyncio
    async def test_fixed_backoff_timing(self):
        """Fixed backoff: each retry waits the same delay."""
        sleep_calls = []

        def _always_fail(ctx):
            raise RuntimeError("boom")

        node = _RetryTestNode(execute_fn=_always_fail)
        ctx = NodeContext(
            node_id="n5",
            node_config={"max_retries": 2, "retry_delay_seconds": 3, "retry_backoff": "fixed"},
            input_data={},
        )

        with patch("citra_workflow.nodes.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            mock_sleep.side_effect = lambda d: sleep_calls.append(d)
            await node.run(ctx)

        # 2 retries, each ~3s fixed delay ± jitter (RETRY_JITTER_FRACTION=0.2).
        assert len(sleep_calls) == 2
        for d in sleep_calls:
            assert 3 * 0.8 <= d <= 3 * 1.2

    @pytest.mark.asyncio
    async def test_exponential_backoff_timing(self):
        """Exponential backoff: delay × 2^attempt."""
        sleep_calls = []

        def _always_fail(ctx):
            raise RuntimeError("boom")

        node = _RetryTestNode(execute_fn=_always_fail)
        ctx = NodeContext(
            node_id="n6",
            node_config={"max_retries": 3, "retry_delay_seconds": 2, "retry_backoff": "exponential"},
            input_data={},
        )

        with patch("citra_workflow.nodes.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            mock_sleep.side_effect = lambda d: sleep_calls.append(d)
            await node.run(ctx)

        # attempt 0: 2×2^0=2, attempt 1: 2×2^1=4, attempt 2: 2×2^2=8 — each ± jitter.
        assert len(sleep_calls) == 3
        for d, base in zip(sleep_calls, [2, 4, 8]):
            assert base * 0.8 <= d <= base * 1.2

    @pytest.mark.asyncio
    async def test_backward_compat_no_config(self):
        """Existing nodes with no retry config → identical behavior to before."""
        def _fail(ctx):
            raise RuntimeError("old error")

        node = _RetryTestNode(execute_fn=_fail)
        ctx = NodeContext(node_id="n7", node_config={}, input_data={})

        result = await node.run(ctx)

        assert result.status == NodeExecutionStatus.FAILED
        assert "old error" in result.error
        assert result.retry_count == 0


# ============================================================================
# NodeExecutionResult Model Tests
# ============================================================================

class TestNodeExecutionResultRetryFields:

    def test_retry_fields_default(self):
        """retry_count defaults to 0, retry_errors defaults to []."""
        result = NodeExecutionResult(
            node_id="n1", status=NodeExecutionStatus.COMPLETED
        )
        assert result.retry_count == 0
        assert result.retry_errors == []

    def test_retry_fields_serialization(self):
        """retry_count and retry_errors should round-trip through model_dump()."""
        result = NodeExecutionResult(
            node_id="n1",
            status=NodeExecutionStatus.FAILED,
            error="last error",
            retry_count=3,
            retry_errors=["err1", "err2", "err3"],
        )
        d = result.model_dump()
        assert d["retry_count"] == 3
        assert d["retry_errors"] == ["err1", "err2", "err3"]


# ============================================================================
# Executor Integration: Retry Info in Failure Notification
# ============================================================================

class TestExecutorRetryIntegration:
    """Verify retry_count/retry_errors flow from node → executor → failure notification."""

    @pytest.mark.asyncio
    async def test_executor_passes_retry_info_to_notify(self, mock_executor, make_node, make_edge):
        """When a node fails after retries, executor._notify_failure receives retry info."""
        wf = WorkflowDefinition(
            workflow_id="wf-retry",
            user_id="test-user",
            name="Retry Failure Test",
            nodes=[
                make_node("trigger", NodeType.MANUAL_TRIGGER),
                make_node("proc", NodeType.LLM_PROCESSOR, label="Processor"),
            ],
            edges=[make_edge("trigger", "proc")],
        )

        def node_factory(node_type):
            instance = AsyncMock()

            async def _run(ctx):
                if ctx.node_id == "proc":
                    return NodeExecutionResult(
                        node_id="proc",
                        status=NodeExecutionStatus.FAILED,
                        error="API timeout",
                        retry_count=2,
                        retry_errors=["API timeout", "API timeout", "API timeout"],
                    )
                return NodeExecutionResult(
                    node_id=ctx.node_id,
                    status=NodeExecutionStatus.COMPLETED,
                    output_data={"triggered": True},
                )

            instance.run = AsyncMock(side_effect=_run)
            return instance

        mock_executor._notify_failure = AsyncMock()

        with patch("citra_workflow.executor.get_node", side_effect=node_factory):
            result = await mock_executor.execute(wf)

        assert result.status == ExecutionStatus.FAILED
        mock_executor._notify_failure.assert_called_once()
        call_kwargs = mock_executor._notify_failure.call_args
        # Positional args: (execution, workflow, ...)
        assert call_kwargs.kwargs["retry_count"] == 2
        assert len(call_kwargs.kwargs["retry_errors"]) == 3

    @pytest.mark.asyncio
    async def test_executor_retry_info_in_node_results(self, mock_executor, make_node, make_edge):
        """After retry exhaustion, node_results should include retry_count and retry_errors."""
        wf = WorkflowDefinition(
            workflow_id="wf-retry-2",
            user_id="test-user",
            name="Retry Info Test",
            nodes=[
                make_node("trigger", NodeType.MANUAL_TRIGGER),
                make_node("api", NodeType.API_SOURCE, label="API Call", config={
                    "max_retries": 1, "retry_delay_seconds": 0,
                }),
            ],
            edges=[make_edge("trigger", "api")],
        )

        call_count = 0

        def node_factory(node_type):
            instance = AsyncMock()

            async def _run(ctx):
                nonlocal call_count
                if ctx.node_id == "api":
                    call_count += 1
                    return NodeExecutionResult(
                        node_id="api",
                        status=NodeExecutionStatus.FAILED,
                        error="Connection refused",
                        retry_count=1,
                        retry_errors=["Connection refused", "Connection refused"],
                    )
                return NodeExecutionResult(
                    node_id=ctx.node_id,
                    status=NodeExecutionStatus.COMPLETED,
                    output_data={"triggered": True},
                )

            instance.run = AsyncMock(side_effect=_run)
            return instance

        mock_executor._notify_failure = AsyncMock()

        with patch("citra_workflow.executor.get_node", side_effect=node_factory):
            result = await mock_executor.execute(wf)

        assert result.status == ExecutionStatus.FAILED
        api_result = result.node_results["api"]
        assert api_result.retry_count == 1
        assert len(api_result.retry_errors) == 2
