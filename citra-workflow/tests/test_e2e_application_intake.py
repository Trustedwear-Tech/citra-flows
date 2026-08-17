# Copyright (c) 2026 Trustedwear Tech Private Limited (https://citra-ai.com)
# Author: Rohit Kumar Chandan
# SPDX-License-Identifier: BUSL-1.1
#
# Licensed under the Business Source License 1.1. Non-production use is granted;
# production use requires a commercial licence until the Change Date, after
# which this file converts to Apache-2.0. See LICENSE at the repository root.

"""
End-to-end tests for Workflow 1: Application Intake & LLM Evaluation.

Simulates a government application processing pipeline triggered by a webhook:
  webhook_trigger → sql_source → sftp_source → llm_processor → condition
    → (qualified)   sql_writer
    → (unqualified)  set_variable

The real BFS executor traverses the DAG; all node I/O is mocked with
deterministic side effects from conftest_e2e.
"""

import sys
import os
import pytest
from unittest.mock import AsyncMock, patch
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

os.environ["DISABLE_AUTH"] = "true"

from citra_workflow.models import ExecutionStatus, NodeExecutionStatus

from tests.conftest_e2e import (
    APPLICANT_RECORDS,
    APPLICANT_BY_ID,
    APPLICANT_FILES,
    LLM_EVALUATION_RESULTS,
    make_run_result,
    make_mock_node,
    webhook_trigger_side_effect,
    sql_source_fetch_applicant,
    sftp_source_side_effect,
    llm_evaluator_side_effect,
    condition_qualified_side_effect,
    sql_writer_side_effect,
    set_variable_rejected_side_effect,
    assert_node_completed,
    assert_node_skipped,
    assert_node_output_contains,
)


# ============================================================================
# Node factory builder
# ============================================================================

def _build_intake_node_factory(applicant_id: str, qualified_override=None):
    """Build a get_node() factory for the intake workflow.

    Each node type gets a realistic side-effect function.
    If qualified_override is set, it forces the condition result
    regardless of the LLM evaluation.
    """
    def _condition_side_effect(ctx):
        if qualified_override is not None:
            return {
                "items": ctx.input_data.get("items", []) if isinstance(ctx.input_data, dict) else [],
                "meta": {
                    "condition_result": qualified_override,
                    "branch": "true" if qualified_override else "false",
                },
            }
        return condition_qualified_side_effect(ctx)

    dispatch = {
        "webhook":         webhook_trigger_side_effect,
        "sql_fetch":       sql_source_fetch_applicant,
        "sftp_fetch":      sftp_source_side_effect,
        "llm_eval":        llm_evaluator_side_effect,
        "check_qualified": _condition_side_effect,
        "store_result":    sql_writer_side_effect,
        "mark_rejected":   set_variable_rejected_side_effect,
    }

    def node_factory(node_type):
        return make_mock_node(
            side_effect_fn=lambda ctx: dispatch.get(ctx.node_id, lambda c: {})(ctx)
        )

    return node_factory


# ============================================================================
# Tests — Qualified Applicant Path
# ============================================================================

class TestQualifiedApplicant:
    """Webhook fires for a qualified applicant → condition true → sql_writer stores result."""

    QUALIFIED_APPLICANT = APPLICANT_RECORDS[0]  # APP-0001, score 60, qualified=True

    @pytest.mark.asyncio
    async def test_full_pipeline_qualified(self, mock_executor, workflow1_intake):
        """Complete E2E: webhook → sql → sftp → llm → condition(true) → sql_writer."""
        app_id = self.QUALIFIED_APPLICANT["id"]
        factory = _build_intake_node_factory(app_id)

        with patch("citra_workflow.executor.get_node", side_effect=factory):
            result = await mock_executor.execute(
                workflow1_intake,
                trigger_data={"applicant_id": app_id},
            )

        assert result.status == ExecutionStatus.COMPLETED

        # All nodes in the qualified path should complete
        for nid in ("webhook", "sql_fetch", "sftp_fetch", "llm_eval", "check_qualified", "store_result"):
            assert_node_completed(result, nid)

        # The rejected branch should be skipped
        assert_node_skipped(result, "mark_rejected")

    @pytest.mark.asyncio
    async def test_webhook_trigger_passes_applicant_id(self, mock_executor, workflow1_intake):
        """Webhook trigger extracts applicant_id from payload into variables."""
        app_id = self.QUALIFIED_APPLICANT["id"]
        factory = _build_intake_node_factory(app_id)

        with patch("citra_workflow.executor.get_node", side_effect=factory):
            result = await mock_executor.execute(
                workflow1_intake,
                trigger_data={"applicant_id": app_id},
            )

        webhook_nr = result.node_results["webhook"]
        output = webhook_nr["output_data"] if isinstance(webhook_nr, dict) else webhook_nr.output_data
        assert output["variables"]["applicant_id"] == app_id

    @pytest.mark.asyncio
    async def test_sql_source_returns_applicant_record(self, mock_executor, workflow1_intake):
        """SQL source returns the correct applicant record."""
        app_id = self.QUALIFIED_APPLICANT["id"]
        factory = _build_intake_node_factory(app_id)

        with patch("citra_workflow.executor.get_node", side_effect=factory):
            result = await mock_executor.execute(
                workflow1_intake,
                trigger_data={"applicant_id": app_id},
            )

        sql_nr = result.node_results["sql_fetch"]
        output = sql_nr["output_data"] if isinstance(sql_nr, dict) else sql_nr.output_data
        assert output["count"] == 1
        assert output["records"][0]["id"] == app_id

    @pytest.mark.asyncio
    async def test_sftp_source_returns_resume_content(self, mock_executor, workflow1_intake):
        """SFTP source returns resume text for the applicant."""
        app_id = self.QUALIFIED_APPLICANT["id"]
        factory = _build_intake_node_factory(app_id)

        with patch("citra_workflow.executor.get_node", side_effect=factory):
            result = await mock_executor.execute(
                workflow1_intake,
                trigger_data={"applicant_id": app_id},
            )

        sftp_nr = result.node_results["sftp_fetch"]
        output = sftp_nr["output_data"] if isinstance(sftp_nr, dict) else sftp_nr.output_data
        assert self.QUALIFIED_APPLICANT["name"] in output["content"]
        assert output["source_type"] == "text"

    @pytest.mark.asyncio
    async def test_llm_returns_evaluation_with_score(self, mock_executor, workflow1_intake):
        """LLM processor returns evaluation with score and qualified flag."""
        app_id = self.QUALIFIED_APPLICANT["id"]
        factory = _build_intake_node_factory(app_id)

        with patch("citra_workflow.executor.get_node", side_effect=factory):
            result = await mock_executor.execute(
                workflow1_intake,
                trigger_data={"applicant_id": app_id},
            )

        llm_nr = result.node_results["llm_eval"]
        output = llm_nr["output_data"] if isinstance(llm_nr, dict) else llm_nr.output_data
        evaluation = output["result"]
        assert evaluation["applicant_id"] == app_id
        assert evaluation["qualified"] is True
        assert 0 <= evaluation["score"] <= 100

    @pytest.mark.asyncio
    async def test_condition_takes_true_branch(self, mock_executor, workflow1_intake):
        """Condition node evaluates to true for qualified applicant."""
        app_id = self.QUALIFIED_APPLICANT["id"]
        factory = _build_intake_node_factory(app_id)

        with patch("citra_workflow.executor.get_node", side_effect=factory):
            result = await mock_executor.execute(
                workflow1_intake,
                trigger_data={"applicant_id": app_id},
            )

        cond_nr = result.node_results["check_qualified"]
        output = cond_nr["output_data"] if isinstance(cond_nr, dict) else cond_nr.output_data
        assert output["meta"]["condition_result"] is True
        assert output["meta"]["branch"] == "true"

    @pytest.mark.asyncio
    async def test_sql_writer_stores_evaluation(self, mock_executor, workflow1_intake):
        """SQL writer is invoked with the evaluation data for a qualified applicant."""
        app_id = self.QUALIFIED_APPLICANT["id"]
        factory = _build_intake_node_factory(app_id)

        with patch("citra_workflow.executor.get_node", side_effect=factory):
            result = await mock_executor.execute(
                workflow1_intake,
                trigger_data={"applicant_id": app_id},
            )

        assert_node_completed(result, "store_result")
        writer_nr = result.node_results["store_result"]
        output = writer_nr["output_data"] if isinstance(writer_nr, dict) else writer_nr.output_data
        assert output["table"] == "evaluated_applicants"
        assert output["written"] == 1


# ============================================================================
# Tests — Unqualified Applicant Path
# ============================================================================

class TestUnqualifiedApplicant:
    """Webhook fires for an unqualified applicant → condition false → set_variable, sql_writer skipped."""

    UNQUALIFIED_APPLICANT = APPLICANT_RECORDS[45]  # APP-0046, not qualified

    @pytest.mark.asyncio
    async def test_full_pipeline_unqualified(self, mock_executor, workflow1_intake):
        """Complete E2E: webhook → ... → condition(false) → set_variable; sql_writer skipped."""
        app_id = self.UNQUALIFIED_APPLICANT["id"]
        factory = _build_intake_node_factory(app_id)

        with patch("citra_workflow.executor.get_node", side_effect=factory):
            result = await mock_executor.execute(
                workflow1_intake,
                trigger_data={"applicant_id": app_id},
            )

        assert result.status == ExecutionStatus.COMPLETED

        # Core pipeline nodes still complete
        for nid in ("webhook", "sql_fetch", "sftp_fetch", "llm_eval", "check_qualified"):
            assert_node_completed(result, nid)

        # False branch fires, true branch skipped
        assert_node_completed(result, "mark_rejected")
        assert_node_skipped(result, "store_result")

    @pytest.mark.asyncio
    async def test_condition_takes_false_branch(self, mock_executor, workflow1_intake):
        """Condition node evaluates to false for unqualified applicant."""
        app_id = self.UNQUALIFIED_APPLICANT["id"]
        factory = _build_intake_node_factory(app_id)

        with patch("citra_workflow.executor.get_node", side_effect=factory):
            result = await mock_executor.execute(
                workflow1_intake,
                trigger_data={"applicant_id": app_id},
            )

        cond_nr = result.node_results["check_qualified"]
        output = cond_nr["output_data"] if isinstance(cond_nr, dict) else cond_nr.output_data
        assert output["meta"]["condition_result"] is False
        assert output["meta"]["branch"] == "false"
