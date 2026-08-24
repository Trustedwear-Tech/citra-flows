# Copyright (c) 2026 Trustedwear Tech Private Limited (https://citra-ai.com)
# Author: Rohit Kumar Chandan
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may not
# use this file except in compliance with the License. You may obtain a copy of
# the License at http://www.apache.org/licenses/LICENSE-2.0

"""Bug 006 — Condition node field resolution + branch gating.

(a) A bare field name ("score") silently evaluated false because node outputs are
    shaped {"items": [ {...} ], "meta": {...}} and only the full path
    ("items.0.score") resolved. Fix: fall back to items[0].
(b) When the condition was true, BOTH downstream branches ran; when false, NEITHER
    ran — because the condition's outgoing edges had no source_handle (defaulted to
    "true"). Fix: derive each edge's branch from source_handle OR label, and gate
    accordingly.
"""
from __future__ import annotations

import pytest

from citra_workflow.nodes import NodeContext, get_node
from citra_workflow.models import NodeType, EdgeDefinition
from citra_workflow.executor import _normalize_branch_token, _edge_condition_branch


# ── (a) field resolution ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_bare_field_resolves_against_first_item():
    """THE BUG 006a CASE: field='score' on {items:[{score:72}]} → 72 > 50 → True."""
    node = get_node(NodeType.CONDITION)
    ctx = NodeContext(
        node_id="c1",
        node_config={"field": "score", "operator": ">", "value": "50"},
        input_data={"items": [{"score": 72}], "meta": {"count": 1}},
    )
    result = await node.execute(ctx)
    assert result["meta"]["condition_result"] is True
    assert result["meta"]["branch"] == "true"


@pytest.mark.asyncio
async def test_full_dotted_path_still_works():
    node = get_node(NodeType.CONDITION)
    ctx = NodeContext(
        node_id="c1",
        node_config={"field": "items.0.score", "operator": ">", "value": "50"},
        input_data={"items": [{"score": 72}], "meta": {}},
    )
    result = await node.execute(ctx)
    assert result["meta"]["condition_result"] is True


@pytest.mark.asyncio
async def test_bare_field_false_when_value_not_greater():
    node = get_node(NodeType.CONDITION)
    ctx = NodeContext(
        node_id="c1",
        node_config={"field": "score", "operator": ">", "value": "50"},
        input_data={"items": [{"score": 12}], "meta": {}},
    )
    result = await node.execute(ctx)
    assert result["meta"]["condition_result"] is False
    assert result["meta"]["branch"] == "false"


@pytest.mark.asyncio
async def test_top_level_field_still_preferred():
    """A top-level field on the envelope must still resolve directly."""
    node = get_node(NodeType.CONDITION)
    ctx = NodeContext(
        node_id="c1",
        node_config={"field": "status", "operator": "==", "value": "active"},
        input_data={"status": "active", "items": [{"status": "other"}]},
    )
    result = await node.execute(ctx)
    assert result["meta"]["condition_result"] is True


@pytest.mark.asyncio
async def test_unresolved_field_logs_warning(caplog):
    import logging
    node = get_node(NodeType.CONDITION)
    ctx = NodeContext(
        node_id="c1",
        node_config={"field": "nope", "operator": ">", "value": "50"},
        input_data={"items": [{"score": 72}], "meta": {}},
    )
    with caplog.at_level(logging.WARNING):
        result = await node.execute(ctx)
    assert result["meta"]["condition_result"] is False
    assert any("did not resolve" in r.message for r in caplog.records)


# ── (b) branch normalization + gating ───────────────────────────────────────

def test_normalize_branch_token():
    assert _normalize_branch_token("true") == "true"
    assert _normalize_branch_token("false") == "false"
    assert _normalize_branch_token("passed") == "true"
    assert _normalize_branch_token("failed") == "false"
    assert _normalize_branch_token("Met") == "true"
    assert _normalize_branch_token("Not Met") == "false"
    assert _normalize_branch_token("yes") == "true"
    assert _normalize_branch_token("no") == "false"
    assert _normalize_branch_token("") is None
    assert _normalize_branch_token(None) is None
    assert _normalize_branch_token("banana") is None


def test_edge_branch_from_handle_then_label():
    # source_handle wins
    e = EdgeDefinition(source="c1", target="d1", source_handle="false", label="passed")
    assert _edge_condition_branch(e) == "false"
    # falls back to label when handle missing
    e2 = EdgeDefinition(source="c1", target="d1", source_handle=None, label="failed")
    assert _edge_condition_branch(e2) == "false"
    e3 = EdgeDefinition(source="c1", target="d2", source_handle=None, label="passed")
    assert _edge_condition_branch(e3) == "true"


def _gate_skip(edges_to_target, taken_branch):
    """Replicates the executor's gating decision for one target."""
    edge_branches = [_edge_condition_branch(e) for e in edges_to_target]
    known = [b for b in edge_branches if b is not None]
    return bool(known and taken_branch not in known)


def test_gating_true_runs_only_passed_branch():
    """Bug 006b: with labelled edges, true → pass runs, fail skipped."""
    pass_edge = EdgeDefinition(source="c1", target="dl_pass", label="passed")
    fail_edge = EdgeDefinition(source="c1", target="dl_fail", label="failed")
    assert _gate_skip([pass_edge], "true") is False   # pass branch runs
    assert _gate_skip([fail_edge], "true") is True    # fail branch skipped


def test_gating_false_runs_only_failed_branch():
    pass_edge = EdgeDefinition(source="c1", target="dl_pass", label="passed")
    fail_edge = EdgeDefinition(source="c1", target="dl_fail", label="failed")
    assert _gate_skip([pass_edge], "false") is True   # pass branch skipped
    assert _gate_skip([fail_edge], "false") is False  # fail branch runs


def test_gating_unlabelled_edges_fall_through():
    """No branch info anywhere → don't gate (legacy pass-through, no regression)."""
    e = EdgeDefinition(source="c1", target="d1", source_handle=None, label=None)
    assert _gate_skip([e], "true") is False
    assert _gate_skip([e], "false") is False
