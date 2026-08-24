# Copyright (c) 2026 Trustedwear Tech Private Limited (https://citra-ai.com)
# Author: Rohit Kumar Chandan
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may not
# use this file except in compliance with the License. You may obtain a copy of
# the License at http://www.apache.org/licenses/LICENSE-2.0

"""
Integration tests for the workflow API router.
All external deps (MongoDB, Redis, auth) are mocked.
Uses FastAPI TestClient for synchronous testing.
"""

import sys
import os
import json
import pytest
from unittest.mock import patch, AsyncMock, MagicMock
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from fastapi import FastAPI
from fastapi.testclient import TestClient

from citra_workflow.router import router
from citra_workflow.models import WorkflowStatus, ExecutionStatus


app = FastAPI()
app.include_router(router)


# ── Shared mock helpers ─────────────────────────────────────────────


def _mock_db():
    """Return an AsyncMock that mimics the MongoDB database interface."""
    mock_col = AsyncMock()
    mock_col.find_one = AsyncMock(return_value=None)
    mock_col.insert_one = AsyncMock()
    mock_col.update_one = AsyncMock(return_value=MagicMock(matched_count=1))
    mock_col.count_documents = AsyncMock(return_value=0)

    # find() returns an object supporting async for
    class _AsyncCursorMock:
        def __init__(self):
            self._items = []
        def sort(self, *a, **kw):
            return self
        def skip(self, *a, **kw):
            return self
        def limit(self, *a, **kw):
            return self
        def __aiter__(self):
            return self
        async def __anext__(self):
            raise StopAsyncIteration

    mock_col.find = MagicMock(return_value=_AsyncCursorMock())
    # aggregate() must also return an async-iterable, not a coroutine
    mock_col.aggregate = MagicMock(return_value=_AsyncCursorMock())

    mock_db = MagicMock()
    mock_db.__getitem__ = MagicMock(return_value=mock_col)
    return mock_db, mock_col


AUTH_PATCH = patch("citra_workflow.router.get_secure_user_id", return_value="test_user")

# The workflow surface is gated by _require_workflow_access → _jwt_claims(request).
# This bare TestClient app mounts no auth middleware to populate request.state, so
# stub claims with a workflow-capable identity (super_admin + org), honouring the
# fixture's "auth is mocked" contract. Without this the gate 403s every handler.
CLAIMS_PATCH = patch(
    "citra_workflow.router._jwt_claims",
    return_value={
        "user_id": "test_user", "email": "test_user@example.com",
        "org_id": "test_org", "roles": ["super_admin"], "dept_ids": [],
        "is_service_account": False, "service_account_admin_of": [],
        "service_account_member_of": [], "work_sa_id": "", "personal_sa_id": "",
    },
)


@pytest.fixture
def db_and_client():
    """Fixture: returns (mock_db, mock_collection, TestClient) with auth patched."""
    mock_db, mock_col = _mock_db()
    with AUTH_PATCH, CLAIMS_PATCH:
        with patch("citra_workflow.router._db", return_value=mock_db):
            client = TestClient(app)
            yield mock_db, mock_col, client


@pytest.mark.integration
class TestWorkflowCRUD:

    def test_create_workflow(self, db_and_client):
        _, mock_col, client = db_and_client
        resp = client.post("/api/workflows", json={"name": "Test WF"})
        assert resp.status_code == 200
        data = resp.json()
        assert "workflow_id" in data
        mock_col.insert_one.assert_awaited_once()

    def test_list_workflows(self, db_and_client):
        _, _, client = db_and_client
        resp = client.get("/api/workflows")
        assert resp.status_code == 200
        assert "workflows" in resp.json()

    def test_get_workflow_not_found(self, db_and_client):
        _, mock_col, client = db_and_client
        mock_col.find_one.return_value = None
        resp = client.get("/api/workflows/nonexistent")
        assert resp.status_code == 404

    def test_get_workflow_found(self, db_and_client):
        _, mock_col, client = db_and_client
        mock_col.find_one.return_value = {
            "_id": "obj123",
            "workflow_id": "w1",
            "user_id": "test_user",
            "name": "Found",
        }
        resp = client.get("/api/workflows/w1")
        assert resp.status_code == 200
        assert resp.json()["name"] == "Found"

    def test_update_workflow(self, db_and_client):
        _, mock_col, client = db_and_client
        # Update authorises against the loaded doc (org-ownership) before
        # writing, so the workflow must exist and belong to the caller's org.
        mock_col.find_one.return_value = {
            "workflow_id": "w1", "org_id": "test_org", "user_id": "test_user",
        }
        mock_col.update_one.return_value = MagicMock(matched_count=1)
        resp = client.put("/api/workflows/w1", json={"name": "Renamed"})
        assert resp.status_code == 200

    def test_update_workflow_not_found(self, db_and_client):
        _, mock_col, client = db_and_client
        mock_col.update_one.return_value = MagicMock(matched_count=0)
        resp = client.put("/api/workflows/w1", json={"name": "X"})
        assert resp.status_code == 404

    def test_delete_workflow(self, db_and_client):
        _, mock_col, client = db_and_client
        mock_col.find_one.return_value = {
            "workflow_id": "w1", "org_id": "test_org", "user_id": "test_user",
        }
        mock_col.update_one.return_value = MagicMock(matched_count=1)
        resp = client.delete("/api/workflows/w1")
        assert resp.status_code == 200


@pytest.mark.integration
class TestNodeSchemas:

    def test_list_node_schemas(self, db_and_client):
        _, _, client = db_and_client
        resp = client.get("/api/workflows/node-schemas")
        assert resp.status_code == 200
        schemas = resp.json()["schemas"]
        assert len(schemas) >= 35
        types = {s["type"] for s in schemas}
        assert "llm_processor" in types
        assert "ai_agent" in types
        assert "dept_mcp_source" in types

    def test_list_agent_tools(self, db_and_client):
        _, _, client = db_and_client
        resp = client.get("/api/workflows/agent-tools")
        assert resp.status_code == 200
        tools = resp.json()["tools"]
        names = {t["name"] for t in tools}
        assert "web_search" in names


@pytest.mark.integration
class TestExecution:

    def test_execute_workflow_not_found(self, db_and_client):
        _, mock_col, client = db_and_client
        mock_col.find_one.return_value = None
        resp = client.post("/api/workflows/w1/execute", json={})
        assert resp.status_code == 404

    def test_execute_workflow_success(self, db_and_client):
        _, mock_col, client = db_and_client
        # Execute enqueues a `workflow.run` job for Citra-Worker (it no longer
        # runs inline). It mints the workflow's org-level execution token first,
        # so the doc needs an org_id; the queue + token mint are stubbed.
        mock_col.find_one.return_value = {
            "workflow_id": "w1",
            "org_id": "test_org",
            "user_id": "test_user",
            "name": "Test",
            "nodes": [{"id": "n1", "type": "manual_trigger", "label": "Start", "position": {"x": 0, "y": 0}, "config": {}}],
            "edges": [],
            "status": "draft",
            "schedule": {"enabled": False},
            "variables": {},
            "is_active": True,
            "version": 1,
        }

        with patch("citra_auth.mint_workflow_org_token", return_value="fake.jwt.token"), \
             patch("citra_queue.enqueue", return_value="job1"):
            resp = client.post("/api/workflows/w1/execute", json={})

        assert resp.status_code == 200
        # Router generates a fresh execution_id (UUID); just verify it exists.
        assert isinstance(resp.json().get("execution_id"), str)
        assert len(resp.json()["execution_id"]) > 0


@pytest.mark.integration
class TestDeployment:

    def test_deploy_no_nodes_fails(self, db_and_client):
        _, mock_col, client = db_and_client
        mock_col.find_one.return_value = {
            "workflow_id": "w1",
            "user_id": "test_user",
            "nodes": [],
        }
        resp = client.post("/api/workflows/w1/deploy", json={"action": "deploy"})
        assert resp.status_code == 400

    def test_deploy_with_nodes_succeeds(self, db_and_client):
        _, mock_col, client = db_and_client
        mock_col.find_one.return_value = {
            "workflow_id": "w1",
            "user_id": "test_user",
            "nodes": [{"type": "manual_trigger"}],
            "schedule": {},
        }
        with patch("citra_workflow.scheduler.WorkflowSchedulerManager.register_workflow"):
            resp = client.post("/api/workflows/w1/deploy", json={"action": "deploy"})
        assert resp.status_code == 200
        assert resp.json()["status"] == "deployed"


@pytest.mark.integration
class TestWebhookTrigger:

    def test_webhook_not_found(self, db_and_client):
        _, mock_col, client = db_and_client
        mock_col.find_one.return_value = None
        resp = client.post("/api/workflows/webhook/badtoken", json={})
        assert resp.status_code == 404

    def test_webhook_triggers_execution(self, db_and_client):
        # Webhooks are unauthenticated by JWT but require a mandatory HMAC
        # signature over `"{timestamp}.".encode() + raw_body` using the
        # workflow's webhook_secret (fail-closed). The trigger enqueues a
        # `workflow.run` job and returns a fresh execution_id.
        import hmac as _hmac
        import hashlib as _hashlib
        import time as _time

        _, mock_col, client = db_and_client
        secret = "whsec-123"
        mock_col.find_one.return_value = {
            "workflow_id": "w1",
            "org_id": "test_org",
            "user_id": "u1",
            "name": "WH Workflow",
            "webhook_token": "tok123",
            "webhook_secret": secret,
            "nodes": [{"id": "n1", "type": "webhook_trigger", "label": "", "position": {"x": 0, "y": 0}, "config": {}}],
            "edges": [],
            "status": "deployed",
            "schedule": {"enabled": False},
            "variables": {},
            "is_active": True,
            "version": 1,
        }
        body = json.dumps({"event": "new_order"}).encode()
        ts = str(int(_time.time()))
        sig = _hmac.new(secret.encode(), f"{ts}.".encode() + body, _hashlib.sha256).hexdigest()

        with patch("citra_auth.mint_workflow_org_token", return_value="fake.jwt.token"), \
             patch("citra_queue.enqueue", return_value="job-wh"):
            resp = client.post(
                "/api/workflows/webhook/tok123",
                content=body,
                headers={
                    "X-Webhook-Signature": sig,
                    "X-Webhook-Timestamp": ts,
                    "Content-Type": "application/json",
                },
            )

        assert resp.status_code == 200
        assert isinstance(resp.json().get("execution_id"), str)
        assert resp.json()["status"] == "queued"
