# Copyright (c) 2026 Trustedwear Tech Private Limited (https://citra-ai.com)
# Author: Rohit Kumar Chandan
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may not
# use this file except in compliance with the License. You may obtain a copy of
# the License at http://www.apache.org/licenses/LICENSE-2.0

"""
Shared fixtures and test data for end-to-end government application processing tests.

Scenario:
  - 50 applicants apply to a government program via a web app.
  - Each application upload fires a webhook triggering Workflow 1 (intake + LLM evaluation).
  - An admin later runs Workflow 2 (sort qualified applicants, generate PDF, email results).

All external I/O (SQL, SFTP, LLM, PDF render, email) is mocked.
The real BFS executor engine is tested end-to-end.
"""

import sys
import os
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

os.environ["DISABLE_AUTH"] = "true"

from citra_workflow.models import (
    NodeType, WorkflowDefinition, NodeDefinition, EdgeDefinition,
    NodeExecutionResult, NodeExecutionStatus, ExecutionStatus,
)
from citra_workflow.nodes import NodeContext


# ============================================================================
# Test Data — 50 Applicants
# ============================================================================

def _generate_applicants(n=50):
    """Generate n deterministic applicant records."""
    educations = ["Bachelor's", "Master's", "PhD", "Associate", "High School"]
    departments = ["Engineering", "Policy", "Legal", "Finance", "Operations"]
    records = []
    for i in range(1, n + 1):
        records.append({
            "id": f"APP-{i:04d}",
            "name": f"Applicant {i}",
            "email": f"applicant{i}@example.gov",
            "education": educations[i % len(educations)],
            "department": departments[i % len(departments)],
            "experience_years": (i * 3) % 20 + 1,
            "gpa": round(2.5 + (i % 20) * 0.1, 2),
        })
    return records


APPLICANT_RECORDS = _generate_applicants(50)
APPLICANT_BY_ID = {r["id"]: r for r in APPLICANT_RECORDS}


def _generate_applicant_files():
    """Generate fake SFTP file content for each applicant (resume text)."""
    files = {}
    for r in APPLICANT_RECORDS:
        files[r["id"]] = (
            f"RESUME — {r['name']}\n"
            f"Education: {r['education']}\n"
            f"Experience: {r['experience_years']} years\n"
            f"Department: {r['department']}\n"
            f"GPA: {r['gpa']}\n"
        )
    return files


APPLICANT_FILES = _generate_applicant_files()


def _generate_llm_evaluations():
    """Pre-computed LLM evaluation for each applicant.

    First 40 are qualified (scores 60–98), last 10 are not qualified (scores 30–55).
    """
    evaluations = {}
    for i, r in enumerate(APPLICANT_RECORDS):
        if i < 40:
            score = 60 + int((i / 39) * 38)  # 60..98
            qualified = True
            reasoning = f"{r['name']} meets criteria: {r['education']}, {r['experience_years']}yr exp."
        else:
            score = 30 + (i - 40) * 3  # 30..57
            qualified = False
            reasoning = f"{r['name']} does not meet minimum requirements."
        evaluations[r["id"]] = {
            "applicant_id": r["id"],
            "name": r["name"],
            "score": score,
            "qualified": qualified,
            "reasoning": reasoning,
        }
    return evaluations


LLM_EVALUATION_RESULTS = _generate_llm_evaluations()

# Pre-compute expected sorted top 10 (qualified only, descending by score)
QUALIFIED_EVALUATED = [
    e for e in LLM_EVALUATION_RESULTS.values() if e["qualified"]
]
QUALIFIED_SORTED = sorted(QUALIFIED_EVALUATED, key=lambda x: x["score"], reverse=True)
EXPECTED_TOP_10 = QUALIFIED_SORTED[:10]


# ============================================================================
# Helper: make NodeExecutionResult
# ============================================================================

def make_run_result(node_id, output_data=None, status=NodeExecutionStatus.COMPLETED):
    return NodeExecutionResult(
        node_id=node_id,
        status=status,
        output_data=output_data or {},
        started_at=datetime.utcnow(),
        completed_at=datetime.utcnow(),
        duration_ms=5,
    )


# ============================================================================
# Mock Node Instance Factory
# ============================================================================

def make_mock_node(side_effect_fn=None, default_output=None):
    """Create a mock node whose run(ctx) returns a NodeExecutionResult."""
    instance = AsyncMock()

    async def _run(ctx):
        if side_effect_fn:
            output = side_effect_fn(ctx)
        else:
            output = default_output or {"processed": True}
        return make_run_result(ctx.node_id, output)

    instance.run = AsyncMock(side_effect=_run)
    return instance


# ============================================================================
# Node Side-Effect Functions (simulate real node behavior)
# ============================================================================

def webhook_trigger_side_effect(ctx):
    """Simulate WebhookTriggerNode: extract applicant_id from payload."""
    payload = ctx.input_data or {}
    applicant_id = payload.get("applicant_id", "")
    return {
        "triggered": True,
        "trigger_type": "webhook",
        "payload": payload,
        "variables": {"applicant_id": applicant_id},
        "inputs": {"applicant_id": applicant_id},
    }


def manual_trigger_side_effect(ctx):
    """Simulate ManualTriggerNode: pass through variables."""
    return {
        "triggered": True,
        "trigger_type": "manual",
        "variables": dict(ctx.variables),
    }


def sql_source_fetch_applicant(ctx):
    """Simulate SQLSourceNode: fetch single applicant by ID from 'applications' table."""
    applicant_id = ctx.variables.get("applicant_id", "")
    record = APPLICANT_BY_ID.get(applicant_id)
    if record:
        return {"items": [record], "records": [record], "count": 1}
    return {"items": [], "records": [], "count": 0}


def sql_source_fetch_evaluated(ctx):
    """Simulate SQLSourceNode: fetch all qualified evaluated applicants."""
    qualified = [e for e in LLM_EVALUATION_RESULTS.values() if e["qualified"]]
    return {"items": qualified, "records": qualified, "count": len(qualified)}


def sql_source_fetch_evaluated_empty(ctx):
    """Simulate SQLSourceNode: return empty result set (edge case)."""
    return {"items": [], "records": [], "count": 0}


def sftp_source_side_effect(ctx):
    """Simulate SFTPSourceNode: fetch applicant file by ID."""
    applicant_id = ctx.variables.get("applicant_id", "")
    content = APPLICANT_FILES.get(applicant_id, "")
    return {
        "items": [{"content": content}],
        "records": [{"content": content}],
        "count": 1,
        "source_type": "text",
        "content": content,
        "line_count": len(content.splitlines()),
    }


def llm_evaluator_side_effect(ctx):
    """Simulate LLMProcessorNode: evaluate applicant, return score + qualified flag."""
    input_data = ctx.input_data
    # The LLM processor receives upstream data — find applicant_id
    applicant_id = ctx.variables.get("applicant_id", "")
    evaluation = LLM_EVALUATION_RESULTS.get(applicant_id, {
        "applicant_id": applicant_id,
        "score": 0,
        "qualified": False,
        "reasoning": "Unknown applicant",
    })
    return {"result": evaluation, "items": [evaluation]}


def condition_qualified_side_effect(ctx):
    """Simulate ConditionNode: check if applicant is qualified."""
    input_data = ctx.input_data or {}
    # Input comes from llm_processor → result dict
    result = input_data.get("result", {})
    if isinstance(result, dict):
        qualified = result.get("qualified", False)
    else:
        qualified = False
    return {
        "items": input_data.get("items", []),
        "meta": {
            "condition_result": qualified,
            "branch": "true" if qualified else "false",
        },
    }


def sql_writer_side_effect(ctx):
    """Simulate SQLWriterNode: store evaluated applicant record."""
    records = ctx.input_data.get("data", {}).get("result", {})
    return {"written": 1, "table": "evaluated_applicants"}


def set_variable_rejected_side_effect(ctx):
    """Simulate SetVariableNode: increment rejected counter."""
    return {
        "variables": {"last_rejected": ctx.variables.get("applicant_id", "")},
    }


def data_transform_sort_side_effect(ctx):
    """Simulate DataTransformNode: sort records by score descending, take top 10."""
    records = ctx.input_data.get("records", [])
    sorted_records = sorted(records, key=lambda x: x.get("score", 0), reverse=True)
    top_10 = sorted_records[:10]
    return {"records": top_10, "count": len(top_10)}


def data_transform_sort_empty_side_effect(ctx):
    """Simulate DataTransformNode: handle empty input."""
    return {"records": [], "count": 0}


def pdf_export_side_effect(ctx):
    """Simulate PDFExportNode: generate PDF from data."""
    return {
        "size_bytes": 15420,
        "title": "Top 10 Government Application Results",
        "s3_key": "exports/results-2026-04-02.pdf",
    }


def email_sender_side_effect(ctx):
    """Simulate EmailSenderNode: send email."""
    recipient = ctx.variables.get("recipient_email", "admin@example.gov")
    return {"sent": True, "to": recipient, "subject": "Application Results Published"}


# ============================================================================
# Assertion Helpers
# ============================================================================

def assert_node_completed(execution, node_id):
    """Assert a node completed successfully."""
    assert node_id in execution.node_results, f"Node '{node_id}' not in results"
    nr = execution.node_results[node_id]
    status = nr["status"] if isinstance(nr, dict) else nr.status
    if hasattr(status, "value"):
        status = status.value
    assert status == "completed", f"Node '{node_id}' status={status}, expected 'completed'"


def assert_node_skipped(execution, node_id):
    """Assert a node was skipped (wrong branch)."""
    assert node_id in execution.node_results, f"Node '{node_id}' not in results"
    nr = execution.node_results[node_id]
    status = nr["status"] if isinstance(nr, dict) else nr.status
    if hasattr(status, "value"):
        status = status.value
    assert status == "skipped", f"Node '{node_id}' status={status}, expected 'skipped'"


def assert_node_output_contains(execution, node_id, key, expected):
    """Assert a node's output_data contains a key with expected value."""
    assert node_id in execution.node_results, f"Node '{node_id}' not in results"
    nr = execution.node_results[node_id]
    output = nr["output_data"] if isinstance(nr, dict) else nr.output_data
    assert key in output, f"Key '{key}' not in node '{node_id}' output: {list(output.keys())}"
    assert output[key] == expected, f"Node '{node_id}' output['{key}']={output[key]}, expected {expected}"


# ============================================================================
# Workflow DAG Fixtures
# ============================================================================

@pytest.fixture
def workflow1_intake(make_node, make_edge):
    """Workflow 1: Webhook-triggered application intake + LLM evaluation.

    DAG:
      webhook_trigger → sql_source → sftp_source → llm_processor → condition
        condition --true--> sql_writer
        condition --false-> set_variable
    """
    return WorkflowDefinition(
        workflow_id="wf-e2e-intake",
        user_id="test-user",
        name="Application Intake & Evaluation",
        variables={"applicant_id": ""},
        nodes=[
            make_node("webhook", NodeType.WEBHOOK_TRIGGER, label="Webhook Trigger", config={
                "input_schema": [{"name": "applicant_id", "type": "string"}],
            }),
            make_node("sql_fetch", NodeType.SQL_SOURCE, label="Fetch Applicant", config={
                "connection_id": "test-sql-conn",
                "query": "SELECT * FROM applications WHERE id = '{{applicant_id}}'",
                "max_rows": 1,
            }),
            make_node("sftp_fetch", NodeType.SFTP_SOURCE, label="Fetch Resume", config={
                "connection_id": "test-sftp-conn",
                "remote_path": "/applications/{{applicant_id}}/resume.pdf",
                "file_type": "text",
            }),
            make_node("llm_eval", NodeType.LLM_PROCESSOR, label="LLM Evaluate", config={
                "system_prompt": (
                    "You are a government application evaluator. "
                    "Score the applicant 0-100 and determine if they are qualified. "
                    "Return JSON: {applicant_id, name, score, qualified, reasoning}"
                ),
                "user_prompt": "Applicant data: {{data}}",
                "model": "gpt-4o-mini",
                "processing_mode": "all",
            }),
            make_node("check_qualified", NodeType.CONDITION, label="Qualified?", config={
                "field": "qualified",
                "operator": "==",
                "value": "true",
            }),
            make_node("store_result", NodeType.SQL_WRITER, label="Store Evaluation", config={
                "connection_id": "test-sql-conn",
                "table": "evaluated_applicants",
                "mode": "append",
            }),
            make_node("mark_rejected", NodeType.SET_VARIABLE, label="Mark Rejected", config={
                "assignments": [{"name": "last_rejected", "value": "{{applicant_id}}"}],
            }),
        ],
        edges=[
            make_edge("webhook", "sql_fetch"),
            make_edge("sql_fetch", "sftp_fetch"),
            make_edge("sftp_fetch", "llm_eval"),
            make_edge("llm_eval", "check_qualified"),
            make_edge("check_qualified", "store_result", source_handle="true"),
            make_edge("check_qualified", "mark_rejected", source_handle="false"),
        ],
    )


@pytest.fixture
def workflow2_sort_publish(make_node, make_edge):
    """Workflow 2: Manual-triggered sort & publish results.

    DAG:
      manual_trigger → sql_read_scores → sort_transform → pdf_report → send_email
    """
    return WorkflowDefinition(
        workflow_id="wf-e2e-sort-publish",
        user_id="test-user",
        name="Sort & Publish Results",
        variables={"recipient_email": "admin@example.gov"},
        nodes=[
            make_node("trigger", NodeType.MANUAL_TRIGGER, label="Manual Trigger", config={
                "input_schema": [{"name": "recipient_email", "type": "string"}],
            }),
            make_node("sql_scores", NodeType.SQL_SOURCE, label="Read All Scores", config={
                "connection_id": "test-sql-conn",
                "query": "SELECT * FROM evaluated_applicants WHERE qualified = true ORDER BY score DESC",
            }),
            make_node("sort_top10", NodeType.DATA_TRANSFORM, label="Sort & Top 10", config={
                "operation": "sort",
                "params": {"column": "score", "ascending": False},
            }),
            make_node("pdf_report", NodeType.PDF_EXPORT, label="Generate PDF Report", config={
                "title": "Top 10 Government Application Results",
                "template": "<h1>{{title}}</h1><table>{{data}}</table>",
                "save_to_s3": True,
            }),
            make_node("send_email", NodeType.EMAIL_SENDER, label="Email Results", config={
                "to": "{{recipient_email}}",
                "subject": "Application Results Published",
                "body_template": "Attached are the top 10 results.\n\n{{data}}",
            }),
        ],
        edges=[
            make_edge("trigger", "sql_scores"),
            make_edge("sql_scores", "sort_top10"),
            make_edge("sort_top10", "pdf_report"),
            make_edge("pdf_report", "send_email"),
        ],
    )
