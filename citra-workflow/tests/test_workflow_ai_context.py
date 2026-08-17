# Copyright (c) 2026 Trustedwear Tech Private Limited (https://citra-ai.com)
# Author: Rohit Kumar Chandan
# SPDX-License-Identifier: BUSL-1.1
#
# Licensed under the Business Source License 1.1. Non-production use is granted;
# production use requires a commercial licence until the Change Date, after
# which this file converts to Apache-2.0. See LICENSE at the repository root.

"""Tests for the AI authoring context: node palette, connections, and the
reference validator that gates AI output.

These tests don't hit the LLM — they exercise the deterministic context
gathering and validation paths.

The dept-MCP catalogue and MCP-sources sections these also covered were
removed with the Citra platform coupling; their render/fetch functions no
longer exist."""

import sys
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from citra_workflow import ai_context
from citra_workflow.ai_context import (
    render_node_palette_section,
    render_connections_section,
    fetch_connections_for_ai,
    gather_ai_context,
    render_ai_context_sections,
)
from citra_workflow.workflow_reference_validator import (
    validate_workflow_references,
    errors_to_suggestions,
    detect_setup_gaps,
)


# ── Node palette section (Phase 6) ───────────────────────────────────


class TestNodePaletteSection:

    def test_palette_includes_registered_nodes(self):
        out = render_node_palette_section()
        # Known visible nodes from the registry
        assert "manual_trigger" in out
        assert "scheduled_trigger" in out
        assert "human_approval" in out

    def test_palette_offers_the_generic_data_nodes(self):
        """The AI must be able to reach the replacements for the removed
        Citra-coupled nodes: any MCP server, any vector DB, embedding and
        reranking."""
        out = render_node_palette_section()
        for node_type in ("mcp_server", "vector_search", "vector_embed", "reranker"):
            assert f"- {node_type} (" in out, f"{node_type} missing from AI palette"

    def test_palette_never_offers_removed_citra_nodes(self):
        """Regression guard. The palette is built from the live registry, but
        node DESCRIPTIONS and ai_authoring_hints are free text — a removed node
        can keep leaking to the AI builder through another node's prose long
        after its class is gone. That is exactly what happened with
        `dept_mcp_action`, which survived in webhook_output's description and in
        ai_agent's authoring hint."""
        out = render_node_palette_section()
        for gone in ("dept_mcp_source", "dept_mcp_action", "chunk_embed",
                     "vector_sink", "structured_schema_sink", "catalogue_sink",
                     "mongo_writer", "smart_app"):
            assert gone not in out, f"removed node '{gone}' is still advertised to the AI builder"

    def test_palette_includes_ai_authoring_hints(self):
        out = render_node_palette_section()
        assert "AI HINT:" in out


# ── Connections section (Phase 1) ────────────────────────────────────


class TestConnectionsSection:

    def test_empty_connections_section(self):
        out = render_connections_section([])
        assert "(none yet" in out
        assert "register the connection" in out

    def test_renders_connections(self):
        out = render_connections_section([
            {"id": "sql_prod", "type": "sql", "name": "Prod Postgres",
             "description": "Customer orders database"},
            {"id": "s3_invoices", "type": "bucket", "name": "Invoices",
             "description": ""},
        ])
        assert "sql_prod" in out
        assert "Prod Postgres" in out
        assert "Customer orders database" in out
        assert "s3_invoices" in out

    @pytest.mark.asyncio
    async def test_fetch_connections_for_ai_empty_org(self):
        out = await fetch_connections_for_ai("")
        assert out == []

    @pytest.mark.asyncio
    async def test_fetch_connections_for_ai_returns_trimmed(self):
        """Mock the mongo client and verify the returned shape strips secrets
        AND that the query is ORG-scoped (shared across the IT team), not
        siloed by user_id."""
        cursor = MagicMock()
        cursor.sort = MagicMock(return_value=cursor)

        async def _aiter(self):
            for doc in [
                {"connection_id": "sql1", "name": "SQL A", "type": "sql",
                 "description": "primary"},
                {"connection_id": "api1", "name": "API B", "type": "api",
                 "description": ""},
            ]:
                yield doc
        cursor.__aiter__ = _aiter

        col = MagicMock()
        col.find = MagicMock(return_value=cursor)
        db = MagicMock()
        db.__getitem__ = MagicMock(return_value=col)
        client = MagicMock()
        client.__getitem__ = MagicMock(return_value=db)

        with patch("citra_mongo.get_async_mongo_client", return_value=client), \
             patch("citra_mongo.MONGODB_DATABASE", "testdb"):
            result = await fetch_connections_for_ai("acme")
        assert len(result) == 2
        assert result[0] == {
            "id": "sql1", "type": "sql", "name": "SQL A", "description": "primary",
        }
        # The filter must be org-scoped, and the projection must exclude the
        # encrypted test/prod secret blobs.
        filter_arg, projection_arg = col.find.call_args[0]
        assert filter_arg == {"org_id": "acme"}
        assert "test" not in projection_arg and "prod" not in projection_arg


# ── Reference validator (Phase 2) ────────────────────────────────────


class TestReferenceValidator:

    def _wf(self, *nodes):
        return {"nodes": list(nodes), "edges": [], "variables": {}}

    def test_unknown_connection_id_flagged(self):
        wf = self._wf(
            {"id": "s1", "type": "sql_source", "config": {
                "connection_id": "phantom_sql", "query": "SELECT 1",
            }},
        )
        errors = validate_workflow_references(
            workflow=wf, connections=[],
        )
        assert any(e["code"] == "E_UNKNOWN_CONNECTION" for e in errors)
        suggestions = errors_to_suggestions(errors)
        assert any("phantom_sql" in s for s in suggestions)

    def test_connection_type_mismatch_flagged(self):
        wf = self._wf(
            {"id": "s1", "type": "sql_source", "config": {
                "connection_id": "wrong_type_conn",
            }},
        )
        connections = [{"id": "wrong_type_conn", "type": "api", "name": "n/a"}]
        errors = validate_workflow_references(
            workflow=wf, connections=connections,
        )
        assert any(e["code"] == "E_CONNECTION_TYPE_MISMATCH" for e in errors)

    def test_unknown_node_type_flagged(self):
        wf = self._wf({"id": "x", "type": "not_a_real_type", "config": {}})
        errors = validate_workflow_references(
            workflow=wf, connections=[],
        )
        assert any(e["code"] == "E_UNKNOWN_NODE_TYPE" for e in errors)


class TestDetectSetupGaps:
    def _wf(self, config):
        return {"nodes": [
            {"id": "t1", "type": "manual_trigger", "config": {}},
            {"id": "s1", "type": "sql_source", "label": "Query", "config": config},
        ]}

    def test_empty_connection_no_inline_is_a_gap(self):
        gaps = detect_setup_gaps(self._wf({"connection_id": "", "query": "SELECT 1"}), [])
        assert len(gaps) == 1
        assert gaps[0]["node_id"] == "s1"
        assert gaps[0]["connection_type"] == "sql"

    def test_inline_connection_string_is_not_a_gap(self):
        gaps = detect_setup_gaps(
            self._wf({"connection_id": "", "connection_string": "postgresql://h/db"}), [])
        assert gaps == []

    def test_selected_connection_is_not_a_gap(self):
        gaps = detect_setup_gaps(self._wf({"connection_id": "picked"}), [])
        assert gaps == []

    def test_message_points_to_existing_connection_when_org_has_matching_type(self):
        """When the org already has a connection of the right type, the gap
        message tells the user to PICK an existing one, not just register."""
        gaps = detect_setup_gaps(
            self._wf({"connection_id": "", "query": "SELECT 1"}),
            [{"id": "c1", "type": "sql", "name": "Finance SQL"}],
        )
        assert len(gaps) == 1
        assert "existing sql connection" in gaps[0]["message"].lower()

    def test_message_says_register_when_no_matching_type(self):
        gaps = detect_setup_gaps(
            self._wf({"connection_id": "", "query": "SELECT 1"}),
            [{"id": "c1", "type": "api", "name": "Some API"}],  # wrong type
        )
        assert len(gaps) == 1
        assert "register one" in gaps[0]["message"].lower()
