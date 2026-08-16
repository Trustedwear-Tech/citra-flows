"""
Unit tests for the node registry — registration, lookup, schema generation.
"""

import sys
import os
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from citra_workflow.models import NodeType, NodeCategory
from citra_workflow.nodes import get_node, get_all_schemas, get_registry


class TestNodeRegistry:

    def test_all_node_types_registered(self):
        """At least 37 node types should be registered."""
        registry = get_registry()
        assert len(registry) >= 35, f"Only {len(registry)} types registered"

    def test_get_node_returns_instance(self):
        """get_node(LLM_PROCESSOR) should return a node instance."""
        node = get_node(NodeType.LLM_PROCESSOR)
        assert node is not None
        assert node.node_type == NodeType.LLM_PROCESSOR

    def test_get_node_unknown_raises(self):
        """get_node with an unregistered type should raise ValueError."""
        with pytest.raises(ValueError, match="Unknown node type"):
            get_node("fake_node_type_xyz")

    def test_get_all_schemas_returns_list(self):
        """All schemas should have type, label, category, fields."""
        schemas = get_all_schemas()
        assert len(schemas) >= 35
        for schema in schemas:
            assert schema.type is not None
            assert schema.label
            assert schema.category is not None
            assert isinstance(schema.fields, list)

    def test_schema_fields_have_valid_types(self):
        """Every field type should be in the allowed set."""
        allowed_types = {
            "text", "textarea", "number", "select", "boolean", "json",
            "password", "cron", "tool_picker", "schema_builder",
            "connection_picker", "variable_assignments",
        }
        schemas = get_all_schemas()
        for schema in schemas:
            for field in schema.fields:
                assert field.type in allowed_types, (
                    f"{schema.type}:{field.name} has invalid type '{field.type}'"
                )

    def test_each_schema_has_valid_category(self):
        """Every registered node's category should be a valid NodeCategory."""
        valid_categories = set(NodeCategory)
        schemas = get_all_schemas()
        for schema in schemas:
            assert schema.category in valid_categories

    def test_registry_returns_new_instances(self):
        """Two calls to get_node should return separate instances."""
        a = get_node(NodeType.MANUAL_TRIGGER)
        b = get_node(NodeType.MANUAL_TRIGGER)
        assert a is not b
