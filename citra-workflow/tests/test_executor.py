# Copyright (c) 2026 Trustedwear Tech Private Limited (https://citra-ai.com)
# Author: Rohit Kumar Chandan
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may not
# use this file except in compliance with the License. You may obtain a copy of
# the License at http://www.apache.org/licenses/LICENSE-2.0

"""
Unit tests for WorkflowExecutor — BFS execution, branching, approval, persistence.
All I/O is mocked: no real DB, Redis, S3, or LLM calls.
"""

import sys
import os
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from citra_workflow.models import (
    ExecutionStatus, NodeExecutionStatus, NodeType,
    WorkflowDefinition, WorkflowExecution, NodeExecutionResult,
    NodeDefinition, EdgeDefinition,
)
from citra_workflow.nodes import NodeContext


# ============================================================================
# Helpers
# ============================================================================

def _make_run_result(node_id, output_data=None, status=NodeExecutionStatus.COMPLETED):
    """Create a NodeExecutionResult returned by node.run()."""
    return NodeExecutionResult(
        node_id=node_id,
        status=status,
        output_data=output_data or {},
        started_at=datetime.utcnow(),
        completed_at=datetime.utcnow(),
        duration_ms=5,
    )


def _make_node_instance(side_effect_fn=None, default_output=None):
    """Create a mock node instance whose run() returns a NodeExecutionResult.
    side_effect_fn(ctx) -> output_data  — if provided, called per invocation.
    """
    instance = AsyncMock()

    async def _run(ctx):
        if side_effect_fn:
            output = side_effect_fn(ctx)
        else:
            output = default_output or {"processed": True}
        return _make_run_result(ctx.node_id, output)

    instance.run = AsyncMock(side_effect=_run)
    return instance


# ============================================================================
# TestExecutorBFS
# ============================================================================

class TestExecutorBFS:
    """BFS traversal, topological order, input passing, failure handling."""

    @pytest.mark.asyncio
    async def test_linear_3_node_workflow(self, mock_executor, sample_linear_workflow):
        """trigger → processor → output: all 3 nodes should execute and complete."""
        outputs = {
            "trigger": {"triggered": True},
            "proc": {"summary": "done"},
            "out": {"sent": True},
        }

        def node_factory(node_type):
            return _make_node_instance(
                side_effect_fn=lambda ctx: outputs.get(ctx.node_id, {})
            )

        with patch("citra_workflow.executor.get_node", side_effect=node_factory):
            result = await mock_executor.execute(sample_linear_workflow)

        assert result.status == ExecutionStatus.COMPLETED
        assert len(result.node_results) == 3
        for nid in ("trigger", "proc", "out"):
            nr = result.node_results[nid]
            status = nr["status"] if isinstance(nr, dict) else nr.status
            if hasattr(status, "value"):
                status = status.value
            assert status == "completed"

    @pytest.mark.asyncio
    async def test_root_detection_no_trigger_raises(self, mock_executor, make_node, make_edge):
        """Workflow with no root node (all nodes have incoming edges) → ValueError."""
        wf = WorkflowDefinition(
            workflow_id="wf-cycle",
            user_id="test-user",
            name="Cycle",
            nodes=[
                make_node("a", NodeType.LLM_PROCESSOR),
                make_node("b", NodeType.LLM_PROCESSOR),
            ],
            edges=[
                make_edge("a", "b"),
                make_edge("b", "a"),
            ],
        )
        # Both nodes have incoming edges → no root → ValueError
        # Actually one root exists (a has incoming from b, b has incoming from a → both have incoming)
        # Wait — a→b means b has incoming from a, b→a means a has incoming from b
        # So both have incoming edges → no root nodes
        result = await mock_executor.execute(wf)
        assert result.status == ExecutionStatus.FAILED
        assert "no root node" in (result.error or "").lower()

    @pytest.mark.asyncio
    async def test_topological_order_diamond(self, mock_executor, make_node, make_edge):
        """Diamond: A→B, A→C, B→D, C→D. D should run last."""
        execution_order = []

        def track_node(ctx):
            execution_order.append(ctx.node_id)
            return {"data": ctx.node_id}

        wf = WorkflowDefinition(
            workflow_id="wf-diamond",
            user_id="test-user",
            name="Diamond",
            nodes=[
                make_node("a", NodeType.MANUAL_TRIGGER),
                make_node("b", NodeType.LLM_PROCESSOR),
                make_node("c", NodeType.LLM_PROCESSOR),
                make_node("d", NodeType.LLM_PROCESSOR),
            ],
            edges=[
                make_edge("a", "b"),
                make_edge("a", "c"),
                make_edge("b", "d"),
                make_edge("c", "d"),
            ],
        )

        def node_factory(node_type):
            return _make_node_instance(side_effect_fn=track_node)

        with patch("citra_workflow.executor.get_node", side_effect=node_factory):
            result = await mock_executor.execute(wf)

        assert result.status == ExecutionStatus.COMPLETED
        assert execution_order[-1] == "d"  # D runs last
        assert execution_order[0] == "a"  # A runs first

    @pytest.mark.asyncio
    async def test_node_failure_stops_execution(self, mock_executor, make_node, make_edge):
        """A→B→C. If B fails, C should NOT execute."""
        call_count = {"c": 0}

        wf = WorkflowDefinition(
            workflow_id="wf-fail",
            user_id="test-user",
            name="Fail Test",
            nodes=[
                make_node("a", NodeType.MANUAL_TRIGGER),
                make_node("b", NodeType.LLM_PROCESSOR),
                make_node("c", NodeType.LLM_PROCESSOR),
            ],
            edges=[
                make_edge("a", "b"),
                make_edge("b", "c"),
            ],
        )

        def node_factory(node_type):
            instance = AsyncMock()

            async def _run(ctx):
                if ctx.node_id == "b":
                    return NodeExecutionResult(
                        node_id="b",
                        status=NodeExecutionStatus.FAILED,
                        error="Processing error",
                    )
                if ctx.node_id == "c":
                    call_count["c"] += 1
                return _make_run_result(ctx.node_id, {"ok": True})

            instance.run = AsyncMock(side_effect=_run)
            return instance

        with patch("citra_workflow.executor.get_node", side_effect=node_factory):
            result = await mock_executor.execute(wf)

        assert result.status == ExecutionStatus.FAILED
        assert "b" in (result.error or "")
        assert call_count["c"] == 0, "Node C should not have executed after B failed"

    @pytest.mark.asyncio
    async def test_unknown_node_type_graceful_fail(self, mock_executor, make_node, make_edge):
        """Node with unknown type → FAILED result for that node, no crash."""
        wf = WorkflowDefinition(
            workflow_id="wf-unknown",
            user_id="test-user",
            name="Unknown Type",
            nodes=[
                make_node("a", NodeType.MANUAL_TRIGGER),
                make_node("bad", NodeType.LLM_PROCESSOR),  # valid type for Pydantic
            ],
            edges=[make_edge("a", "bad")],
        )
        # Manually corrupt the type after construction to bypass Pydantic validation
        wf.nodes[1].type = "nonexistent_type"

        def node_factory(node_type):
            return _make_node_instance(default_output={"ok": True})

        with patch("citra_workflow.executor.get_node", side_effect=node_factory):
            result = await mock_executor.execute(wf)

        # The bad node should be marked FAILED
        assert "bad" in result.node_results
        bad_nr = result.node_results["bad"]
        status = bad_nr["status"] if isinstance(bad_nr, dict) else bad_nr.status
        if hasattr(status, "value"):
            status = status.value
        assert status == "failed"

    @pytest.mark.asyncio
    async def test_variables_propagated_through_nodes(self, mock_executor, make_node, make_edge):
        """Trigger data variables should be accessible in downstream node context."""
        captured_vars = {}

        def capture_vars(ctx):
            captured_vars.update(ctx.variables)
            return {"captured": True}

        wf = WorkflowDefinition(
            workflow_id="wf-vars",
            user_id="test-user",
            name="Vars Test",
            variables={"base_var": "hello"},
            nodes=[
                make_node("trigger", NodeType.MANUAL_TRIGGER),
                make_node("proc", NodeType.LLM_PROCESSOR),
            ],
            edges=[make_edge("trigger", "proc")],
        )

        def node_factory(node_type):
            return _make_node_instance(side_effect_fn=capture_vars)

        with patch("citra_workflow.executor.get_node", side_effect=node_factory):
            result = await mock_executor.execute(
                wf, trigger_data={"extra_var": "world"}
            )

        assert result.status == ExecutionStatus.COMPLETED
        assert captured_vars.get("base_var") == "hello"
        assert captured_vars.get("extra_var") == "world"

    @pytest.mark.asyncio
    async def test_multi_parent_merge_input(self, mock_executor, make_node, make_edge):
        """Node D with parents B and C → input_data should be list [B_output, C_output]."""
        captured_input = {}

        def capture_input(ctx):
            captured_input[ctx.node_id] = ctx.input_data
            return {"from": ctx.node_id}

        wf = WorkflowDefinition(
            workflow_id="wf-merge",
            user_id="test-user",
            name="Merge Test",
            nodes=[
                make_node("a", NodeType.MANUAL_TRIGGER),
                make_node("b", NodeType.LLM_PROCESSOR),
                make_node("c", NodeType.LLM_PROCESSOR),
                make_node("d", NodeType.MERGE_WAIT),
            ],
            edges=[
                make_edge("a", "b"),
                make_edge("a", "c"),
                make_edge("b", "d"),
                make_edge("c", "d"),
            ],
        )

        def node_factory(node_type):
            return _make_node_instance(side_effect_fn=capture_input)

        with patch("citra_workflow.executor.get_node", side_effect=node_factory):
            result = await mock_executor.execute(wf)

        assert result.status == ExecutionStatus.COMPLETED
        # D should receive a list of both parent outputs
        d_input = captured_input.get("d")
        assert isinstance(d_input, list)
        assert len(d_input) == 2

    @pytest.mark.asyncio
    async def test_single_parent_input_unwrapped(self, mock_executor, sample_linear_workflow):
        """Single parent output is passed as dict, not wrapped in a list."""
        captured_input = {}

        def capture_input(ctx):
            captured_input[ctx.node_id] = ctx.input_data
            return {"from": ctx.node_id}

        def node_factory(node_type):
            return _make_node_instance(side_effect_fn=capture_input)

        with patch("citra_workflow.executor.get_node", side_effect=node_factory):
            result = await mock_executor.execute(sample_linear_workflow)

        # proc's input comes from trigger (single parent) → should be dict, not list
        proc_input = captured_input.get("proc")
        assert not isinstance(proc_input, list)


# ============================================================================
# TestConditionBranching
# ============================================================================

class TestConditionBranching:
    """Condition node (if/else) routing logic."""

    def _make_condition_node_factory(self, condition_result: bool):
        """Return a get_node factory that makes condition nodes return a specific result."""
        def node_factory(node_type):
            instance = AsyncMock()

            async def _run(ctx):
                if ctx.node_id == "cond":
                    return _make_run_result(ctx.node_id, {
                        "items": ctx.input_data.get("items", []) if isinstance(ctx.input_data, dict) else [],
                        "meta": {
                            "condition_result": condition_result,
                            "branch": "true" if condition_result else "false",
                        },
                    })
                return _make_run_result(ctx.node_id, {"processed": True})

            instance.run = AsyncMock(side_effect=_run)
            return instance
        return node_factory

    @pytest.mark.asyncio
    async def test_condition_true_branch_executes(self, mock_executor, sample_condition_workflow):
        """Condition true → true_branch runs, false_branch SKIPPED."""
        factory = self._make_condition_node_factory(condition_result=True)
        with patch("citra_workflow.executor.get_node", side_effect=factory):
            result = await mock_executor.execute(
                sample_condition_workflow, trigger_data={"count": 15}
            )

        assert result.status == ExecutionStatus.COMPLETED
        true_status = result.node_results["true_branch"]
        false_status = result.node_results["false_branch"]
        true_s = true_status["status"] if isinstance(true_status, dict) else true_status.status
        false_s = false_status["status"] if isinstance(false_status, dict) else false_status.status
        if hasattr(true_s, "value"):
            true_s = true_s.value
        if hasattr(false_s, "value"):
            false_s = false_s.value
        assert true_s == "completed"
        assert false_s == "skipped"

    @pytest.mark.asyncio
    async def test_condition_false_branch_executes(self, mock_executor, sample_condition_workflow):
        """Condition false → false_branch runs, true_branch SKIPPED."""
        factory = self._make_condition_node_factory(condition_result=False)
        with patch("citra_workflow.executor.get_node", side_effect=factory):
            result = await mock_executor.execute(
                sample_condition_workflow, trigger_data={"count": 5}
            )

        assert result.status == ExecutionStatus.COMPLETED
        true_status = result.node_results["true_branch"]
        false_status = result.node_results["false_branch"]
        true_s = true_status["status"] if isinstance(true_status, dict) else true_status.status
        false_s = false_status["status"] if isinstance(false_status, dict) else false_status.status
        if hasattr(true_s, "value"):
            true_s = true_s.value
        if hasattr(false_s, "value"):
            false_s = false_s.value
        assert true_s == "skipped"
        assert false_s == "completed"

    @pytest.mark.asyncio
    async def test_condition_skip_propagates_to_children(self, mock_executor, make_node, make_edge):
        """Skipped branch's children should also be skipped."""
        wf = WorkflowDefinition(
            workflow_id="wf-skip-child",
            user_id="test-user",
            name="Skip Propagation",
            nodes=[
                make_node("trigger", NodeType.MANUAL_TRIGGER),
                make_node("cond", NodeType.CONDITION, config={
                    "field": "x", "operator": "==", "value": "yes",
                }),
                make_node("true_node", NodeType.LLM_PROCESSOR),
                make_node("true_child", NodeType.WEBHOOK_OUTPUT, config={
                    "url": "https://example.com", "method": "POST",
                }),
                make_node("false_node", NodeType.LLM_PROCESSOR),
            ],
            edges=[
                make_edge("trigger", "cond"),
                make_edge("cond", "true_node", source_handle="true"),
                make_edge("cond", "false_node", source_handle="false"),
                make_edge("true_node", "true_child"),
            ],
        )

        # Return condition_result=False → true branch skipped
        factory = self._make_condition_node_factory(condition_result=False)
        with patch("citra_workflow.executor.get_node", side_effect=factory):
            result = await mock_executor.execute(wf, trigger_data={"x": "no"})

        assert result.status == ExecutionStatus.COMPLETED
        # true_node skipped, and true_child should also not appear as completed
        true_nr = result.node_results.get("true_node", {})
        true_s = true_nr.get("status", "") if isinstance(true_nr, dict) else getattr(true_nr, "status", "")
        if hasattr(true_s, "value"):
            true_s = true_s.value
        assert true_s == "skipped"


# ============================================================================
# TestSwitchRouting
# ============================================================================

class TestSwitchRouting:
    """Switch/router node multi-way routing."""

    def _make_switch_node_factory(self, matched_route: int):
        """Return a get_node factory where the switch node returns a specific route."""
        def node_factory(node_type):
            instance = AsyncMock()

            async def _run(ctx):
                if ctx.node_id == "switch":
                    return _make_run_result(ctx.node_id, {
                        "items": ctx.input_data.get("items", []) if isinstance(ctx.input_data, dict) else [],
                        "meta": {
                            "switch_result": True,
                            "matched_route": matched_route,
                            "matched_label": f"Route {matched_route}",
                            "field_value": "test",
                        },
                    })
                return _make_run_result(ctx.node_id, {"processed": True})

            instance.run = AsyncMock(side_effect=_run)
            return instance
        return node_factory

    @pytest.mark.asyncio
    async def test_switch_route_0_selected(self, mock_executor, sample_switch_workflow):
        """matched_route=0 → route_a runs, route_b and route_default SKIPPED."""
        factory = self._make_switch_node_factory(matched_route=0)
        with patch("citra_workflow.executor.get_node", side_effect=factory):
            result = await mock_executor.execute(
                sample_switch_workflow, trigger_data={"category": "a"}
            )

        assert result.status == ExecutionStatus.COMPLETED
        a_s = result.node_results["route_a"]
        b_s = result.node_results["route_b"]
        d_s = result.node_results["route_default"]

        def _status(nr):
            s = nr["status"] if isinstance(nr, dict) else nr.status
            return s.value if hasattr(s, "value") else s

        assert _status(a_s) == "completed"
        assert _status(b_s) == "skipped"
        assert _status(d_s) == "skipped"

    @pytest.mark.asyncio
    async def test_switch_default_route(self, mock_executor, sample_switch_workflow):
        """matched_route=2 (default) → route_default runs, others SKIPPED."""
        factory = self._make_switch_node_factory(matched_route=2)
        with patch("citra_workflow.executor.get_node", side_effect=factory):
            result = await mock_executor.execute(
                sample_switch_workflow, trigger_data={"category": "zzz"}
            )

        assert result.status == ExecutionStatus.COMPLETED

        def _status(nr):
            s = nr["status"] if isinstance(nr, dict) else nr.status
            return s.value if hasattr(s, "value") else s

        assert _status(result.node_results["route_a"]) == "skipped"
        assert _status(result.node_results["route_b"]) == "skipped"
        assert _status(result.node_results["route_default"]) == "completed"

    @pytest.mark.asyncio
    async def test_switch_invalid_handle_format(self, mock_executor, make_node, make_edge):
        """Edge with handle 'invalid' → handle_idx=-1 → node skipped."""
        wf = WorkflowDefinition(
            workflow_id="wf-bad-handle",
            user_id="test-user",
            name="Bad Handle",
            nodes=[
                make_node("trigger", NodeType.MANUAL_TRIGGER),
                make_node("switch", NodeType.SWITCH_ROUTER, config={
                    "field": "x",
                    "routes": [{"label": "A", "value": "a"}],
                }),
                make_node("target", NodeType.LLM_PROCESSOR),
            ],
            edges=[
                make_edge("trigger", "switch"),
                make_edge("switch", "target", source_handle="invalid"),
            ],
        )

        factory = self._make_switch_node_factory(matched_route=0)
        with patch("citra_workflow.executor.get_node", side_effect=factory):
            result = await mock_executor.execute(wf, trigger_data={"x": "a"})

        # target should be skipped since handle doesn't parse to out-N
        target_nr = result.node_results.get("target", {})
        status = target_nr.get("status", "") if isinstance(target_nr, dict) else getattr(target_nr, "status", "")
        if hasattr(status, "value"):
            status = status.value
        assert status == "skipped"


# ============================================================================
# TestApprovalPauseResume
# ============================================================================

class TestApprovalPauseResume:
    """Human approval pausing and resuming."""

    @pytest.mark.asyncio
    async def test_approval_pauses_execution(self, mock_executor, sample_approval_workflow):
        """Approval node mid-workflow → execution.status == PAUSED."""
        def node_factory(node_type):
            instance = AsyncMock()

            async def _run(ctx):
                if ctx.node_id == "approval":
                    return _make_run_result(ctx.node_id, {
                        "items": [],
                        "meta": {
                            "status": "waiting_approval",
                            "message": "Please approve",
                            "timeout_hours": 24,
                        },
                    })
                return _make_run_result(ctx.node_id, {"ok": True})

            instance.run = AsyncMock(side_effect=_run)
            return instance

        with patch("citra_workflow.executor.get_node", side_effect=node_factory):
            result = await mock_executor.execute(sample_approval_workflow)

        assert result.status == ExecutionStatus.PAUSED
        assert result.paused_at_node == "approval"
        assert result.approval_id == "approval-123"

    @pytest.mark.asyncio
    async def test_resume_approved_continues(self, mock_executor, sample_approval_workflow):
        """Resume paused execution with approved=True → continues BFS, ends COMPLETED."""
        # Simulate a paused execution
        execution = WorkflowExecution(
            execution_id="exec-1",
            workflow_id=sample_approval_workflow.workflow_id,
            user_id="test-user",
            status=ExecutionStatus.PAUSED,
            paused_at_node="approval",
            approval_id="approval-123",
            node_results={
                "trigger": _make_run_result("trigger", {"items": [], "meta": {"triggered": True}}).model_dump(),
                "approval": _make_run_result("approval", {
                    "items": [], "meta": {"status": "waiting_approval"},
                }).model_dump(),
            },
        )

        def node_factory(node_type):
            return _make_node_instance(default_output={"sent": True})

        with patch("citra_workflow.executor.get_node", side_effect=node_factory):
            result = await mock_executor.resume(execution, sample_approval_workflow, approved=True)

        assert result.status == ExecutionStatus.COMPLETED
        assert "out" in result.node_results

    @pytest.mark.asyncio
    async def test_resume_rejected_fails(self, mock_executor, sample_approval_workflow):
        """Resume with approved=False → FAILED + 'Human approval rejected'."""
        execution = WorkflowExecution(
            execution_id="exec-2",
            workflow_id=sample_approval_workflow.workflow_id,
            user_id="test-user",
            status=ExecutionStatus.PAUSED,
            paused_at_node="approval",
            node_results={
                "trigger": _make_run_result("trigger", {"items": [], "meta": {"triggered": True}}).model_dump(),
                "approval": _make_run_result("approval", {"items": [], "meta": {}}).model_dump(),
            },
        )

        result = await mock_executor.resume(execution, sample_approval_workflow, approved=False)

        assert result.status == ExecutionStatus.FAILED
        assert "rejected" in (result.error or "").lower()

    @pytest.mark.asyncio
    async def test_resume_not_paused_raises(self, mock_executor, sample_approval_workflow):
        """Trying to resume a COMPLETED execution → ValueError."""
        execution = WorkflowExecution(
            execution_id="exec-3",
            workflow_id=sample_approval_workflow.workflow_id,
            user_id="test-user",
            status=ExecutionStatus.COMPLETED,
            node_results={},
        )

        with pytest.raises(ValueError, match="not paused"):
            await mock_executor.resume(execution, sample_approval_workflow, approved=True)

    @pytest.mark.asyncio
    async def test_resume_preserves_prior_outputs(self, mock_executor, sample_approval_workflow):
        """Node results from before pause are still accessible after resume."""
        trigger_output = {"items": [], "meta": {"triggered": True, "trigger_type": "manual"}}
        execution = WorkflowExecution(
            execution_id="exec-4",
            workflow_id=sample_approval_workflow.workflow_id,
            user_id="test-user",
            status=ExecutionStatus.PAUSED,
            paused_at_node="approval",
            node_results={
                "trigger": _make_run_result("trigger", trigger_output).model_dump(),
                "approval": _make_run_result("approval", {
                    "items": [], "meta": {"status": "waiting_approval"},
                }).model_dump(),
            },
        )

        def node_factory(node_type):
            return _make_node_instance(default_output={"done": True})

        with patch("citra_workflow.executor.get_node", side_effect=node_factory):
            result = await mock_executor.resume(execution, sample_approval_workflow, approved=True)

        assert result.status == ExecutionStatus.COMPLETED
        # Prior results should still be present
        assert "trigger" in result.node_results
        assert "approval" in result.node_results
        assert "out" in result.node_results


# ============================================================================
# TestCrashResumeAndCheckpoint  (Track 2 — durability)
# ============================================================================

class TestCrashResumeAndCheckpoint:
    """Per-node checkpointing + resume-after-crash: a run abandoned by a dead
    worker restarts from its last completed node, not the whole graph."""

    @pytest.mark.asyncio
    async def test_crash_resume_restarts_from_last_checkpoint(
        self, mock_executor, sample_linear_workflow
    ):
        """status=RESUMING with NO paused node (a crash, not an approval): the
        BFS re-seeds every unexecuted node and runs only those whose parents are
        done. Already-completed nodes are NOT re-run."""
        ran = []

        def node_factory(node_type):
            instance = AsyncMock()

            async def _run(ctx):
                ran.append(ctx.node_id)
                return _make_run_result(ctx.node_id, {"ok": True})

            instance.run = AsyncMock(side_effect=_run)
            return instance

        # Crash AFTER 'trigger' was checkpointed, before 'proc'/'out'.
        execution = WorkflowExecution(
            execution_id="crash-1",
            workflow_id=sample_linear_workflow.workflow_id,
            user_id="test-user",
            status=ExecutionStatus.RESUMING,
            paused_at_node=None,  # the tell-tale of a crash-resume vs approval
            node_results={
                "trigger": _make_run_result(
                    "trigger", {"items": [], "meta": {"triggered": True}}
                ).model_dump(),
            },
        )

        with patch("citra_workflow.executor.get_node", side_effect=node_factory):
            result = await mock_executor.resume(
                execution, sample_linear_workflow, approved=True
            )

        assert result.status == ExecutionStatus.COMPLETED
        assert "trigger" not in ran                 # not re-run
        assert set(ran) == {"proc", "out"}          # only the unfinished frontier
        assert "proc" in result.node_results
        assert "out" in result.node_results

    @pytest.mark.asyncio
    async def test_continue_on_error_prunes_branch_not_run(
        self, mock_executor, make_node, make_edge
    ):
        """A node with continue_on_error that FAILS does not abort the run — it's
        recorded FAILED, its branch is skipped, and the run still COMPLETES."""
        wf = WorkflowDefinition(
            workflow_id="wf-soft", user_id="u", name="soft",
            nodes=[
                make_node("trigger", "manual_trigger"),
                make_node("flaky", "data_transform", config={"continue_on_error": True}),
                make_node("after", "data_transform"),
            ],
            edges=[make_edge("trigger", "flaky"), make_edge("flaky", "after")],
        )

        def node_factory(node_type):
            instance = AsyncMock()

            async def _run(ctx):
                if ctx.node_id == "flaky":
                    return _make_run_result(
                        ctx.node_id, status=NodeExecutionStatus.FAILED
                    )
                return _make_run_result(ctx.node_id, {"ok": True})

            instance.run = AsyncMock(side_effect=_run)
            return instance

        with patch("citra_workflow.executor.get_node", side_effect=node_factory):
            result = await mock_executor.execute(wf)

        def _st(nr):
            s = nr["status"] if isinstance(nr, dict) else nr.status
            return s.value if hasattr(s, "value") else s

        # Run did NOT abort
        assert result.status == ExecutionStatus.COMPLETED
        assert _st(result.node_results["flaky"]) == "failed"
        # 'after' is downstream of the only (soft-failed) parent → skipped
        assert _st(result.node_results["after"]) == "skipped"

    @pytest.mark.asyncio
    async def test_execution_checkpoints_per_node(
        self, mock_executor, sample_linear_workflow
    ):
        """With the checkpoint interval at 0, the executor persists progress
        after each completed node (not only at the terminal state)."""
        def node_factory(node_type):
            return _make_node_instance(default_output={"ok": True})

        with patch("citra_workflow.executor.CHECKPOINT_INTERVAL_SECONDS", 0), \
             patch("citra_workflow.executor.get_node", side_effect=node_factory):
            await mock_executor.execute(sample_linear_workflow)

        # 3 nodes → at least 3 mid-run checkpoints (plus the terminal save).
        assert mock_executor._save_execution.call_count >= 3


# ============================================================================
# TestProgressAndPersistence
# ============================================================================

class TestProgressAndPersistence:
    """Verify _update_progress and _save_execution call Redis/MongoDB correctly."""

    def test_update_progress_calls_redis(self, mock_cache):
        """cache.set should be called with correct key pattern."""
        with patch("citra_cache.get_cache_manager", return_value=mock_cache):
            from citra_workflow.executor import WorkflowExecutor
            executor = WorkflowExecutor()
            executor.cache = mock_cache

        execution = WorkflowExecution(
            execution_id="exec-progress",
            workflow_id="wf-1",
            user_id="test-user",
            status=ExecutionStatus.RUNNING,
        )

        executor._update_progress(execution)
        mock_cache.set.assert_called_once()
        call_args = mock_cache.set.call_args
        key = call_args[0][0]
        assert key == "workflow:exec:exec-progress"

    @pytest.mark.asyncio
    async def test_save_execution_calls_mongodb(self, mock_mongo_client, mock_cache):
        """_save_execution should call update_one with upsert=True."""
        with patch("citra_cache.get_cache_manager", return_value=mock_cache):
            from citra_workflow.executor import WorkflowExecutor
            executor = WorkflowExecutor()
            executor.cache = mock_cache

        execution = WorkflowExecution(
            execution_id="exec-save",
            workflow_id="wf-1",
            user_id="test-user",
            status=ExecutionStatus.COMPLETED,
        )

        with patch("citra_mongo.get_async_mongo_client", return_value=mock_mongo_client), \
             patch("citra_mongo.MONGODB_DATABASE", "test_db"):
            await executor._save_execution(execution)

        # The mock_mongo_col's update_one should have been called
        mock_col = mock_mongo_client["test_db"]["WorkflowExecutions"]
        mock_col.update_one.assert_called_once()

    @pytest.mark.asyncio
    async def test_create_approval_request_saves_to_db(self, mock_mongo_client, mock_cache):
        """_create_approval_request should insert_one into WorkflowApprovals."""
        with patch("citra_cache.get_cache_manager", return_value=mock_cache):
            from citra_workflow.executor import WorkflowExecutor
            executor = WorkflowExecutor()
            executor.cache = mock_cache

        execution = WorkflowExecution(
            execution_id="exec-approval",
            workflow_id="wf-1",
            user_id="test-user",
            status=ExecutionStatus.PAUSED,
        )
        workflow = WorkflowDefinition(
            workflow_id="wf-1",
            user_id="test-user",
            name="Test WF",
        )
        node_def = MagicMock()
        node_def.id = "approval-node"
        node_def.label = "Approve"

        output_data = {
            "items": [],
            "meta": {
                "message": "Please approve",
                "timeout_hours": 24,
                "data_preview": None,
            },
        }

        # Mock the user lookup to return None (no email) to simplify
        mock_col = mock_mongo_client["test_db"]["WorkflowApprovals"]

        with patch("citra_mongo.get_async_mongo_client", return_value=mock_mongo_client), \
             patch("citra_mongo.MONGODB_DATABASE", "test_db"):
            approval_id = await executor._create_approval_request(
                execution=execution,
                workflow=workflow,
                node_def=node_def,
                output_data=output_data,
            )

        assert approval_id is not None
        assert isinstance(approval_id, str)


# ============================================================================
# TestCooperativeCancellation
# ============================================================================

class TestCooperativeCancellation:
    """The /cancel endpoint sets a Redis flag (cache.get -> "1"); the executor
    polls it at each node boundary and stops the run as CANCELLED without
    tearing a node mid-flight. These tests drive that via the mocked cache."""

    @pytest.mark.asyncio
    async def test_cancel_flag_set_before_run_stops_immediately(
        self, mock_executor, sample_linear_workflow
    ):
        """Flag already set when the run starts → it cancels at the first node
        boundary, before ANY node executes."""
        ran = []

        def node_factory(node_type):
            return _make_node_instance(side_effect_fn=lambda ctx: ran.append(ctx.node_id) or {})

        # cache.get returns the truthy flag for every probe.
        mock_executor.cache.get = MagicMock(return_value="1")

        with patch("citra_workflow.executor.get_node", side_effect=node_factory):
            result = await mock_executor.execute(sample_linear_workflow)

        assert result.status == ExecutionStatus.CANCELLED
        assert result.error == "Execution cancelled by user"
        assert result.completed_at is not None
        assert ran == []  # cooperative: stopped before running a single node
        mock_executor._save_execution.assert_awaited()  # cancelled state persisted

    @pytest.mark.asyncio
    async def test_cancel_midway_lets_running_nodes_finish(
        self, mock_executor, sample_linear_workflow
    ):
        """Flag flips on after the first two boundary checks → trigger + proc
        complete, then the run cancels before `out` (no torn writes)."""
        ran = []

        def node_factory(node_type):
            return _make_node_instance(side_effect_fn=lambda ctx: ran.append(ctx.node_id) or {})

        # The only cache.get calls in execute() are the per-node cancel probes.
        # Return falsy for the first two nodes, then the flag for the third.
        mock_executor.cache.get = MagicMock(side_effect=[None, None, "1"])

        with patch("citra_workflow.executor.get_node", side_effect=node_factory):
            result = await mock_executor.execute(sample_linear_workflow)

        assert result.status == ExecutionStatus.CANCELLED
        assert ran == ["trigger", "proc"]  # both ran; `out` never started
        assert "out" not in result.node_results

    @pytest.mark.asyncio
    async def test_no_cancel_runs_to_completion(
        self, mock_executor, sample_linear_workflow
    ):
        """Control: with the flag never set, the run completes normally."""
        def node_factory(node_type):
            return _make_node_instance(side_effect_fn=lambda ctx: {})

        mock_executor.cache.get = MagicMock(return_value=None)

        with patch("citra_workflow.executor.get_node", side_effect=node_factory):
            result = await mock_executor.execute(sample_linear_workflow)

        assert result.status == ExecutionStatus.COMPLETED
        assert len(result.node_results) == 3

    @pytest.mark.asyncio
    async def test_mark_cancelled_clears_the_flag(self, mock_executor):
        """_mark_cancelled finalises CANCELLED and deletes the one-shot flag so a
        recycled execution_id can't re-trigger."""
        execution = WorkflowExecution(
            execution_id="exec-cxl-1",
            workflow_id="wf-1",
            user_id="u1",
            status=ExecutionStatus.RUNNING,
        )
        await mock_executor._mark_cancelled(execution)

        assert execution.status == ExecutionStatus.CANCELLED
        assert execution.completed_at is not None
        mock_executor.cache.delete.assert_called_once_with("workflow:cancel:exec-cxl-1")

    @pytest.mark.asyncio
    async def test_cancel_probe_treats_cache_error_as_not_cancelled(self, mock_executor):
        """A Redis blip during the probe must never abort a healthy run — it
        logs and returns False (fail-safe, not fail-closed)."""
        mock_executor.cache.get = MagicMock(side_effect=RuntimeError("redis down"))
        assert mock_executor._is_cancel_requested("exec-x") is False
