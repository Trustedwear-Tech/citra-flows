# Copyright (c) 2026 Trustedwear Tech Private Limited (https://citra-ai.com)
# Author: Rohit Kumar Chandan
# SPDX-License-Identifier: BUSL-1.1
#
# Licensed under the Business Source License 1.1. Non-production use is granted;
# production use requires a commercial licence until the Change Date, after
# which this file converts to Apache-2.0. See LICENSE at the repository root.

"""Tests for the maintenance orphaned-blob scan/sweep (_scan_blob_orphans).

This is the correctness-critical path: it decides which workflow media gets
deleted. The rules under test:

  * A blob whose execution is TERMINAL (completed/failed/cancelled/...) is an
    orphan → reclaimable.
  * A blob whose execution is still ALIVE (running/paused/...) is KEPT.
  * A blob whose execution record is GONE is an orphan (super_admin), but is
    'unattributable' for a non-super admin (can't be tied to their org).
  * A non-super admin only ever touches media for their own org's workflows.
  * dry_run (delete=False) deletes nothing; delete=True calls delete_blob.

GridFS + Mongo are faked so the classification logic is exercised directly.
"""
import os
import sys

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from citra_auth import Roles
from citra_workflow import router as wf_router


class _Cursor:
    """Async cursor over a fixed list of docs (what find() returns)."""

    def __init__(self, docs):
        self._docs = docs

    def __aiter__(self):
        async def _gen():
            for d in self._docs:
                yield d
        return _gen()


def _make_db(*, workflows, executions):
    """Fake DB whose Workflows / WorkflowExecutions collections return the
    given docs from find() (the projection/query args are ignored — the docs
    are pre-filtered by the test)."""
    wf_col = MagicMock()
    wf_col.find = MagicMock(side_effect=lambda *a, **k: _Cursor(workflows))

    ex_col = MagicMock()
    ex_col.find = MagicMock(side_effect=lambda *a, **k: _Cursor(executions))

    db = MagicMock()
    db.__getitem__ = MagicMock(side_effect=lambda name: {
        "Workflows": wf_col,
        "WorkflowExecutions": ex_col,
    }[name])
    return db


def _blob(bid, exec_id, size=100, upload_date=None):
    return {"id": bid, "execution_id": exec_id, "mime": "image/png",
            "filename": f"{bid}.png", "size": size, "upload_date": upload_date}


async def _scan(*, blobs, executions, workflows=None, super_admin=False, delete=False):
    claims = {
        "roles": [Roles.SUPER_ADMIN] if super_admin else [Roles.ORG_ADMIN],
        "org_id": "org-1",
        "user_id": "u1",
        "email": "a@b.c",
    }
    deleted_ids = []

    async def _fake_delete(gid):
        deleted_ids.append(gid)
        return True

    db = _make_db(workflows=workflows or [], executions=executions)
    with patch.object(wf_router, "_db", return_value=db), \
         patch("citra_workflow.blob_store.list_blob_files",
               new=AsyncMock(return_value=blobs)), \
         patch("citra_workflow.blob_store.delete_blob", new=_fake_delete):
        result = await wf_router._scan_blob_orphans(claims, delete=delete)
    return result, deleted_ids


@pytest.mark.asyncio
async def test_super_admin_terminal_is_orphan_alive_is_kept():
    blobs = [_blob("b1", "e-done"), _blob("b2", "e-running"), _blob("b3", "e-paused")]
    executions = [
        {"execution_id": "e-done", "status": "completed", "workflow_id": "w1"},
        {"execution_id": "e-running", "status": "running", "workflow_id": "w1"},
        {"execution_id": "e-paused", "status": "paused", "workflow_id": "w1"},
    ]
    result, _ = await _scan(blobs=blobs, executions=executions, super_admin=True)
    assert result["scanned"] == 3
    assert result["orphans"] == 1          # only the completed run
    assert result["live_kept"] == 2        # running + paused
    assert result["dry_run"] is True


@pytest.mark.asyncio
async def test_super_admin_missing_execution_is_orphan():
    blobs = [_blob("b1", "e-gone")]
    result, _ = await _scan(blobs=blobs, executions=[], super_admin=True)
    assert result["orphans"] == 1
    assert result["skipped_unattributable"] == 0


@pytest.mark.asyncio
async def test_dry_run_deletes_nothing():
    blobs = [_blob("b1", "e-done", size=500)]
    executions = [{"execution_id": "e-done", "status": "failed", "workflow_id": "w1"}]
    result, deleted = await _scan(blobs=blobs, executions=executions,
                                  super_admin=True, delete=False)
    assert result["orphans"] == 1
    assert result["orphan_bytes"] == 500
    assert result["deleted"] == 0
    assert deleted == []


@pytest.mark.asyncio
async def test_sweep_deletes_orphans_only():
    blobs = [_blob("b1", "e-done", size=300), _blob("b2", "e-running", size=999)]
    executions = [
        {"execution_id": "e-done", "status": "cancelled", "workflow_id": "w1"},
        {"execution_id": "e-running", "status": "running", "workflow_id": "w1"},
    ]
    result, deleted = await _scan(blobs=blobs, executions=executions,
                                  super_admin=True, delete=True)
    assert result["deleted"] == 1
    assert result["deleted_bytes"] == 300
    assert deleted == ["b1"]               # the live run's media is untouched


@pytest.mark.asyncio
async def test_non_super_scopes_to_own_org_and_skips_unattributable():
    # b1 → terminal run in our org (orphan), b2 → terminal run in ANOTHER org
    # (invisible), b3 → run record gone (unattributable for a non-super admin).
    blobs = [_blob("b1", "e-mine"), _blob("b2", "e-other"), _blob("b3", "e-gone")]
    executions = [
        {"execution_id": "e-mine", "status": "completed", "workflow_id": "w-mine"},
        {"execution_id": "e-other", "status": "completed", "workflow_id": "w-other"},
    ]
    workflows = [{"workflow_id": "w-mine"}]   # only w-mine belongs to org-1
    result, deleted = await _scan(blobs=blobs, executions=executions,
                                  workflows=workflows, super_admin=False, delete=True)
    assert result["orphans"] == 1              # only b1
    assert result["deleted"] == 1
    assert deleted == ["b1"]
    assert result["skipped_unattributable"] == 1   # b3
    # b2 (other org) is neither an orphan nor unattributable — simply invisible.
