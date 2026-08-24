# Copyright (c) 2026 Trustedwear Tech Private Limited (https://citra-ai.com)
# Author: Rohit Kumar Chandan
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may not
# use this file except in compliance with the License. You may obtain a copy of
# the License at http://www.apache.org/licenses/LICENSE-2.0

"""
Unit tests for BaseNode — _extract_items, run wrapper, validate_config.
"""

import sys
import os
import pytest
from unittest.mock import AsyncMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from citra_workflow.nodes import BaseNode, NodeContext
from citra_workflow.models import NodeType, NodeCategory, NodeFieldSchema, NodeExecutionStatus


# ============================================================================
# Concrete test node for testing BaseNode methods
# ============================================================================

class _TestNode(BaseNode):
    node_type = NodeType.MANUAL_TRIGGER
    category = NodeCategory.TRIGGER
    label = "Test Node"
    description = "For unit tests"

    def __init__(self, execute_fn=None):
        self._execute_fn = execute_fn

    async def execute(self, ctx: NodeContext):
        if self._execute_fn:
            return self._execute_fn(ctx)
        return {"ok": True}

    @classmethod
    def get_fields(cls):
        return [
            NodeFieldSchema(name="name", label="Name", type="text", required=True),
            NodeFieldSchema(name="count", label="Count", type="number", required=False),
        ]


# ============================================================================
# _extract_items tests
# ============================================================================

class TestExtractItems:

    def test_from_items_key(self):
        assert BaseNode._extract_items({"items": [1, 2]}) == [1, 2]

    def test_from_records_key(self):
        assert BaseNode._extract_items({"records": [{"a": 1}]}) == [{"a": 1}]

    def test_from_plain_list(self):
        assert BaseNode._extract_items([1, 2, 3]) == [1, 2, 3]

    def test_from_single_dict(self):
        result = BaseNode._extract_items({"name": "x"})
        assert result == [{"name": "x"}]

    def test_from_none(self):
        assert BaseNode._extract_items(None) == []

    def test_from_string(self):
        assert BaseNode._extract_items("hello") == ["hello"]

    def test_items_key_takes_precedence(self):
        """If dict has 'items' key with a list, it should be extracted."""
        data = {"items": [1, 2], "records": [3, 4], "other": "stuff"}
        assert BaseNode._extract_items(data) == [1, 2]

    def test_records_key_when_no_items(self):
        """If dict has 'records' but no 'items' key, use records."""
        data = {"records": [{"x": 1}], "other": "stuff"}
        assert BaseNode._extract_items(data) == [{"x": 1}]

    def test_empty_list(self):
        assert BaseNode._extract_items([]) == []

    def test_nested_list_not_flattened(self):
        """Nested lists should not be flattened."""
        data = [[1, 2], [3, 4]]
        assert BaseNode._extract_items(data) == [[1, 2], [3, 4]]


# ============================================================================
# run() wrapper tests
# ============================================================================

class TestRunWrapper:

    @pytest.mark.asyncio
    async def test_run_success_with_timing(self):
        """Successful execute() → COMPLETED result with duration_ms > 0."""
        node = _TestNode(execute_fn=lambda ctx: {"result": "done"})
        ctx = NodeContext(node_id="n1", node_config={}, input_data={})

        result = await node.run(ctx)

        assert result.status == NodeExecutionStatus.COMPLETED
        assert result.output_data == {"result": "done"}
        assert result.duration_ms >= 0
        assert result.error is None
        assert result.node_id == "n1"

    @pytest.mark.asyncio
    async def test_run_failure_with_error(self):
        """execute() raising Exception → FAILED result with error message."""
        def _fail(ctx):
            raise RuntimeError("Something broke")

        node = _TestNode(execute_fn=_fail)
        ctx = NodeContext(node_id="n2", node_config={}, input_data={})

        result = await node.run(ctx)

        assert result.status == NodeExecutionStatus.FAILED
        assert "Something broke" in result.error
        assert result.duration_ms >= 0

    @pytest.mark.asyncio
    async def test_run_returns_none_output(self):
        """execute() returning None → COMPLETED with output_data=None."""
        node = _TestNode(execute_fn=lambda ctx: None)
        ctx = NodeContext(node_id="n3", node_config={}, input_data={})

        result = await node.run(ctx)

        assert result.status == NodeExecutionStatus.COMPLETED
        assert result.output_data is None


# ============================================================================
# validate_config tests
# ============================================================================

class TestValidateConfig:

    def test_missing_required_field(self):
        """Missing required field → errors list has entry."""
        node = _TestNode()
        errors = node.validate_config({"count": 5})
        assert len(errors) == 1
        assert "Name" in errors[0]

    def test_all_present(self):
        """All fields present → empty errors list."""
        node = _TestNode()
        errors = node.validate_config({"name": "test", "count": 5})
        assert errors == []

    def test_optional_field_missing_ok(self):
        """Optional field missing → no error."""
        node = _TestNode()
        errors = node.validate_config({"name": "test"})
        assert errors == []
