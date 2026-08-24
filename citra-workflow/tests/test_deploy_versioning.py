# Copyright (c) 2026 Trustedwear Tech Private Limited (https://citra-ai.com)
# Author: Rohit Kumar Chandan
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may not
# use this file except in compliance with the License. You may obtain a copy of
# the License at http://www.apache.org/licenses/LICENSE-2.0

"""Tests for safe-deploy versioning + rollback.

Exercises the deploy → snapshot → rollback lineage end to end against a small
in-memory fake of the two collections the feature touches (``Workflows`` and
``WorkflowVersions``). All auth + env-validation + scheduler I/O is patched so
the test focuses on the lineage bookkeeping:

  - every deploy appends an immutable snapshot and advances deployed_version
  - the webhook token is PRESERVED across redeploys (the safe-deploy fix)
  - rolling back a LIVE workflow restores the chosen graph AND appends a new
    snapshot (append-only history), leaving deployed_version on the new entry
  - rolling back a DRAFT workflow restores the graph but does NOT touch the
    deploy lineage
"""

import os
import sys

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from citra_workflow import router as wf_router
from citra_workflow.models import WorkflowDefinition, NodeDefinition, EdgeDefinition, NodeType


# ─── In-memory Mongo fake ──────────────────────────────────────────────

class _FakeCursor:
    def __init__(self, docs):
        self._docs = [dict(d) for d in docs]

    def sort(self, key, direction):
        self._docs.sort(key=lambda d: d.get(key) or 0, reverse=(direction < 0))
        return self

    def __aiter__(self):
        async def _gen():
            for d in self._docs:
                yield dict(d)
        return _gen()


class _FakeCol:
    def __init__(self):
        self.docs = []

    @staticmethod
    def _match(d, query):
        return all(d.get(k) == v for k, v in query.items())

    async def find_one(self, query, sort=None, projection=None):
        rows = [d for d in self.docs if self._match(d, query)]
        if sort:
            for key, direction in reversed(sort):
                rows.sort(key=lambda d: d.get(key) or 0, reverse=(direction < 0))
        return dict(rows[0]) if rows else None

    def find(self, query, projection=None):
        return _FakeCursor([d for d in self.docs if self._match(d, query)])

    async def insert_one(self, doc):
        self.docs.append(dict(doc))
        return MagicMock(inserted_id="x")

    async def update_one(self, query, update):
        for d in self.docs:
            if self._match(d, query):
                if "$set" in update:
                    d.update(update["$set"])
                if "$inc" in update:
                    for k, v in update["$inc"].items():
                        d[k] = (d.get(k) or 0) + v
                return MagicMock(modified_count=1)
        return MagicMock(modified_count=0)

    async def delete_one(self, query):
        for i, d in enumerate(self.docs):
            if self._match(d, query):
                del self.docs[i]
                return MagicMock(deleted_count=1)
        return MagicMock(deleted_count=0)

    async def delete_many(self, query):
        # Support the {field: {"$in": [...]}} shape used by pruning.
        def _match_in(d):
            for k, v in query.items():
                if isinstance(v, dict) and "$in" in v:
                    if d.get(k) not in v["$in"]:
                        return False
                elif d.get(k) != v:
                    return False
            return True
        before = len(self.docs)
        self.docs = [d for d in self.docs if not _match_in(d)]
        return MagicMock(deleted_count=before - len(self.docs))

    async def create_index(self, *a, **k):
        return None


class _FakeDB:
    def __init__(self):
        self.cols = {}

    def __getitem__(self, name):
        return self.cols.setdefault(name, _FakeCol())


# ─── Fixtures / helpers ────────────────────────────────────────────────

def _workflow_doc(nodes, edges, **extra):
    wf = WorkflowDefinition(
        workflow_id="wf-1",
        name="Test",
        org_id="org-1",
        user_id="u1",
        nodes=nodes,
        edges=edges,
    )
    doc = wf.model_dump()
    doc.update(extra)
    return doc


def _linear_nodes():
    return [
        NodeDefinition(id="trigger", type=NodeType.MANUAL_TRIGGER, label="Start"),
        NodeDefinition(id="out", type=NodeType.WEBHOOK_OUTPUT, label="Out",
                       config={"url": "https://example.com/h", "method": "POST"}),
    ]


def _edges():
    return [EdgeDefinition(id="e1", source="trigger", target="out")]


class _Ctx:
    """Patches every collaborator the deploy/rollback handlers reach for."""
    def __init__(self, db):
        self.db = db
        self._patches = [
            patch.object(wf_router, "_db", return_value=db),
            patch.object(wf_router, "get_secure_user_id", return_value="u1"),
            patch.object(wf_router, "_jwt_claims",
                         return_value={"user_id": "u1", "email": "u1@x.io", "org_id": "org-1"}),
            patch.object(wf_router, "_check_workflow_action", return_value=None),
            patch.object(wf_router, "_require_workflow_access", return_value={"org_id": "org-1"}),
            patch.object(wf_router, "_validate_deploy_environment", AsyncMock(return_value=[])),
        ]

    def __enter__(self):
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in self._patches:
            p.stop()


async def _deploy(db, note=""):
    body = wf_router.DeployWorkflowRequest(action="deploy", note=note)
    return await wf_router.deploy_workflow(MagicMock(), "wf-1", body)


async def _rollback(db, version_number, note=""):
    body = wf_router.RollbackWorkflowRequest(version_number=version_number, note=note)
    with patch("citra_workflow.scheduler.scheduler_manager", MagicMock()), \
         patch("citra_workflow.scheduler.WorkflowSchedulerManager.publish_schedule_refresh",
               AsyncMock()):
        return await wf_router.rollback_workflow(MagicMock(), "wf-1", body)


# ─── Tests ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_deploy_appends_snapshot_and_sets_deployed_version():
    db = _FakeDB()
    db["Workflows"].docs.append(_workflow_doc(_linear_nodes(), _edges()))

    with _Ctx(db):
        result = await _deploy(db, note="first ship")

    assert result["deployed_version"] == 1
    wf = db["Workflows"].docs[0]
    assert wf["status"] == "deployed"
    assert wf["deployed_version"] == 1
    assert wf["run_environment"] == "prod"
    assert wf["last_deploy_note"] == "first ship"

    versions = db["WorkflowVersions"].docs
    assert len(versions) == 1
    assert versions[0]["version_number"] == 1
    assert versions[0]["source"] == "deploy"
    assert versions[0]["note"] == "first ship"
    assert versions[0]["deployed_by_email"] == "u1@x.io"
    assert versions[0]["content_hash"]  # non-empty


@pytest.mark.asyncio
async def test_redeploy_increments_version_lineage():
    db = _FakeDB()
    db["Workflows"].docs.append(_workflow_doc(_linear_nodes(), _edges()))

    with _Ctx(db):
        await _deploy(db)
        # simulate an edit changing the graph
        db["Workflows"].docs[0]["nodes"][1]["config"]["method"] = "PUT"
        r2 = await _deploy(db)

    assert r2["deployed_version"] == 2
    assert db["Workflows"].docs[0]["deployed_version"] == 2
    assert len(db["WorkflowVersions"].docs) == 2
    # distinct graphs ⇒ distinct content hashes
    hashes = {v["content_hash"] for v in db["WorkflowVersions"].docs}
    assert len(hashes) == 2


@pytest.mark.asyncio
async def test_webhook_token_preserved_across_redeploys():
    db = _FakeDB()
    nodes = [
        NodeDefinition(id="trigger", type=NodeType.WEBHOOK_TRIGGER, label="Hook"),
        NodeDefinition(id="out", type=NodeType.WEBHOOK_OUTPUT, label="Out",
                       config={"url": "https://example.com/h", "method": "POST"}),
    ]
    db["Workflows"].docs.append(_workflow_doc(nodes, [EdgeDefinition(id="e", source="trigger", target="out")]))

    with _Ctx(db):
        r1 = await _deploy(db)
        token_after_first = db["Workflows"].docs[0]["webhook_token"]
        secret_after_first = db["Workflows"].docs[0]["webhook_secret"]
        assert token_after_first  # minted on first deploy
        # redeploy must NOT rotate the token (safe-deploy fix)
        r2 = await _deploy(db)

    assert db["Workflows"].docs[0]["webhook_token"] == token_after_first
    assert db["Workflows"].docs[0]["webhook_secret"] == secret_after_first
    assert r1["webhook_url"].endswith(token_after_first)
    assert r2["webhook_url"].endswith(token_after_first)


@pytest.mark.asyncio
async def test_rollback_live_restores_graph_and_appends_snapshot():
    db = _FakeDB()
    db["Workflows"].docs.append(_workflow_doc(_linear_nodes(), _edges()))

    with _Ctx(db):
        await _deploy(db)                                  # v1 (method POST)
        # rollback restores exactly the v1 SNAPSHOT graph, so compare to that
        v1_snapshot_nodes = [dict(n) for n in db["WorkflowVersions"].docs[0]["nodes"]]
        db["Workflows"].docs[0]["nodes"][1]["config"]["method"] = "PUT"
        await _deploy(db)                                  # v2 (method PUT)
        result = await _rollback(db, 1, note="bad change")

    assert result["live"] is True
    assert result["restored_from_version"] == 1
    assert result["new_version"] == 3
    wf = db["Workflows"].docs[0]
    assert wf["deployed_version"] == 3
    # live graph now matches v1 (method back to POST)
    assert wf["nodes"][1]["config"]["method"] == "POST"
    assert wf["nodes"] == v1_snapshot_nodes
    # history is append-only: 3 entries, the last is the rollback
    versions = sorted(db["WorkflowVersions"].docs, key=lambda v: v["version_number"])
    assert [v["version_number"] for v in versions] == [1, 2, 3]
    assert versions[2]["source"] == "rollback"
    assert versions[2]["restored_from_version"] == 1


@pytest.mark.asyncio
async def test_rollback_draft_restores_graph_without_touching_lineage():
    db = _FakeDB()
    db["Workflows"].docs.append(_workflow_doc(_linear_nodes(), _edges()))

    with _Ctx(db):
        await _deploy(db)                                  # v1
        db["Workflows"].docs[0]["nodes"][1]["config"]["method"] = "PUT"
        await _deploy(db)                                  # v2
        # undeploy → draft
        db["Workflows"].docs[0]["status"] = "draft"
        result = await _rollback(db, 1)

    assert result["live"] is False
    assert result["new_version"] is None
    wf = db["Workflows"].docs[0]
    # graph restored to v1...
    assert wf["nodes"][1]["config"]["method"] == "POST"
    # ...but lineage untouched (still 2 snapshots, deployed_version still 2)
    assert len(db["WorkflowVersions"].docs) == 2
    assert wf["deployed_version"] == 2


@pytest.mark.asyncio
async def test_list_versions_marks_live_and_orders_newest_first():
    db = _FakeDB()
    db["Workflows"].docs.append(_workflow_doc(_linear_nodes(), _edges()))

    with _Ctx(db):
        await _deploy(db, note="v1")
        db["Workflows"].docs[0]["nodes"][1]["config"]["method"] = "PUT"
        await _deploy(db, note="v2")
        listing = await wf_router.list_workflow_versions(MagicMock(), "wf-1")

    assert listing["deployed_version"] == 2
    assert listing["status"] == "deployed"
    nums = [v["version_number"] for v in listing["versions"]]
    assert nums == [2, 1]  # newest first
    # summaries omit the heavy graph payload
    assert "nodes" not in listing["versions"][0]
    assert listing["versions"][0]["node_count"] == 2


@pytest.mark.asyncio
async def test_retention_caps_history_at_three_keeping_newest():
    db = _FakeDB()
    db["Workflows"].docs.append(_workflow_doc(_linear_nodes(), _edges()))

    with _Ctx(db):
        for i in range(5):  # five deploys ⇒ v1..v5
            db["Workflows"].docs[0]["nodes"][1]["config"]["method"] = f"M{i}"
            await _deploy(db)

    nums = sorted(v["version_number"] for v in db["WorkflowVersions"].docs)
    assert nums == [3, 4, 5]                      # only newest 3 retained
    assert db["Workflows"].docs[0]["deployed_version"] == 5


@pytest.mark.asyncio
async def test_delete_version_removes_non_live_snapshot():
    db = _FakeDB()
    db["Workflows"].docs.append(_workflow_doc(_linear_nodes(), _edges()))

    with _Ctx(db):
        await _deploy(db)                                  # v1
        db["Workflows"].docs[0]["nodes"][1]["config"]["method"] = "PUT"
        await _deploy(db)                                  # v2 (live)
        res = await wf_router.delete_workflow_version(MagicMock(), "wf-1", 1)

    assert res["deleted_version"] == 1
    nums = [v["version_number"] for v in db["WorkflowVersions"].docs]
    assert nums == [2]


@pytest.mark.asyncio
async def test_delete_live_version_is_blocked():
    from fastapi import HTTPException
    db = _FakeDB()
    db["Workflows"].docs.append(_workflow_doc(_linear_nodes(), _edges()))

    with _Ctx(db):
        await _deploy(db)                                  # v1 is live
        with pytest.raises(HTTPException) as ei:
            await wf_router.delete_workflow_version(MagicMock(), "wf-1", 1)
    assert ei.value.status_code == 400
    # nothing deleted
    assert len(db["WorkflowVersions"].docs) == 1


@pytest.mark.asyncio
async def test_list_reports_max_versions():
    db = _FakeDB()
    db["Workflows"].docs.append(_workflow_doc(_linear_nodes(), _edges()))
    with _Ctx(db):
        await _deploy(db)
        listing = await wf_router.list_workflow_versions(MagicMock(), "wf-1")
    assert listing["max_versions"] == wf_router.MAX_WORKFLOW_VERSIONS


@pytest.mark.asyncio
async def test_rollback_to_missing_version_404s():
    from fastapi import HTTPException
    db = _FakeDB()
    db["Workflows"].docs.append(_workflow_doc(_linear_nodes(), _edges()))

    with _Ctx(db):
        await _deploy(db)
        with pytest.raises(HTTPException) as ei:
            await _rollback(db, 99)
    assert ei.value.status_code == 404
