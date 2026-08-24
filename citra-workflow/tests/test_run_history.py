# Copyright (c) 2026 Trustedwear Tech Private Limited (https://citra-ai.com)
# Author: Rohit Kumar Chandan
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may not
# use this file except in compliance with the License. You may obtain a copy of
# the License at http://www.apache.org/licenses/LICENSE-2.0

"""Tests for the per-workflow run-history read API.

Covers the pure helpers (`_is_deployed`, `_compute_duration_ms`) and the
`list_executions` endpoint's response shaping — in particular that it never
leaks `node_results` and that it surfaces the parent workflow's deploy state.
"""

import sys
import os
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from citra_workflow import router as wf_router
from citra_workflow.router import _is_deployed, _compute_duration_ms, list_executions
from citra_workflow.models import WorkflowStatus


# ─── _is_deployed ──────────────────────────────────────────────────────

class TestIsDeployed:
    def test_deployed_status_is_true(self):
        assert _is_deployed({"status": WorkflowStatus.DEPLOYED.value}) is True

    def test_draft_is_false(self):
        assert _is_deployed({"status": WorkflowStatus.DRAFT.value}) is False

    def test_paused_is_false(self):
        assert _is_deployed({"status": WorkflowStatus.PAUSED.value}) is False

    def test_is_active_does_not_imply_deployed(self):
        # is_active means "not archived" — it must never drive deploy state.
        assert _is_deployed({"status": WorkflowStatus.DRAFT.value, "is_active": True}) is False

    def test_lifecycle_stage_does_not_imply_deployed(self):
        assert _is_deployed({"status": WorkflowStatus.DRAFT.value, "lifecycle_stage": "org_managed"}) is False

    def test_none_is_false(self):
        assert _is_deployed(None) is False


# ─── _compute_duration_ms ──────────────────────────────────────────────

class TestComputeDurationMs:
    def test_both_datetimes(self):
        start = datetime(2026, 1, 1, 12, 0, 0)
        end = start + timedelta(seconds=2, milliseconds=500)
        assert _compute_duration_ms(start, end) == 2500

    def test_iso_strings(self):
        start = "2026-01-01T12:00:00"
        end = "2026-01-01T12:00:01"
        assert _compute_duration_ms(start, end) == 1000

    def test_missing_completed_returns_none(self):
        assert _compute_duration_ms(datetime(2026, 1, 1), None) is None

    def test_missing_started_returns_none(self):
        assert _compute_duration_ms(None, datetime(2026, 1, 1)) is None

    def test_negative_returns_none(self):
        start = datetime(2026, 1, 1, 12, 0, 1)
        end = datetime(2026, 1, 1, 12, 0, 0)
        assert _compute_duration_ms(start, end) is None

    def test_garbage_returns_none(self):
        assert _compute_duration_ms("not-a-date", "also-bad") is None


# ─── list_executions response shaping ──────────────────────────────────

class _FakeCursor:
    """Minimal async cursor: chainable sort/skip/limit + async iteration."""

    def __init__(self, docs):
        self._docs = docs

    def sort(self, *a, **k):
        return self

    def skip(self, *a, **k):
        return self

    def limit(self, *a, **k):
        return self

    def __aiter__(self):
        async def _gen():
            for d in self._docs:
                yield d
        return _gen()


def _make_db(workflow_doc, exec_docs):
    exec_col = MagicMock()
    exec_col.find = MagicMock(return_value=_FakeCursor(exec_docs))

    wf_col = MagicMock()
    wf_col.find_one = AsyncMock(return_value=workflow_doc)

    db = MagicMock()
    db.__getitem__ = MagicMock(
        side_effect=lambda name: wf_col if name == "Workflows" else exec_col
    )
    return db, exec_col


@pytest.mark.asyncio
async def test_list_executions_omits_node_results_and_adds_summary_fields():
    workflow_doc = {
        "workflow_id": "wf-1",
        "name": "My Flow",
        "status": WorkflowStatus.DEPLOYED.value,
    }
    start = datetime(2026, 1, 1, 12, 0, 0)
    end = start + timedelta(seconds=3)
    exec_docs = [
        {
            "execution_id": "exec-1",
            "workflow_id": "wf-1",
            "status": "completed",
            "environment": "prod",
            "trigger_type": "manual",
            "started_at": start,
            "completed_at": end,
            "current_node": None,
            "paused_at_node": None,
            "error": None,
        },
    ]
    db, _ = _make_db(workflow_doc, exec_docs)

    with patch.object(wf_router, "_db", return_value=db), \
         patch.object(wf_router, "get_secure_user_id", return_value="user-1"), \
         patch.object(wf_router, "_check_workflow_action", return_value=None):
        resp = await list_executions(MagicMock(), "wf-1", skip=0, limit=20, status=None)

    assert resp["workflow"]["is_deployed"] is True
    assert resp["workflow"]["name"] == "My Flow"
    assert len(resp["executions"]) == 1
    row = resp["executions"][0]
    # node_results must never appear in the list response.
    assert "node_results" not in row
    assert row["duration_ms"] == 3000
    assert row["trigger_type"] == "manual"
    assert row["environment"] == "prod"


@pytest.mark.asyncio
async def test_list_executions_trigger_fallback_and_missing_timestamps():
    workflow_doc = {"workflow_id": "wf-1", "name": "Flow", "status": WorkflowStatus.DRAFT.value}
    exec_docs = [
        # Old doc: no trigger_type, only legacy `trigger`; no completed_at.
        {
            "execution_id": "exec-old",
            "workflow_id": "wf-1",
            "status": "running",
            "trigger": "scheduled",
            "started_at": datetime(2026, 1, 1),
        },
        # Even older doc: nothing — trigger_type should default to "manual".
        {"execution_id": "exec-bare", "workflow_id": "wf-1", "status": "failed"},
    ]
    db, _ = _make_db(workflow_doc, exec_docs)

    with patch.object(wf_router, "_db", return_value=db), \
         patch.object(wf_router, "get_secure_user_id", return_value="user-1"), \
         patch.object(wf_router, "_check_workflow_action", return_value=None):
        resp = await list_executions(MagicMock(), "wf-1", skip=0, limit=20, status=None)

    assert resp["workflow"]["is_deployed"] is False
    rows = {r["execution_id"]: r for r in resp["executions"]}
    assert rows["exec-old"]["trigger_type"] == "scheduled"
    assert rows["exec-old"]["duration_ms"] is None
    assert rows["exec-bare"]["trigger_type"] == "manual"
    assert rows["exec-bare"]["duration_ms"] is None


@pytest.mark.asyncio
async def test_list_executions_status_filter_passed_to_query():
    workflow_doc = {"workflow_id": "wf-1", "name": "Flow", "status": WorkflowStatus.DRAFT.value}
    db, exec_col = _make_db(workflow_doc, [])

    with patch.object(wf_router, "_db", return_value=db), \
         patch.object(wf_router, "get_secure_user_id", return_value="user-1"), \
         patch.object(wf_router, "_check_workflow_action", return_value=None):
        await list_executions(MagicMock(), "wf-1", skip=0, limit=20, status="failed")

    # The Mongo query must include the status filter.
    query_arg = exec_col.find.call_args[0][0]
    assert query_arg == {"workflow_id": "wf-1", "status": "failed"}


@pytest.mark.asyncio
async def test_list_executions_404_when_workflow_missing():
    from fastapi import HTTPException
    db, _ = _make_db(None, [])

    with patch.object(wf_router, "_db", return_value=db), \
         patch.object(wf_router, "get_secure_user_id", return_value="user-1"):
        with pytest.raises(HTTPException) as exc:
            await list_executions(MagicMock(), "missing", skip=0, limit=20, status=None)
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_ensure_execution_indexes_idempotent():
    coll = MagicMock()
    coll.create_index = AsyncMock(return_value="idx_workflow_started")
    db = MagicMock()
    db.__getitem__ = MagicMock(return_value=coll)

    # Reset module-level guard so the test exercises creation.
    wf_router._EXECUTION_INDEXES_READY = False
    with patch.object(wf_router, "_db", return_value=db):
        await wf_router.ensure_execution_indexes()
        await wf_router.ensure_execution_indexes()  # second call short-circuits

    # Two indexes on two collections, created on the FIRST call only — the
    # second call must add nothing. This previously asserted `called_once`,
    # which conflated "ran once" with "creates one index" and started failing
    # the moment the WorkflowVersions index was added.
    assert coll.create_index.await_count == 2, coll.create_index.await_args_list
    created = {
        kwargs["name"]: args[0]
        for args, kwargs in coll.create_index.call_args_list
    }
    assert created == {
        "idx_workflow_started": [("workflow_id", 1), ("started_at", -1)],
        "idx_wfver_workflow_version": [("workflow_id", 1), ("version_number", -1)],
    }, created
