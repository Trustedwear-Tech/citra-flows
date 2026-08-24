# Copyright (c) 2026 Trustedwear Tech Private Limited (https://citra-ai.com)
# Author: Rohit Kumar Chandan
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may not
# use this file except in compliance with the License. You may obtain a copy of
# the License at http://www.apache.org/licenses/LICENSE-2.0

"""Integration tests for the AI workflow endpoints.

Exercises the full path: HTTP request → router endpoint → context
gather → LLM mock → response (with validation) → diff (for refine).
The LLM is mocked so the test is deterministic; the rest of the
pipeline runs for real.
"""

import sys
import os
import json
import pytest
from unittest.mock import patch, AsyncMock, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from fastapi import FastAPI
from fastapi.testclient import TestClient

from citra_workflow.router import router


# Fresh app per test module. Middleware is attached once at module load
# (FastAPI rejects post-startup middleware additions) so every fixture
# sees the same stubbed state.
_app = FastAPI()


@_app.middleware("http")
async def _attach_state(request, call_next):
    request.state.org_id = "acme"
    request.state.dept_ids = ["claims"]
    # Workflow surface RBAC (_require_workflow_access) needs a workflow-capable
    # role; without it every handler 403s. org_admin satisfies the gate.
    request.state.roles = ["org_admin"]
    return await call_next(request)


_app.include_router(router)


def _mock_mongo_with_connections(connections):
    """Build an async mongo client that returns ``connections`` from
    WorkflowConnections.find() and behaves like an empty db elsewhere."""
    class _Cursor:
        def __init__(self, items):
            self._items = items
        def sort(self, *_a, **_kw):
            return self
        def __aiter__(self):
            self._iter = iter(self._items)
            return self
        async def __anext__(self):
            try:
                return next(self._iter)
            except StopIteration:
                raise StopAsyncIteration

    col = MagicMock()
    col.find = MagicMock(return_value=_Cursor(connections))
    col.find_one = AsyncMock(return_value=None)
    col.insert_one = AsyncMock()
    col.update_one = AsyncMock(return_value=MagicMock(matched_count=1))
    col.count_documents = AsyncMock(return_value=0)
    col.aggregate = MagicMock(return_value=_Cursor([]))

    db = MagicMock()
    db.__getitem__ = MagicMock(return_value=col)
    client = MagicMock()
    client.__getitem__ = MagicMock(return_value=db)
    return client, db, col


# ── Mock helpers for the LLM + auth ──────────────────────────────────


def _llm_returning(payload):
    """Build a sync mock for citra_llm.oss.llm_call that returns the
    JSON-serialised ``payload`` regardless of args."""
    def _fake(**_kwargs):
        return json.dumps(payload)
    return _fake


@pytest.fixture(autouse=True)
def _no_ai_rate_limit():
    """Disable the AI rate limiter for these tests.

    ``_enforce_ai_rate_limit`` uses a process-wide *distributed* counter
    (``ai:user:`` / ``ai:org:``) that is NOT reset between tests. Left alone,
    the cumulative AI-endpoint call count across the whole suite eventually
    trips the ceiling and 429s whichever AI tests happen to run last — making
    the suite order-dependent. None of these tests exercise throttling, so we
    bypass the limiter for hermetic, order-independent runs."""
    with patch("citra_workflow.router._check_rate_limit_distributed", return_value=True):
        yield


@pytest.fixture
def http_client():
    """FastAPI test client with auth + mongo stubbed."""
    mongo_client, _, _ = _mock_mongo_with_connections([])
    with patch("citra_workflow.router.get_secure_user_id", return_value="alice@acme.com"), \
         patch("citra_mongo.get_async_mongo_client", return_value=mongo_client), \
         patch("citra_mongo.MONGODB_DATABASE", "testdb"):
        yield TestClient(_app)


# ── Generate endpoint ─────────────────────────────────────────────────


class TestGenerateWorkflow:

    def test_returns_workflow_with_validation_clean(self, http_client):
        """A clean workflow with valid node types and no MCP refs passes
        validation and returns is_clean=True."""
        payload = {
            "name": "Simple", "description": "A starter",
            "icon": "🤖", "tags": ["test"],
            "nodes": [
                {"id": "t1", "type": "manual_trigger", "config": {}},
                {"id": "p1", "type": "data_transform", "config": {"mapping": {}}},
            ],
            "edges": [{"id": "e1", "source": "t1", "target": "p1"}],
            "variables": {},
            "suggestions": ["Add a writer at the end"],
        }
        with patch("citra_llm.oss.llm_call", _llm_returning(payload)):
            resp = http_client.post(
                "/api/workflows/generate-workflow",
                json={"prompt": "build a simple workflow"},
            )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["name"] == "Simple"
        assert len(body["nodes"]) == 2
        assert body["validation"]["is_clean"] is True
        assert body["validation"]["errors"] == []

    def test_generate_passes_reply_and_prerequisites_through(self, http_client):
        """When the AI asks a clarifying question / flags a user-side setup
        step, generate surfaces `reply` and `prerequisites` to the UI."""
        payload = {
            "name": "Theft Report", "description": "drafted",
            "icon": "🚨", "tags": [],
            "nodes": [{"id": "t1", "type": "manual_trigger", "config": {}}],
            "edges": [], "variables": {},
            "reply": "Which database holds the theft incidents? I drafted a "
                     "best-guess graph meanwhile.",
            "suggestions": ["Switch to a daily 6 PM schedule"],
            "prerequisites": ["Register a SQL connection in the Connections screen."],
        }
        with patch("citra_llm.oss.llm_call", _llm_returning(payload)):
            resp = http_client.post(
                "/api/workflows/generate-workflow",
                json={"prompt": "create a theft report and email it"},
            )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["reply"].startswith("Which database")
        assert body["prerequisites"]
        assert body["suggestions"] == ["Switch to a daily 6 PM schedule"]

    def test_empty_connection_is_a_deterministic_setup_gap(self, http_client):
        """A node whose connection_picker is empty (no inline config) is
        SILENTLY unrunnable — the reference validator skips it, so it
        validates 'clean'. detect_setup_gaps must catch it and surface a
        prerequisite regardless of whether the LLM mentioned it. This is the
        theft-report 'clean but can't run' failure."""
        payload = {
            "name": "Theft", "description": "", "icon": "🚨", "tags": [],
            "nodes": [
                {"id": "t1", "type": "manual_trigger", "config": {}},
                {"id": "s1", "type": "sql_source", "config": {
                    "connection_id": "", "query": "SELECT 1",
                }},
            ],
            "edges": [{"id": "e1", "source": "t1", "target": "s1"}],
            "variables": {},
            "suggestions": [], "prerequisites": [],  # LLM forgot to mention it
        }
        with patch("citra_llm.oss.llm_call", _llm_returning(payload)):
            resp = http_client.post(
                "/api/workflows/generate-workflow",
                json={"prompt": "query theft incidents"},
            )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        # No reference *error* — the empty id is skipped by validate_*
        assert body["validation"]["is_clean"] is True
        # But a deterministic setup gap IS surfaced...
        assert len(body["setup_gaps"]) == 1
        assert body["setup_gaps"][0]["node_id"] == "s1"
        # ...as a prerequisite, NOT a clickable refinement
        assert any("connection" in p.lower() for p in body["prerequisites"])
        assert body["suggestions"] == []

    def test_inline_connection_is_not_a_setup_gap(self, http_client):
        """A node with no connection_id but an inline connection string can
        still run — don't flag it."""
        payload = {
            "name": "Inline", "description": "", "icon": "🤖", "tags": [],
            "nodes": [
                {"id": "t1", "type": "manual_trigger", "config": {}},
                {"id": "s1", "type": "sql_source", "config": {
                    "connection_id": "",
                    "connection_string": "postgresql://h/db",
                    "query": "SELECT 1",
                }},
            ],
            "edges": [{"id": "e1", "source": "t1", "target": "s1"}],
            "variables": {}, "suggestions": [],
        }
        with patch("citra_llm.oss.llm_call", _llm_returning(payload)):
            resp = http_client.post(
                "/api/workflows/generate-workflow",
                json={"prompt": "query with inline creds"},
            )
        body = resp.json()
        assert body["setup_gaps"] == []

    def test_selected_connection_is_not_a_setup_gap(self, http_client):
        """When a connection IS chosen it's not a setup gap — if the id is
        unknown that's a reference error instead, handled separately."""
        payload = {
            "name": "Picked", "description": "", "icon": "🤖", "tags": [],
            "nodes": [
                {"id": "t1", "type": "manual_trigger", "config": {}},
                {"id": "s1", "type": "sql_source", "config": {
                    "connection_id": "phantom", "query": "SELECT 1",
                }},
            ],
            "edges": [{"id": "e1", "source": "t1", "target": "s1"}],
            "variables": {}, "suggestions": [],
        }
        with patch("citra_llm.oss.llm_call", _llm_returning(payload)):
            resp = http_client.post(
                "/api/workflows/generate-workflow",
                json={"prompt": "query with a picked connection"},
            )
        body = resp.json()
        assert body["setup_gaps"] == []
        codes = [e["code"] for e in body["validation"]["errors"]]
        assert "E_UNKNOWN_CONNECTION" in codes

    def test_clarification_returns_questions_and_no_nodes(self, http_client):
        """For a too-vague request the AI may ask instead of guessing:
        needs_clarification=True, empty nodes, questions in reply."""
        payload = {
            "name": "", "description": "", "icon": "🤖", "tags": [],
            "nodes": [], "edges": [], "variables": {},
            "needs_clarification": True,
            "reply": "Which system holds the approvals, and what should happen "
                     "when one is approved?",
            "suggestions": [], "prerequisites": [],
        }
        with patch("citra_llm.oss.llm_call", _llm_returning(payload)):
            resp = http_client.post(
                "/api/workflows/generate-workflow",
                json={"prompt": "automate my approvals"},
            )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["needs_clarification"] is True
        assert body["nodes"] == []
        assert "approv" in body["reply"].lower()

    def test_empty_nodes_with_reply_is_treated_as_clarification(self, http_client):
        """Even without the explicit flag, an empty graph + a message is an
        implicit question — the server marks needs_clarification."""
        payload = {
            "name": "", "description": "", "icon": "🤖", "tags": [],
            "nodes": [], "edges": [], "variables": {},
            "reply": "What should trigger this?",
            "suggestions": [],
        }
        with patch("citra_llm.oss.llm_call", _llm_returning(payload)):
            resp = http_client.post(
                "/api/workflows/generate-workflow",
                json={"prompt": "do a thing"},
            )
        body = resp.json()
        assert body["needs_clarification"] is True

    def test_unknown_connection_id_is_flagged(self, http_client):
        """The AI references a non-existent connection — validation
        flags it and the user can fix it before applying."""
        payload = {
            "name": "Bad", "description": "", "icon": "🤖", "tags": [],
            "nodes": [
                {"id": "t1", "type": "manual_trigger", "config": {}},
                {"id": "s1", "type": "sql_source", "config": {
                    "connection_id": "phantom_sql", "query": "SELECT 1",
                }},
            ],
            "edges": [{"id": "e1", "source": "t1", "target": "s1"}],
            "variables": {}, "suggestions": [],
        }
        with patch("citra_llm.oss.llm_call", _llm_returning(payload)):
            resp = http_client.post(
                "/api/workflows/generate-workflow",
                json={"prompt": "build a sql query workflow"},
            )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        codes = [e["code"] for e in body["validation"]["errors"]]
        assert "E_UNKNOWN_CONNECTION" in codes

    def test_invalid_json_is_422(self, http_client):
        with patch("citra_llm.oss.llm_call", return_value="not json at all"):
            resp = http_client.post(
                "/api/workflows/generate-workflow",
                json={"prompt": "x"},
            )
        assert resp.status_code == 422

    def test_empty_prompt_is_400(self, http_client):
        resp = http_client.post(
            "/api/workflows/generate-workflow",
            json={"prompt": "   "},
        )
        assert resp.status_code == 400


# ── Refine endpoint (diff!) ───────────────────────────────────────────


class TestRefineWorkflow:

    def test_refine_returns_diff_patch(self, http_client):
        """Refining returns BOTH the new full workflow AND a diff so
        the UI can apply patches without wiping manual edits."""
        current = {
            "name": "Old", "nodes": [
                {"id": "t1", "type": "manual_trigger",
                 "label": "Trigger", "position": {"x": 0, "y": 0}, "config": {}},
            ],
            "edges": [], "variables": {},
        }
        after = {
            "name": "New",
            "description": "Added a transform",
            "icon": "🤖", "tags": [],
            "nodes": [
                {"id": "t1", "type": "manual_trigger",
                 "label": "Trigger", "position": {"x": 0, "y": 0}, "config": {}},
                {"id": "p1", "type": "data_transform",
                 "label": "Transform", "position": {"x": 0, "y": 200},
                 "config": {"mapping": {"x": "$.y"}}},
            ],
            "edges": [{"id": "e1", "source": "t1", "target": "p1"}],
            "variables": {}, "suggestions": [],
        }
        with patch("citra_llm.oss.llm_call", _llm_returning(after)):
            resp = http_client.post(
                "/api/workflows/generate-workflow/refine",
                json={
                    "prompt": "add a transform after the trigger",
                    "workflow": current,
                    "return_diff": True,
                },
            )
        assert resp.status_code == 200, resp.text
        body = resp.json()

        # Full workflow returned
        assert body["name"] == "New"
        assert len(body["nodes"]) == 2

        # Diff also returned
        diff = body["diff"]
        assert len(diff["nodes_added"]) == 1
        assert diff["nodes_added"][0]["id"] == "p1"
        assert diff["nodes_removed"] == []
        assert diff["nodes_updated"] == []
        assert len(diff["edges_added"]) == 1

    def test_refine_preserves_dept_flow_internals(self, http_client):
        """When the existing workflow contains a chunk_embed, refine
        must not flag it as forbidden — it was already there."""
        current = {
            "name": "Existing", "nodes": [
                {"id": "ck1", "type": "chunk_embed", "config": {"dept_id": "claims"}},
            ],
            "edges": [], "variables": {},
        }
        after = dict(current)
        after.update({
            "description": "unchanged",
            "icon": "🤖", "tags": [], "suggestions": [],
        })
        with patch("citra_llm.oss.llm_call", _llm_returning(after)):
            resp = http_client.post(
                "/api/workflows/generate-workflow/refine",
                json={"prompt": "no-op", "workflow": current, "return_diff": True},
            )
        assert resp.status_code == 200
        body = resp.json()
        codes = [e["code"] for e in body["validation"]["errors"]]
        # is_fresh=False in refine → no E_FORBIDDEN_NODE_TYPE
        assert "E_FORBIDDEN_NODE_TYPE" not in codes

    def test_refine_without_diff_flag(self, http_client):
        """Legacy clients can ask for full workflow only via return_diff=False."""
        after = {
            "name": "X", "description": "", "icon": "🤖", "tags": [],
            "nodes": [
                {"id": "t1", "type": "manual_trigger", "config": {}},
            ],
            "edges": [], "variables": {}, "suggestions": [],
        }
        with patch("citra_llm.oss.llm_call", _llm_returning(after)):
            resp = http_client.post(
                "/api/workflows/generate-workflow/refine",
                json={
                    "prompt": "any change",
                    "workflow": {"nodes": [], "edges": []},
                    "return_diff": False,
                },
            )
        body = resp.json()
        assert "diff" not in body
        assert body["nodes"]

    def test_refine_noop_sets_reply_and_flag(self, http_client):
        """A refine that changes nothing must NOT return a silent identical
        graph — it sets no_op=True and synthesises a `reply` so the user
        isn't left staring at an unchanged canvas (the bug Rohit caught:
        clicking a 'register a connection' suggestion looped to a no-op)."""
        current = {
            "name": "Daily Theft Report",
            "nodes": [
                {"id": "t1", "type": "manual_trigger",
                 "label": "Trigger", "position": {"x": 0, "y": 0}, "config": {}},
                {"id": "s1", "type": "sql_source",
                 "label": "Query", "position": {"x": 0, "y": 200},
                 "config": {"connection_id": "", "query": "SELECT 1"}},
            ],
            "edges": [{"id": "e1", "source": "t1", "target": "s1"}],
            "variables": {},
        }
        # LLM echoes the same graph back (it can't register a connection) and
        # correctly routes the setup step to prerequisites with a reply.
        after = {
            "name": "Daily Theft Report", "description": "", "icon": "🚨", "tags": [],
            "nodes": current["nodes"],
            "edges": current["edges"],
            "variables": {},
            "reply": "I can't register connections from here — do that in the "
                     "Connections screen, then tell me its name.",
            "suggestions": [],
            "prerequisites": ["Register a SQL connection in the Connections screen."],
        }
        with patch("citra_llm.oss.llm_call", _llm_returning(after)):
            resp = http_client.post(
                "/api/workflows/generate-workflow/refine",
                json={
                    "prompt": "Register a SQL database connection",
                    "workflow": current,
                    "return_diff": True,
                },
            )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["no_op"] is True
        assert body["reply"]  # non-empty conversational message
        assert body["prerequisites"]
        # The setup step is NOT a clickable refinement
        assert body["suggestions"] == []

    def test_refine_noop_synthesises_reply_when_llm_silent(self, http_client):
        """If the LLM returns an unchanged graph with no reply at all, the
        server still fills one in rather than returning a bare no-op."""
        current = {
            "name": "WF",
            "nodes": [{"id": "t1", "type": "manual_trigger",
                       "label": "T", "position": {"x": 0, "y": 0}, "config": {}}],
            "edges": [], "variables": {},
        }
        after = {
            "name": "WF", "description": "", "icon": "🤖", "tags": [],
            "nodes": current["nodes"], "edges": [], "variables": {},
            "suggestions": [],  # no reply, no prerequisites
        }
        with patch("citra_llm.oss.llm_call", _llm_returning(after)):
            resp = http_client.post(
                "/api/workflows/generate-workflow/refine",
                json={"prompt": "make it better", "workflow": current,
                      "return_diff": True},
            )
        body = resp.json()
        assert body["no_op"] is True
        assert body["reply"]  # synthesised, asks what to change

    def test_refine_real_change_is_not_noop(self, http_client):
        """A refine that DOES change the graph carries no_op=False."""
        current = {
            "name": "Old",
            "nodes": [{"id": "t1", "type": "manual_trigger",
                       "label": "T", "position": {"x": 0, "y": 0}, "config": {}}],
            "edges": [], "variables": {},
        }
        after = {
            "name": "New", "description": "", "icon": "🤖", "tags": [],
            "nodes": [
                {"id": "t1", "type": "manual_trigger",
                 "label": "T", "position": {"x": 0, "y": 0}, "config": {}},
                {"id": "p1", "type": "data_transform",
                 "label": "X", "position": {"x": 0, "y": 200},
                 "config": {"mapping": {}}},
            ],
            "edges": [{"id": "e1", "source": "t1", "target": "p1"}],
            "variables": {}, "suggestions": [],
        }
        with patch("citra_llm.oss.llm_call", _llm_returning(after)):
            resp = http_client.post(
                "/api/workflows/generate-workflow/refine",
                json={"prompt": "add a transform", "workflow": current,
                      "return_diff": True},
            )
        body = resp.json()
        assert body["no_op"] is False


# ── Edit-node endpoint ────────────────────────────────────────────────


class TestEditNode:

    def test_edit_one_node_returns_updated_config(self, http_client):
        workflow = {
            "name": "WF", "nodes": [
                {"id": "t1", "type": "manual_trigger", "config": {}},
                {"id": "s1", "type": "sql_source", "config": {
                    "connection_id": "", "query": "SELECT old",
                }},
            ],
            "edges": [{"id": "e1", "source": "t1", "target": "s1"}],
            "variables": {},
        }
        edited = {
            "node": {
                "id": "s1", "type": "sql_source",
                "label": "Customer Query", "position": {"x": 0, "y": 200},
                "config": {"connection_id": "", "query": "SELECT id FROM customers"},
            },
            "rationale": "Targeted the customers table per the request.",
            "suggestions": [],
        }
        with patch("citra_llm.oss.llm_call", _llm_returning(edited)):
            resp = http_client.post(
                "/api/workflows/edit-node",
                json={"prompt": "use customers table", "node_id": "s1",
                      "workflow": workflow},
            )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["node"]["id"] == "s1"
        assert "customers" in body["node"]["config"]["query"]
        assert body["validation"]["is_clean"] is True

    def test_edit_unknown_node_id_404(self, http_client):
        workflow = {"nodes": [{"id": "t1", "type": "manual_trigger", "config": {}}],
                    "edges": [], "variables": {}}
        # No LLM patch — the 404 must fire before any call
        resp = http_client.post(
            "/api/workflows/edit-node",
            json={"prompt": "fix", "node_id": "no_such_node", "workflow": workflow},
        )
        assert resp.status_code == 404

    def test_edit_rejects_id_swap(self, http_client):
        """If the LLM tries to return a node with a different id, we
        reject — that would silently corrupt the canvas."""
        workflow = {
            "nodes": [{"id": "s1", "type": "sql_source", "config": {"query": "SELECT 1"}}],
            "edges": [], "variables": {},
        }
        bad = {
            "node": {"id": "DIFFERENT", "type": "sql_source", "config": {}},
            "rationale": "", "suggestions": [],
        }
        with patch("citra_llm.oss.llm_call", _llm_returning(bad)):
            resp = http_client.post(
                "/api/workflows/edit-node",
                json={"prompt": "edit", "node_id": "s1", "workflow": workflow},
            )
        assert resp.status_code == 422


# ── Self-correction retry on malformed LLM output ────────────────────


class TestSelfCorrectionRetry:
    """The LLM (esp. GLM) occasionally emits invalid JSON or prose around it.
    A single bad response must NOT fail the whole turn — the builder retries
    once with the error fed back, mirroring the MCP NL→SQL planner."""

    def _good_workflow(self):
        return {
            "name": "Recovered", "description": "", "icon": "🤖", "tags": [],
            "nodes": [{"id": "t1", "type": "manual_trigger", "config": {}}],
            "edges": [], "variables": {}, "suggestions": [],
        }

    def test_generate_retries_once_then_succeeds(self, http_client):
        calls = {"n": 0}
        good = self._good_workflow()

        def _seq(**_kwargs):
            calls["n"] += 1
            # First response is unparseable prose; second is valid JSON.
            return "Sure! Here is your workflow:" if calls["n"] == 1 else json.dumps(good)

        with patch("citra_llm.oss.llm_call", side_effect=_seq):
            resp = http_client.post(
                "/api/workflows/generate-workflow",
                json={"prompt": "build something"},
            )
        assert resp.status_code == 200, resp.text
        assert resp.json()["name"] == "Recovered"
        assert calls["n"] == 2  # exactly one retry

    def test_generate_gives_up_after_one_retry(self, http_client):
        calls = {"n": 0}

        def _bad(**_kwargs):
            calls["n"] += 1
            return "still not json"

        with patch("citra_llm.oss.llm_call", side_effect=_bad):
            resp = http_client.post(
                "/api/workflows/generate-workflow",
                json={"prompt": "build something"},
            )
        assert resp.status_code == 422
        assert calls["n"] == 2  # original + one repair attempt, then give up

    def test_refine_retries_on_dag_error_then_succeeds(self, http_client):
        """A DAG-invalid first response (cycle) triggers the same retry."""
        calls = {"n": 0}
        cyclic = {
            "name": "Bad", "description": "", "icon": "🤖", "tags": [],
            "nodes": [
                {"id": "a", "type": "manual_trigger", "config": {}},
                {"id": "b", "type": "data_transform", "config": {"mapping": {}}},
            ],
            "edges": [
                {"id": "e1", "source": "a", "target": "b"},
                {"id": "e2", "source": "b", "target": "a"},  # cycle
            ],
            "variables": {}, "suggestions": [],
        }
        good = {
            "name": "Fixed", "description": "", "icon": "🤖", "tags": [],
            "nodes": [
                {"id": "a", "type": "manual_trigger", "config": {}},
                {"id": "b", "type": "data_transform", "config": {"mapping": {}}},
            ],
            "edges": [{"id": "e1", "source": "a", "target": "b"}],
            "variables": {}, "suggestions": [],
        }

        def _seq(**_kwargs):
            calls["n"] += 1
            return json.dumps(cyclic if calls["n"] == 1 else good)

        with patch("citra_llm.oss.llm_call", side_effect=_seq):
            resp = http_client.post(
                "/api/workflows/generate-workflow/refine",
                json={
                    "prompt": "fix it",
                    "workflow": {"nodes": [{"id": "a", "type": "manual_trigger",
                                            "config": {}}], "edges": []},
                    "return_diff": True,
                },
            )
        assert resp.status_code == 200, resp.text
        assert resp.json()["name"] == "Fixed"
        assert calls["n"] == 2


# ── Starter prompts ──────────────────────────────────────────────────


class TestStarterPrompts:

    def test_returns_list(self, http_client):
        resp = http_client.get("/api/workflows/starter-prompts")
        assert resp.status_code == 200
        body = resp.json()
        prompts = body["prompts"]
        assert isinstance(prompts, list)
        assert len(prompts) >= 1
        # Every entry must carry a label
        for p in prompts:
            assert "label" in p


# ── Multi-turn conversation (the bug Rohit caught) ───────────────────


class TestMultiTurnConversation:
    """Lock in the contract that the LLM receives prior assistant
    artefacts in the conversation history, so it has continuity across
    turns even when the user hasn't clicked Apply."""

    def test_refine_passes_enriched_conversation_to_llm(self, http_client):
        """When the UI sends an enriched conversation history (with
        the bracketed summaries the WorkflowAIChat now adds), the
        backend formatter wires it verbatim into the user-message."""
        observed = {}

        def _spy(**kwargs):
            observed["system_prompt"] = kwargs.get("system_prompt", "")
            observed["user_prompt"] = kwargs.get("user_prompt", "")
            return json.dumps({
                "name": "X", "description": "", "icon": "🤖", "tags": [],
                "nodes": [{"id": "t1", "type": "manual_trigger", "config": {}}],
                "edges": [], "variables": {}, "suggestions": [],
            })

        with patch("citra_llm.oss.llm_call", side_effect=_spy):
            resp = http_client.post(
                "/api/workflows/generate-workflow/refine",
                json={
                    "prompt": "now add an approval step",
                    "workflow": {
                        "nodes": [{"id": "t1", "type": "manual_trigger", "config": {}}],
                        "edges": [],
                    },
                    "conversation": [
                        {"role": "user", "content": "build a workflow"},
                        {"role": "assistant",
                         "content": "Generated: **X**\n"
                                    "[produced full workflow: 1 nodes, 0 edges]"},
                    ],
                    "return_diff": True,
                },
            )
        assert resp.status_code == 200, resp.text

        # The conversation summaries make it into the LLM's user message
        assert "build a workflow" in observed["user_prompt"]
        assert "[produced full workflow: 1 nodes, 0 edges]" in observed["user_prompt"]
        assert "now add an approval step" in observed["user_prompt"]

        # The system prompt carries the dynamic context (palette section
        # is always present even when connections/catalogue are empty)
        assert "Available Node Types" in observed["system_prompt"]
        assert "Available Connections" in observed["system_prompt"]

    def test_long_conversation_is_truncated(self, http_client):
        """A long building session must not grow the prompt unbounded — only
        the most recent turns are replayed; older ones collapse to a marker."""
        observed = {}

        def _spy(**kwargs):
            observed["user_prompt"] = kwargs.get("user_prompt", "")
            return json.dumps({
                "name": "X", "description": "", "icon": "🤖", "tags": [],
                "nodes": [{"id": "t1", "type": "manual_trigger", "config": {}}],
                "edges": [], "variables": {}, "suggestions": [],
            })

        convo = [{"role": "user", "content": f"oldmsg{i}"} for i in range(40)]
        with patch("citra_llm.oss.llm_call", side_effect=_spy):
            resp = http_client.post(
                "/api/workflows/generate-workflow",
                json={"prompt": "build it", "conversation": convo},
            )
        assert resp.status_code == 200, resp.text
        up = observed["user_prompt"]
        assert "earlier message(s) omitted" in up
        assert "oldmsg0" not in up          # oldest dropped
        assert "oldmsg39" in up             # newest kept
        assert "build it" in up             # the live prompt

    def test_focused_node_edit_passes_node_context(self, http_client):
        """The per-node edit endpoint feeds the focused node's current
        value into the LLM prompt verbatim, plus the surrounding
        workflow for context."""
        observed = {}

        def _spy(**kwargs):
            observed["user_prompt"] = kwargs.get("user_prompt", "")
            return json.dumps({
                "node": {"id": "s1", "type": "sql_source",
                         "label": "x", "position": {"x": 0, "y": 0},
                         "config": {"query": "SELECT new"}},
                "rationale": "ok", "suggestions": [],
            })

        workflow = {
            "nodes": [
                {"id": "t1", "type": "manual_trigger", "config": {}},
                {"id": "s1", "type": "sql_source",
                 "config": {"query": "SELECT old"}},
            ],
            "edges": [], "variables": {},
        }

        with patch("citra_llm.oss.llm_call", side_effect=_spy):
            resp = http_client.post(
                "/api/workflows/edit-node",
                json={"prompt": "change SELECT old to SELECT new",
                      "node_id": "s1", "workflow": workflow},
            )
        assert resp.status_code == 200, resp.text

        # The LLM gets the full workflow + the focused node + the prompt
        assert "Focused node id: s1" in observed["user_prompt"]
        assert "SELECT old" in observed["user_prompt"]
        assert "change SELECT old to SELECT new" in observed["user_prompt"]
