"""
End-to-end tests for Workflow 2: Sort & Publish Results.

Simulates a manual-triggered workflow where an admin sorts evaluated applicants,
generates a PDF report of the top 10, and emails it:
  manual_trigger → sql_source → data_transform(sort) → pdf_export → email_sender

The real BFS executor traverses the DAG; all node I/O is mocked with
deterministic side effects from conftest_e2e.
"""

import sys
import os
import pytest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

os.environ["DISABLE_AUTH"] = "true"

from citra_workflow.models import ExecutionStatus

from tests.conftest_e2e import (
    QUALIFIED_SORTED,
    EXPECTED_TOP_10,
    LLM_EVALUATION_RESULTS,
    make_mock_node,
    manual_trigger_side_effect,
    sql_source_fetch_evaluated,
    sql_source_fetch_evaluated_empty,
    data_transform_sort_side_effect,
    data_transform_sort_empty_side_effect,
    pdf_export_side_effect,
    email_sender_side_effect,
    assert_node_completed,
    assert_node_output_contains,
)


# ============================================================================
# Node factory builders
# ============================================================================

def _build_sort_publish_factory(sql_side_effect=None, transform_side_effect=None):
    """Build a get_node() factory for the sort-publish workflow."""
    dispatch = {
        "trigger":    manual_trigger_side_effect,
        "sql_scores": sql_side_effect or sql_source_fetch_evaluated,
        "sort_top10": transform_side_effect or data_transform_sort_side_effect,
        "pdf_report": pdf_export_side_effect,
        "send_email": email_sender_side_effect,
    }

    def node_factory(node_type):
        return make_mock_node(
            side_effect_fn=lambda ctx: dispatch.get(ctx.node_id, lambda c: {})(ctx)
        )

    return node_factory


# ============================================================================
# Tests — Normal Flow (40 qualified applicants → top 10)
# ============================================================================

class TestSortPublishNormal:
    """Happy path: 40 qualified applicants are sorted, top 10 exported and emailed."""

    @pytest.mark.asyncio
    async def test_full_pipeline_sort_publish(self, mock_executor, workflow2_sort_publish):
        """Complete E2E: trigger → sql → sort → pdf → email. All 5 nodes complete."""
        factory = _build_sort_publish_factory()

        with patch("citra_workflow.executor.get_node", side_effect=factory):
            result = await mock_executor.execute(
                workflow2_sort_publish,
                trigger_data={"recipient_email": "director@example.gov"},
            )

        assert result.status == ExecutionStatus.COMPLETED
        for nid in ("trigger", "sql_scores", "sort_top10", "pdf_report", "send_email"):
            assert_node_completed(result, nid)

    @pytest.mark.asyncio
    async def test_manual_trigger_sets_variables(self, mock_executor, workflow2_sort_publish):
        """Manual trigger populates recipient_email variable."""
        factory = _build_sort_publish_factory()

        with patch("citra_workflow.executor.get_node", side_effect=factory):
            result = await mock_executor.execute(
                workflow2_sort_publish,
                trigger_data={"recipient_email": "director@example.gov"},
            )

        trigger_nr = result.node_results["trigger"]
        output = trigger_nr["output_data"] if isinstance(trigger_nr, dict) else trigger_nr.output_data
        assert "recipient_email" in output.get("variables", {})

    @pytest.mark.asyncio
    async def test_sql_source_returns_all_qualified(self, mock_executor, workflow2_sort_publish):
        """SQL source returns all 40 qualified evaluated applicants."""
        factory = _build_sort_publish_factory()

        with patch("citra_workflow.executor.get_node", side_effect=factory):
            result = await mock_executor.execute(
                workflow2_sort_publish,
                trigger_data={"recipient_email": "admin@example.gov"},
            )

        sql_nr = result.node_results["sql_scores"]
        output = sql_nr["output_data"] if isinstance(sql_nr, dict) else sql_nr.output_data
        assert output["count"] == 40
        # All returned records should be qualified
        for rec in output["records"]:
            assert rec["qualified"] is True

    @pytest.mark.asyncio
    async def test_data_transform_sorts_by_score_descending(self, mock_executor, workflow2_sort_publish):
        """Data transform sorts records by score descending and returns top 10."""
        factory = _build_sort_publish_factory()

        with patch("citra_workflow.executor.get_node", side_effect=factory):
            result = await mock_executor.execute(
                workflow2_sort_publish,
                trigger_data={"recipient_email": "admin@example.gov"},
            )

        sort_nr = result.node_results["sort_top10"]
        output = sort_nr["output_data"] if isinstance(sort_nr, dict) else sort_nr.output_data
        records = output["records"]

        assert len(records) == 10
        # Verify descending order
        scores = [r["score"] for r in records]
        assert scores == sorted(scores, reverse=True), "Records should be sorted by score descending"

        # Verify these are the actual top 10
        expected_ids = {e["applicant_id"] for e in EXPECTED_TOP_10}
        actual_ids = {r["applicant_id"] for r in records}
        assert actual_ids == expected_ids

    @pytest.mark.asyncio
    async def test_pdf_export_generates_report(self, mock_executor, workflow2_sort_publish):
        """PDF export node produces a report with expected metadata."""
        factory = _build_sort_publish_factory()

        with patch("citra_workflow.executor.get_node", side_effect=factory):
            result = await mock_executor.execute(
                workflow2_sort_publish,
                trigger_data={"recipient_email": "admin@example.gov"},
            )

        assert_node_completed(result, "pdf_report")
        pdf_nr = result.node_results["pdf_report"]
        output = pdf_nr["output_data"] if isinstance(pdf_nr, dict) else pdf_nr.output_data
        assert output["title"] == "Top 10 Government Application Results"
        assert output["size_bytes"] > 0
        assert "s3_key" in output

    @pytest.mark.asyncio
    async def test_email_sender_delivers_to_recipient(self, mock_executor, workflow2_sort_publish):
        """Email sender uses the recipient_email variable and reports success."""
        factory = _build_sort_publish_factory()

        with patch("citra_workflow.executor.get_node", side_effect=factory):
            result = await mock_executor.execute(
                workflow2_sort_publish,
                trigger_data={"recipient_email": "director@example.gov"},
            )

        assert_node_completed(result, "send_email")
        email_nr = result.node_results["send_email"]
        output = email_nr["output_data"] if isinstance(email_nr, dict) else email_nr.output_data
        assert output["sent"] is True
        assert output["subject"] == "Application Results Published"


# ============================================================================
# Tests — Edge Cases
# ============================================================================

class TestSortPublishEdgeCases:
    """Edge cases: empty applicant pool, etc."""

    @pytest.mark.asyncio
    async def test_empty_applicant_pool(self, mock_executor, workflow2_sort_publish):
        """Zero qualified applicants → pipeline still completes, PDF/email fire with empty data."""
        factory = _build_sort_publish_factory(
            sql_side_effect=sql_source_fetch_evaluated_empty,
            transform_side_effect=data_transform_sort_empty_side_effect,
        )

        with patch("citra_workflow.executor.get_node", side_effect=factory):
            result = await mock_executor.execute(
                workflow2_sort_publish,
                trigger_data={"recipient_email": "admin@example.gov"},
            )

        assert result.status == ExecutionStatus.COMPLETED

        # All nodes still complete — even with empty data
        for nid in ("trigger", "sql_scores", "sort_top10", "pdf_report", "send_email"):
            assert_node_completed(result, nid)

        # Verify empty counts propagate
        sql_nr = result.node_results["sql_scores"]
        sql_output = sql_nr["output_data"] if isinstance(sql_nr, dict) else sql_nr.output_data
        assert sql_output["count"] == 0

        sort_nr = result.node_results["sort_top10"]
        sort_output = sort_nr["output_data"] if isinstance(sort_nr, dict) else sort_nr.output_data
        assert sort_output["count"] == 0
