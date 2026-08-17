# Copyright (c) 2026 Trustedwear Tech Private Limited (https://citra-ai.com)
# Author: Rohit Kumar Chandan
# SPDX-License-Identifier: BUSL-1.1
#
# Licensed under the Business Source License 1.1. Non-production use is granted;
# production use requires a commercial licence until the Change Date, after
# which this file converts to Apache-2.0. See LICENSE at the repository root.

"""
Unit tests for Pydantic models — enums, validation, defaults, serialization.
"""

import sys
import os
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from pydantic import ValidationError
from citra_workflow.models import (
    NodeType, NodeCategory, WorkflowStatus, ExecutionStatus, NodeExecutionStatus,
    NodeDefinition, EdgeDefinition, NodePosition, ScheduleConfig,
    WorkflowDefinition, WorkflowExecution, NodeExecutionResult,
    NodeFieldSchema, NodeSchema, ApprovalRequest,
    CreateWorkflowRequest, UpdateWorkflowRequest, DeployWorkflowRequest,
)


# ============================================================================
# Enum Coverage
# ============================================================================

class TestEnums:

    def test_node_type_count(self):
        """At least 35 node types registered."""
        assert len(NodeType) >= 35

    def test_node_category_values(self):
        # "dept_flow" was removed with the Citra dept-ingestion node category.
        expected = {"trigger", "source", "agent", "processor", "logic", "output"}
        assert {c.value for c in NodeCategory} == expected

    def test_workflow_status_values(self):
        assert set(WorkflowStatus) == {WorkflowStatus.DRAFT, WorkflowStatus.DEPLOYED, WorkflowStatus.PAUSED}

    def test_execution_status_has_waiting_approval(self):
        assert ExecutionStatus.WAITING_APPROVAL.value == "waiting_approval"

    def test_node_execution_status_has_skipped(self):
        assert NodeExecutionStatus.SKIPPED.value == "skipped"


# ============================================================================
# NodeDefinition
# ============================================================================

class TestNodeDefinition:

    def test_defaults(self):
        nd = NodeDefinition(type=NodeType.LLM_PROCESSOR)
        assert nd.id  # auto-generated
        assert nd.label == ""
        assert nd.config == {}
        assert nd.position.x == 0 and nd.position.y == 0

    def test_invalid_type_rejected(self):
        with pytest.raises(ValidationError):
            NodeDefinition(type="totally_fake_node")

    def test_config_preserved(self):
        nd = NodeDefinition(type=NodeType.CONDITION, config={"field": "status", "value": "active"})
        assert nd.config["field"] == "status"


# ============================================================================
# EdgeDefinition
# ============================================================================

class TestEdgeDefinition:

    def test_required_fields(self):
        with pytest.raises(ValidationError):
            EdgeDefinition(source="a")  # missing target

    def test_optional_handles(self):
        ed = EdgeDefinition(source="a", target="b", source_handle="true")
        assert ed.source_handle == "true"
        assert ed.target_handle is None


# ============================================================================
# WorkflowDefinition
# ============================================================================

class TestWorkflowDefinition:

    def test_defaults(self):
        wd = WorkflowDefinition(user_id="u1", name="My Workflow")
        assert wd.workflow_id  # auto-generated
        assert wd.status == WorkflowStatus.DRAFT
        assert wd.version == 1
        assert wd.nodes == []
        assert wd.edges == []
        assert wd.is_active is True

    def test_schedule_config_defaults(self):
        wd = WorkflowDefinition(user_id="u1", name="test")
        assert wd.schedule.enabled is False
        # Default timezone matches the WorkflowSchedule model default.
        assert wd.schedule.timezone == "America/New_York"

    def test_roundtrip_serialization(self):
        wd = WorkflowDefinition(
            user_id="u1",
            name="Test",
            nodes=[NodeDefinition(type=NodeType.MANUAL_TRIGGER)],
            edges=[],
        )
        data = wd.model_dump()
        restored = WorkflowDefinition(**data)
        assert restored.name == "Test"
        assert restored.nodes[0].type == NodeType.MANUAL_TRIGGER


# ============================================================================
# WorkflowExecution
# ============================================================================

class TestWorkflowExecution:

    def test_defaults(self):
        we = WorkflowExecution(workflow_id="w1", user_id="u1")
        assert we.status == ExecutionStatus.PENDING
        assert we.trigger == "manual"
        assert we.node_results == {}

    def test_node_result_embedded(self):
        nr = NodeExecutionResult(
            node_id="n1",
            status=NodeExecutionStatus.COMPLETED,
            output_data={"score": 95},
            duration_ms=200,
        )
        we = WorkflowExecution(
            workflow_id="w1", user_id="u1",
            node_results={"n1": nr},
        )
        assert we.node_results["n1"].output_data == {"score": 95}


# ============================================================================
# NodeFieldSchema — including visible_when
# ============================================================================

class TestNodeFieldSchema:

    def test_basic_field(self):
        f = NodeFieldSchema(name="prompt", label="Prompt", type="textarea", required=True)
        assert f.required is True
        assert f.visible_when is None

    def test_visible_when_serialization(self):
        f = NodeFieldSchema(
            name="batch_size", label="Batch Size", type="number",
            visible_when={"field": "processing_mode", "value": "batch"},
        )
        data = f.model_dump()
        assert data["visible_when"]["field"] == "processing_mode"
        assert data["visible_when"]["value"] == "batch"


# ============================================================================
# Approval Model
# ============================================================================

class TestApprovalRequest:

    def test_defaults(self):
        ar = ApprovalRequest(
            execution_id="e1", workflow_id="w1", user_id="u1", node_id="n1",
        )
        assert ar.approval_id  # auto-generated
        assert ar.timeout_hours == 24
        assert ar.notification_sent is False
        assert ar.resolution is None


# ============================================================================
# API Request Models
# ============================================================================

class TestAPIRequestModels:

    def test_create_workflow_minimal(self):
        req = CreateWorkflowRequest(name="Test")
        assert req.description == ""
        assert req.nodes == []

    def test_update_workflow_all_optional(self):
        req = UpdateWorkflowRequest()
        assert req.name is None
        assert req.nodes is None

    def test_deploy_action_literal(self):
        req = DeployWorkflowRequest(action="deploy")
        assert req.action == "deploy"
        with pytest.raises(ValidationError):
            DeployWorkflowRequest(action="restart")
