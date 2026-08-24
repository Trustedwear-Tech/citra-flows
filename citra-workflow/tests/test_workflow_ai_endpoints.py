# Copyright (c) 2026 Trustedwear Tech Private Limited (https://citra-ai.com)
# Author: Rohit Kumar Chandan
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may not
# use this file except in compliance with the License. You may obtain a copy of
# the License at http://www.apache.org/licenses/LICENSE-2.0

"""Smoke tests for the AI workflow router endpoints — exercises the
diff computation and the deterministic glue around the LLM call. The
LLM itself is mocked."""

import sys
import os
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from citra_workflow.router import (
    _diff_workflows, _node_significantly_different, _edge_key,
)


class TestDiffWorkflows:
    """The diff produced by refine() must let the UI apply patches
    without wiping a user's manual edits between AI turns."""

    def test_pure_addition(self):
        before = {"nodes": [{"id": "a", "type": "manual_trigger", "config": {}}], "edges": []}
        after = {
            "nodes": [
                {"id": "a", "type": "manual_trigger", "config": {}},
                {"id": "b", "type": "sql_source", "config": {"query": "SELECT 1"}},
            ],
            "edges": [{"id": "e1", "source": "a", "target": "b"}],
        }
        diff = _diff_workflows(before, after)
        assert len(diff["nodes_added"]) == 1
        assert diff["nodes_added"][0]["id"] == "b"
        assert diff["nodes_removed"] == []
        assert diff["nodes_updated"] == []
        assert len(diff["edges_added"]) == 1

    def test_pure_removal(self):
        before = {
            "nodes": [
                {"id": "a", "type": "manual_trigger", "config": {}},
                {"id": "b", "type": "sql_source", "config": {}},
            ],
            "edges": [{"id": "e1", "source": "a", "target": "b"}],
        }
        after = {"nodes": [{"id": "a", "type": "manual_trigger", "config": {}}], "edges": []}
        diff = _diff_workflows(before, after)
        assert diff["nodes_added"] == []
        assert diff["nodes_removed"] == ["b"]
        assert diff["edges_removed"] == ["e1"]

    def test_config_update(self):
        before = {"nodes": [{"id": "s", "type": "sql_source", "config": {"query": "SELECT 1"}}],
                  "edges": []}
        after = {"nodes": [{"id": "s", "type": "sql_source", "config": {"query": "SELECT 2"}}],
                 "edges": []}
        diff = _diff_workflows(before, after)
        assert diff["nodes_updated"] == [
            {"id": "s", "type": "sql_source", "config": {"query": "SELECT 2"}},
        ]
        assert diff["nodes_added"] == []
        assert diff["nodes_removed"] == []

    def test_no_change_yields_empty_diff(self):
        wf = {"nodes": [{"id": "a", "type": "manual_trigger", "config": {}}],
              "edges": [{"id": "e1", "source": "a", "target": "a"}], "variables": {}}
        diff = _diff_workflows(wf, wf)
        assert diff["nodes_added"] == []
        assert diff["nodes_removed"] == []
        assert diff["nodes_updated"] == []
        assert diff["edges_added"] == []
        assert diff["edges_removed"] == []

    def test_node_difference_detector(self):
        assert not _node_significantly_different(
            {"id": "a", "type": "t", "label": "L", "config": {}},
            {"id": "a", "type": "t", "label": "L", "config": {}},
        )
        assert _node_significantly_different(
            {"id": "a", "type": "t", "label": "L", "config": {}},
            {"id": "a", "type": "t2", "label": "L", "config": {}},
        )
        assert _node_significantly_different(
            {"id": "a", "type": "t", "label": "L", "config": {"k": 1}},
            {"id": "a", "type": "t", "label": "L", "config": {"k": 2}},
        )

    def test_edge_key_falls_back_on_missing_id(self):
        # Edges without an id should still be diffable
        before = {"nodes": [], "edges": [{"source": "a", "target": "b"}]}
        after = {"nodes": [], "edges": []}
        diff = _diff_workflows(before, after)
        assert len(diff["edges_removed"]) == 1
        assert "a→b" in diff["edges_removed"][0]


class TestDiffReviewEnrichment:
    """Human-review keys on top of the applier patch: field-level
    before→after per updated node, and split variables. Additive only —
    the applier keys above must stay byte-identical."""

    def test_field_level_changes_with_dot_paths(self):
        before = {"nodes": [{"id": "s", "type": "data_transform", "label": "Sort",
                             "config": {"operation": "sort",
                                        "params": {"column": "date", "ascending": True}}}],
                  "edges": []}
        after = {"nodes": [{"id": "s", "type": "data_transform", "label": "Sort",
                            "config": {"operation": "sort",
                                       "params": {"column": "score", "ascending": False}}}],
                 "edges": []}
        diff = _diff_workflows(before, after)
        changes = {c["path"]: c for c in diff["nodes_changed_fields"]["s"]}
        assert changes["config.params.column"]["before"] == "date"
        assert changes["config.params.column"]["after"] == "score"
        assert changes["config.params.ascending"]["before"] is True
        assert changes["config.params.ascending"]["after"] is False
        # Unchanged fields don't appear.
        assert "config.operation" not in changes

    def test_added_and_removed_config_keys(self):
        before = {"nodes": [{"id": "n", "type": "t", "config": {"old_key": 1}}], "edges": []}
        after = {"nodes": [{"id": "n", "type": "t", "config": {"new_key": 2}}], "edges": []}
        diff = _diff_workflows(before, after)
        changes = {c["path"]: c for c in diff["nodes_changed_fields"]["n"]}
        assert changes["config.old_key"] == {"path": "config.old_key", "before": 1, "after": None}
        assert changes["config.new_key"] == {"path": "config.new_key", "before": None, "after": 2}

    def test_label_and_type_changes_reported(self):
        before = {"nodes": [{"id": "n", "type": "summarizer", "label": "Old", "config": {}}],
                  "edges": []}
        after = {"nodes": [{"id": "n", "type": "llm_processor", "label": "New", "config": {}}],
                 "edges": []}
        diff = _diff_workflows(before, after)
        paths = {c["path"] for c in diff["nodes_changed_fields"]["n"]}
        assert paths == {"type", "label"}

    def test_long_string_values_truncated_for_review(self):
        big = "x" * 5000
        before = {"nodes": [{"id": "c", "type": "code_block", "config": {"code": "result = 1"}}],
                  "edges": []}
        after = {"nodes": [{"id": "c", "type": "code_block", "config": {"code": big}}],
                 "edges": []}
        diff = _diff_workflows(before, after)
        entry = diff["nodes_changed_fields"]["c"][0]
        assert entry["path"] == "config.code"
        assert len(entry["after"]) < 400
        assert "chars)" in entry["after"]
        # The applier patch still carries the FULL node untouched.
        assert diff["nodes_updated"][0]["config"]["code"] == big

    def test_variables_split(self):
        before = {"nodes": [], "edges": [], "variables": {"keep": 1, "gone": 2, "changed": "a"}}
        after = {"nodes": [], "edges": [], "variables": {"keep": 1, "new": 3, "changed": "b"}}
        diff = _diff_workflows(before, after)
        assert diff["variables_added"] == {"new": 3}
        assert diff["variables_removed"] == {"gone": 2}
        assert diff["variables_changed"] == [{"name": "changed", "before": "a", "after": "b"}]
        # Replace-all key preserved for the applier.
        assert diff["variables"] == {"keep": 1, "new": 3, "changed": "b"}

    def test_applier_keys_unchanged_shape(self):
        before = {"nodes": [{"id": "s", "type": "sql_source", "config": {"query": "SELECT 1"}}],
                  "edges": []}
        after = {"nodes": [{"id": "s", "type": "sql_source", "config": {"query": "SELECT 2"}}],
                 "edges": []}
        diff = _diff_workflows(before, after)
        assert diff["nodes_updated"] == [
            {"id": "s", "type": "sql_source", "config": {"query": "SELECT 2"}},
        ]


class TestConversationFormatting:
    """Lock in the contract that the conversation history coming from
    the UI is rendered into a useful prompt string for the LLM.

    The UI enriches each assistant turn's content with bracketed
    summaries — these tests just confirm the backend formatter walks
    the conversation correctly."""

    def test_formats_alternating_roles(self):
        from citra_workflow.router import _format_conversation
        parts = _format_conversation([
            {"role": "user", "content": "build workflow X"},
            {"role": "assistant",
             "content": "Generated: **WfX**\n[produced full workflow: 4 nodes, 3 edges]"},
            {"role": "user", "content": "add an approval step"},
            {"role": "assistant",
             "content": "Updated: **WfX**\n[produced diff: +1 new · ~0 updated · −0 removed]\n[validation issues you must fix: a1:E_UNKNOWN_DATASET]"},
        ])
        assert len(parts) == 4
        assert parts[0].startswith("User: build")
        assert parts[1].startswith("Assistant: Generated")
        assert "[produced full workflow: 4 nodes, 3 edges]" in parts[1]
        assert "[validation issues you must fix: a1:E_UNKNOWN_DATASET]" in parts[3]

    def test_skips_unknown_roles(self):
        from citra_workflow.router import _format_conversation
        parts = _format_conversation([
            {"role": "system", "content": "ignored"},
            {"role": "user", "content": "real"},
        ])
        assert parts == ["User: real"]

    def test_empty_history(self):
        from citra_workflow.router import _format_conversation
        assert _format_conversation([]) == []
