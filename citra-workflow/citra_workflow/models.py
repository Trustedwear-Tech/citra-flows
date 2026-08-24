# Copyright (c) 2026 Trustedwear Tech Private Limited (https://citra-ai.com)
# Author: Rohit Kumar Chandan
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may not
# use this file except in compliance with the License. You may obtain a copy of
# the License at http://www.apache.org/licenses/LICENSE-2.0

"""
Workflow Engine Data Models
===========================
Pydantic models for workflow definitions, execution state, and API contracts.
"""

from __future__ import annotations
from pydantic import BaseModel, Field, field_validator
from typing import Optional, List, Dict, Any, Literal
from datetime import datetime
from enum import Enum
import uuid

from .config import (
    DEFAULT_APPROVAL_TIMEOUT_HOURS, MAX_WORKFLOW_NODES, MAX_WORKFLOW_EDGES,
)


# ============================================================================
# Enums
# ============================================================================

class NodeType(str, Enum):
    # Triggers / Entry
    MANUAL_TRIGGER = "manual_trigger"
    SCHEDULED_TRIGGER = "scheduled_trigger"
    WEBHOOK_TRIGGER = "webhook_trigger"
    START_NODE = "start_node"
    
    # Data Sources
    SQL_SOURCE = "sql_source"
    MONGO_SOURCE = "mongo_source"
    CSV_SOURCE = "csv_source"
    API_SOURCE = "api_source"
    VECTOR_SEARCH = "vector_search"      # any vector DB -> matching items
    MCP_SERVER = "mcp_server"            # any standards-compliant MCP server
    VECTOR_EMBED = "vector_embed"        # text field -> vector (ingestion side)
    RERANKER = "reranker"                # cross-encoder re-scoring of candidates
    S3_SOURCE = "s3_source"
    SFTP_SOURCE = "sftp_source"
    FTP_FOLDER_SOURCE = "ftp_folder_source"
    SFTP_FOLDER_SOURCE = "sftp_folder_source"
    S3_FOLDER_SOURCE = "s3_folder_source"
    FILE_FETCH = "file_fetch"   # download a URL into a binary blob (image/audio/file)
    
    # AI Agents
    AI_AGENT = "ai_agent"
    
    # Processors
    LLM_PROCESSOR = "llm_processor"
    CUSTOM_LLM = "custom_llm"
    RULES_ENGINE = "rules_engine"
    DATA_TRANSFORM = "data_transform"
    CLASSIFIER = "classifier"
    EXTRACTOR = "extractor"
    SUMMARIZER = "summarizer"
    VALIDATOR = "validator"
    DEDUPLICATOR = "deduplicator"
    MERGE_DATA = "merge_data"
    CODE_BLOCK = "code_block"
    # Multimodal processors — consume binary blobs (image/audio), emit text.
    OCR = "ocr"
    AUDIO_TRANSCRIBE = "audio_transcribe"
    VISION_LLM = "vision_llm"
    
    # Logic
    CONDITION = "condition"
    SWITCH_ROUTER = "switch_router"
    LOOP = "loop"
    PARALLEL_SPLIT = "parallel_split"
    MERGE_WAIT = "merge_wait"
    HUMAN_APPROVAL = "human_approval"
    DELAY = "delay"
    SET_VARIABLE = "set_variable"
    
    # Outputs
    SQL_WRITER = "sql_writer"
    PDF_EXPORT = "pdf_export"
    EXCEL_EXPORT = "excel_export"
    CSV_EXPORT = "csv_export"
    EMAIL_SENDER = "email_sender"
    BUCKET_WRITER = "bucket_writer"
    SFTP_WRITER = "sftp_writer"
    WEBHOOK_OUTPUT = "webhook_output"
    FILE_DOWNLOAD = "file_download"
    NOTIFY = "notify"   # Slack / Teams / generic incoming-webhook message

    WORKFLOW_STATE_GET = "workflow_state_get"              # read a per-workflow watermark (e.g. last_run_at)
    WORKFLOW_STATE_SET = "workflow_state_set"              # write the watermark at end of run


class NodeCategory(str, Enum):
    TRIGGER = "trigger"
    SOURCE = "source"
    AGENT = "agent"
    PROCESSOR = "processor"
    LOGIC = "logic"
    OUTPUT = "output"


class WorkflowStatus(str, Enum):
    DRAFT = "draft"
    DEPLOYED = "deployed"
    PAUSED = "paused"  # Temporarily disabled


class ExecutionStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
    WAITING_APPROVAL = "waiting_approval"
    PAUSED = "paused"
    # Transient state written by the approve/reject endpoint between a paused
    # execution and the worker picking up the workflow.resume job. Must be a
    # valid enum member — the worker deserialises the execution doc via
    # WorkflowExecution(**doc) before resuming, and an unknown status value
    # made every human-approval resume fail validation (execution stuck).
    RESUMING = "resuming"


class NodeExecutionStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"
    WAITING = "waiting"


# ============================================================================
# Node Configuration Models
# ============================================================================

class NodePosition(BaseModel):
    x: float = 0
    y: float = 0


class NodeDefinition(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4())[:8])
    type: NodeType
    label: str = ""
    position: NodePosition = Field(default_factory=NodePosition)
    config: Dict[str, Any] = Field(default_factory=dict)


class EdgeDefinition(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4())[:8])
    source: str
    target: str
    source_handle: Optional[str] = None
    target_handle: Optional[str] = None
    label: Optional[str] = None


class ScheduleConfig(BaseModel):
    enabled: bool = False
    cron_expression: Optional[str] = None  # e.g. "0 9 * * 1" = Monday 9 AM
    timezone: str = "America/New_York"


# ============================================================================
# Workflow Definition (stored in MongoDB)
# ============================================================================

class WorkflowVisibility(BaseModel):
    """Who can see / run / edit a workflow beyond the owning SA.

    Every workflow is OWNED by exactly one entity — a service_account
    (default), a dept, or an org. Visibility expands that to broader
    boundaries (dept-wide, org-wide, or public).
    """
    # Visibility levels after SA-only ownership:
    #   "sa"     — only admins/members of the owning SA (default)
    #   "dept"   — anyone in any of the workflow's dept_ids
    #   "org"    — anyone in the workflow's org
    #   "public" — no auth check on read
    read: Literal["sa", "dept", "org", "public"] = "sa"
    run: Literal["sa", "dept", "org", "service_account_only"] = "sa"
    edit: Literal["sa", "dept_admin", "org_admin", "super_admin"] = "sa"
    org_admin_override: bool = True


class WorkflowNotifications(BaseModel):
    """Failure-alert delivery for a workflow.

    ONLY the workflow owner (``author_email`` — auto-derived from the
    authenticated user, never free-text) is emailed on failure. There is
    intentionally no way to add arbitrary recipient addresses, so a failure
    alert can never be routed to an outside-org address. The failure is also
    logged at ERROR (Loki + GlitchTip) for IT to pick up.
    See docs/workflow-visibility-ownership.md.
    """
    # Master switch. When False no failure email is sent (the ERROR log still fires).
    notify_on_failure: bool = True


class OwnerHistoryEntry(BaseModel):
    """Audit entry pushed onto previous_owners when ownership changes."""
    owner_type: str
    owner_id: str
    changed_at: Optional[datetime] = None
    changed_by: Optional[str] = None
    reason: Optional[str] = None


class WorkflowDefinition(BaseModel):
    workflow_id: Optional[str] = Field(default_factory=lambda: str(uuid.uuid4()))

    # ── Identity ──
    name: str
    description: str = ""

    # ── Layer 1: AUTHOR (immutable audit truth) ──
    # The user who FIRST created this workflow. Never changes. Survives
    # every transfer / reassign / promotion. Used for audit + provenance
    # ("who originally wrote this?").
    author_user_id: str = ""
    author_email: str = ""
    author_at: Optional[datetime] = None

    # ── Layer 2: OWNER (mutable, current control) ──
    # Workflows are ALWAYS owned by an org. No user, SA, or dept owns a
    # workflow. Create paths force owner_type="org" and owner_id=org_id.
    # The Literal keeps the legacy values so unmigrated documents still
    # deserialise; migration normalises everything to "org" and the
    # transfer endpoints are disabled (410 Gone).
    owner_type: Literal["service_account", "dept", "org"] = "org"
    owner_id: str = ""
    # Legacy shadow field. Kept for backward-compat queries / migrations
    # but NOT used for authorisation. Auth always goes through the
    # owning SA's admins/members.
    user_id: str = ""
    # Audit trail of ownership transitions.
    owner_changed_at: Optional[datetime] = None
    owner_changed_by: Optional[str] = None
    previous_owners: List[OwnerHistoryEntry] = Field(default_factory=list)

    # ── Lifecycle stage ──
    #   personal      — owned by a Personal SA (single human admin)
    #   team_managed  — owned by a multi-member SA
    #   dept_managed  — owned by a dept
    #   org_managed   — owned by an org
    #   archived      — soft-deleted; restorable by admin
    # "draft" and "shared" are retained for back-compat but new docs
    # never enter those stages.
    lifecycle_stage: Literal[
        "draft", "personal", "shared", "team_managed",
        "dept_managed", "org_managed", "archived",
    ] = "org_managed"

    # ── Inheritance: what happens when the SA's sole admin is removed ──
    # Default: archive (safest). transfer_to_dept added in Phase E.
    inheritance_policy: Literal[
        "archive",
        "transfer_to_sa",
        "transfer_to_dept",
        "transfer_to_org",
        "delete_after_grace",
    ] = "archive"
    inheritance_target: Optional[str] = None
    inheritance_grace_days: int = 30

    # ── Org / dept scoping ──
    org_id: str = ""                              # required for all new workflows
    dept_ids: List[str] = Field(default_factory=list)

    # ── Category ──
    workflow_kind: Literal[
        "dept_data_flow",    # ingest enterprise data; dept_admin owned (or SA)
        "mcp_ingestion",     # demo/PoC ingestion; SA-owned
        "smart_app_action",  # tied to a SmartApp; runs under app owner's identity
        "ad_hoc",            # one-off user automation
    ] = "ad_hoc"
    linked_smart_app_id: Optional[str] = None     # required when workflow_kind=smart_app_action

    # ── Visibility ──
    visibility: WorkflowVisibility = Field(default_factory=WorkflowVisibility)

    # ── Failure alerts ──
    # Only the owner (author_email) is emailed on failure; no arbitrary
    # recipients. Failures are also logged at ERROR for IT.
    notifications: WorkflowNotifications = Field(default_factory=WorkflowNotifications)

    # ── Structure ──
    nodes: List[NodeDefinition] = Field(default_factory=list)
    edges: List[EdgeDefinition] = Field(default_factory=list)
    schedule: ScheduleConfig = Field(default_factory=ScheduleConfig)
    variables: Dict[str, Any] = Field(default_factory=dict)
    status: WorkflowStatus = WorkflowStatus.DRAFT
    # ── Authoritative run environment for UNATTENDED triggers ──
    # The environment ("test"/"prod") a DEPLOYED workflow resolves its
    # connections against when fired automatically (cron scheduler / webhook).
    # Set at deploy time and read by BOTH automatic trigger paths so they can
    # never disagree. The manual POST /execute path still overrides per-run via
    # the request body. Defaults to "prod" because a deployed workflow is, by
    # definition, live — and because unmigrated deployed docs (which only the
    # scheduler/webhook fire) must resolve PROD, not the executor's "test"
    # default.
    run_environment: Literal["test", "prod"] = "prod"
    # Per-workflow override of the global per-run LLM-call ceiling
    # (WF_MAX_RUN_LLM_CALLS). A single run may make at most this many LLM calls
    # across all agent/LLM nodes before it aborts fail-loud. None = use the
    # global default. Clamped to [1, WF_MAX_RUN_LLM_CALLS_HARD] at run time.
    max_run_llm_calls: Optional[int] = None
    webhook_token: Optional[str] = None
    is_active: bool = True
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    deployed_at: Optional[datetime] = None
    # Optimistic-concurrency / edit counter — bumped on every PUT. NOT the
    # deploy lineage (see deployed_version).
    version: int = 1
    # ── Deploy lineage (safe-deploy + rollback) ──
    # Pointer to the WorkflowVersions snapshot that is CURRENTLY live. Each
    # deploy/rollback appends an immutable snapshot of the exact graph that
    # went live and advances this number. None until the workflow's first
    # deploy. Lets the UI show "you are running v7" and roll back to any
    # earlier vN. Distinct from `version` (the per-edit concurrency token):
    # an edit bumps `version`, only a deploy/rollback bumps deploy lineage.
    deployed_version: Optional[int] = None
    last_deploy_note: str = ""

    # Reassignment audit trail. Populated by /api/admin/workflows/{id}/reassign.
    reassigned_at: Optional[datetime] = None
    reassigned_by: Optional[str] = None
    previous_owner: Optional[Dict[str, str]] = None  # {"owner_type": ..., "owner_id": ...}


class WorkflowVersion(BaseModel):
    """An IMMUTABLE snapshot of the exact graph that went live at a deploy
    or rollback. One document per deploy/rollback, stored in the
    ``WorkflowVersions`` collection and never mutated after insert.

    This is the backbone of safe deployment + rollback:
      - Safe deploy: "tested and it worked" is pinned to a concrete,
        content-hashed record of *what* was deployed, by whom, when, with
        an optional human note — rather than relying on the mutable live
        doc, which keeps changing under further edits.
      - Rollback: restoring vN copies its snapshotted graph back onto the
        live workflow and appends a NEW snapshot (source="rollback") so the
        history stays strictly append-only and the rollback itself is
        audited.
    """
    version_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    workflow_id: str
    # Monotonic per-workflow deploy lineage number (1, 2, 3, …). Matches the
    # live doc's ``deployed_version`` while this snapshot is the one running.
    version_number: int

    # ── Snapshot of the deployed graph (the rollback payload) ──
    name: str = ""
    description: str = ""
    nodes: List[NodeDefinition] = Field(default_factory=list)
    edges: List[EdgeDefinition] = Field(default_factory=list)
    schedule: ScheduleConfig = Field(default_factory=ScheduleConfig)
    variables: Dict[str, Any] = Field(default_factory=dict)
    # Authoritative run environment this snapshot was deployed against.
    run_environment: Literal["test", "prod"] = "prod"
    max_run_llm_calls: Optional[int] = None
    # sha256 over the canonicalised (nodes, edges, schedule, variables). Lets
    # the UI flag "v8 is identical to v6" and a redeploy detect a no-op graph.
    content_hash: str = ""

    # ── Provenance / audit ──
    source: Literal["deploy", "rollback"] = "deploy"
    # When source == "rollback", the version_number this snapshot was
    # restored FROM (so history reads "v9 ← rolled back to v4").
    restored_from_version: Optional[int] = None
    note: str = ""
    deployed_by: str = ""          # user_id of the actor
    deployed_by_email: str = ""
    org_id: str = ""
    created_at: Optional[datetime] = None


# ============================================================================
# Execution Models
# ============================================================================

class NodeExecutionResult(BaseModel):
    node_id: str
    status: NodeExecutionStatus
    output_data: Any = None
    output_type: Optional[str] = None  # informational: "items", "result", "branch", "status"
    error: Optional[str] = None
    retry_count: int = 0
    retry_errors: List[str] = Field(default_factory=list)
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    duration_ms: Optional[int] = None


class ApprovalRequest(BaseModel):
    """Tracks a pending human approval with notification state."""
    approval_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    execution_id: str
    workflow_id: str
    workflow_name: str = ""
    user_id: str
    node_id: str
    node_label: str = ""
    message: str = ""
    timeout_hours: float = DEFAULT_APPROVAL_TIMEOUT_HOURS
    data_preview: Any = None
    notification_sent: bool = False
    created_at: Optional[datetime] = None
    resolved_at: Optional[datetime] = None
    resolution: Optional[str] = None  # "approved" | "rejected" | "timed_out"
    resolved_by: Optional[str] = None


class WorkflowExecution(BaseModel):
    execution_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    workflow_id: str
    user_id: str
    status: ExecutionStatus = ExecutionStatus.PENDING
    trigger: str = "manual"
    trigger_data: Dict[str, Any] = Field(default_factory=dict)
    environment: str = "test"  # "test" or "prod"
    current_node: Optional[str] = None
    paused_at_node: Optional[str] = None
    approval_id: Optional[str] = None  # Links to ApprovalRequest when paused
    node_results: Dict[str, NodeExecutionResult] = Field(default_factory=dict)
    variables: Dict[str, Any] = Field(default_factory=dict)
    error: Optional[str] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None


# ============================================================================
# API Request/Response Models
# ============================================================================

class CreateWorkflowRequest(BaseModel):
    name: str
    description: str = ""
    nodes: List[NodeDefinition] = Field(default_factory=list, max_length=MAX_WORKFLOW_NODES)
    edges: List[EdgeDefinition] = Field(default_factory=list, max_length=MAX_WORKFLOW_EDGES)
    schedule: ScheduleConfig = Field(default_factory=ScheduleConfig)
    variables: Dict[str, Any] = Field(default_factory=dict)

    # ── Ownership (SA-only model) ──
    # Defaults to "service_account". owner_id must be supplied — clients
    # call POST /api/auth/me/personal-sa to obtain the caller's Personal
    # SA id when no shared SA is selected. "user" is rejected with 400.
    owner_type: Optional[Literal["service_account", "dept", "org"]] = None
    owner_id: Optional[str] = None
    workflow_kind: Optional[Literal[
        "dept_data_flow", "mcp_ingestion", "smart_app_action", "ad_hoc"
    ]] = None
    linked_smart_app_id: Optional[str] = None
    visibility: Optional[WorkflowVisibility] = None
    notifications: Optional[WorkflowNotifications] = None
    dept_ids: Optional[List[str]] = None


class UpdateWorkflowRequest(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    nodes: Optional[List[NodeDefinition]] = Field(default=None, max_length=MAX_WORKFLOW_NODES)
    edges: Optional[List[EdgeDefinition]] = Field(default=None, max_length=MAX_WORKFLOW_EDGES)
    schedule: Optional[ScheduleConfig] = None
    variables: Optional[Dict[str, Any]] = None
    notifications: Optional[WorkflowNotifications] = None
    is_active: Optional[bool] = None
    # Per-run LLM-call ceiling override (None leaves it unchanged here; set to a
    # positive int to override the global default, or 0/None via the UI's
    # "use default" affordance). Clamped to the admin hard-max at run time.
    max_run_llm_calls: Optional[int] = None
    # Optimistic-concurrency token. When set, the PUT is rejected with 409
    # if the stored version no longer matches — prevents a save built on a
    # stale copy (two tabs / AI-apply + manual edit) silently overwriting
    # newer server state.
    expected_version: Optional[int] = None


class DeployWorkflowRequest(BaseModel):
    """Request to deploy or undeploy a workflow."""
    action: Literal["deploy", "undeploy"]
    # Optional human note pinned to the immutable deploy snapshot (e.g.
    # "ticket OPS-431: switch to nightly cadence"). Audit only; max 500 chars.
    note: str = Field(default="", max_length=500)


class RollbackWorkflowRequest(BaseModel):
    """Restore a previously-deployed version's graph onto the live workflow.

    The target version's snapshotted graph is copied back; a NEW snapshot
    (source="rollback") is appended so history stays append-only. If the
    workflow is currently deployed, the restored graph is re-validated and
    its schedule re-registered so the live automation immediately runs the
    rolled-back logic.
    """
    version_number: int = Field(..., ge=1)
    note: str = Field(default="", max_length=500)


class ExecuteWorkflowRequest(BaseModel):
    variables: Dict[str, Any] = Field(default_factory=dict)


class WorkflowListItem(BaseModel):
    workflow_id: str
    name: str
    description: str = ""
    status: WorkflowStatus = WorkflowStatus.DRAFT
    is_active: bool = True
    node_count: int = 0
    schedule: Optional[ScheduleConfig] = None
    last_execution: Optional[Dict[str, Any]] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    deployed_at: Optional[datetime] = None


# ============================================================================
# Node Schema (for UI dynamic forms)
# ============================================================================

class NodeFieldSchema(BaseModel):
    name: str
    label: str
    type: Literal[
        "text", "textarea", "number", "select", "boolean", "json",
        "password", "cron", "tool_picker", "schema_builder",
        "connection_picker", "variable_assignments",
    ]
    required: bool = False
    default: Any = None
    options: Optional[List[Dict[str, Optional[str]]]] = None
    placeholder: Optional[str] = None
    help_text: Optional[str] = None
    visible_when: Optional[Dict[str, Any]] = None  # {"field": "processing_mode", "value": "batch"} — show only when condition met
    connection_type: Optional[str] = None  # For connection_picker: "sql", "mongo", "api"


class NodeSchema(BaseModel):
    type: NodeType
    category: NodeCategory
    label: str
    description: str
    icon: str = "⚙️"
    color: str = "#6366f1"
    fields: List[NodeFieldSchema] = Field(default_factory=list)
    inputs: int = 1  # Number of input handles
    outputs: int = 1  # Number of output handles
    output_labels: Optional[List[str]] = None  # Labels for multi-output handles
    # When True, the workflow PUT validator requires the caller's JWT dept_ids
    # to include the node config's dept_id (or the workflow to be linked to a
    # dept_source). Used by per-dept Milvus sinks to prevent cross-dept writes.
    dept_scope_required: bool = False


# ============================================================================
# Template Models
# ============================================================================

class WorkflowTemplate(BaseModel):
    template_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    name: str
    description: str = ""
    category: str = "general"
    icon: str = "⚙️"
    tags: List[str] = Field(default_factory=list)
    nodes: List[NodeDefinition] = Field(default_factory=list)
    edges: List[EdgeDefinition] = Field(default_factory=list)
    variables: Dict[str, Any] = Field(default_factory=dict)
    is_system: bool = True  # System-provided vs user-created


# ============================================================================
# AI Workflow Generation Models
# ============================================================================

class GenerateWorkflowRequest(BaseModel):
    """Request body for AI-powered workflow generation."""
    prompt: str
    conversation: List[Dict[str, str]] = Field(default_factory=list)  # Multi-turn history


class RefineWorkflowRequest(BaseModel):
    """Request body for refining an AI-generated workflow."""
    prompt: str
    workflow: Dict[str, Any]  # Current workflow JSON (nodes, edges, variables)
    conversation: List[Dict[str, str]] = Field(default_factory=list)
    # When True (default), the response carries a diff patch instead of the
    # full workflow. Set False for legacy clients that need the entire
    # workflow JSON returned.
    return_diff: bool = True


class EditNodeRequest(BaseModel):
    """Request body for AI-powered single-node editing.

    Used by the per-node right-click 'Edit with AI' affordance. The full
    workflow is sent for context but only the focused node's config is
    rewritten."""
    prompt: str
    workflow: Dict[str, Any]
    node_id: str
    conversation: List[Dict[str, str]] = Field(default_factory=list)


class AIChatRequest(BaseModel):
    """Request body for the agentic AI Assistant chat (streaming).

    One endpoint replaces the client-side generate/refine/edit-node routing:
    the model decides — from ``prompt`` — whether to answer, return code, edit
    a node, edit the workflow, or build a new one. ``workflow`` is the current
    canvas (the assistant's working copy); ``focused_node_id`` is set when the
    user is editing one node."""
    prompt: str
    workflow: Dict[str, Any] = Field(default_factory=dict)
    conversation: List[Dict[str, str]] = Field(default_factory=list)
    focused_node_id: Optional[str] = None


class SaveUserTemplateRequest(BaseModel):
    """Request body for saving a workflow as a personal template."""
    name: str
    description: str = ""
    icon: str = "⚙️"
    category: str = "custom"
    tags: List[str] = Field(default_factory=list)
    nodes: List[NodeDefinition] = Field(default_factory=list)
    edges: List[EdgeDefinition] = Field(default_factory=list)
    variables: Dict[str, Any] = Field(default_factory=dict)


# ============================================================================
# Connection Profile Models (Environment-based SDLC)
# ============================================================================

class ConnectionType(str, Enum):
    SQL = "sql"
    MONGO = "mongo"
    API = "api"
    SFTP = "sftp"
    BUCKET = "bucket"
    SMTP = "smtp"


class EnvironmentConfig(BaseModel):
    """Connection details for one environment (test or prod)."""
    connection_string: Optional[str] = None  # SQL conn string or Mongo URI
    url: Optional[str] = None  # For API connections
    headers: Dict[str, str] = Field(default_factory=dict)  # For API connections
    database: Optional[str] = None  # For Mongo external DB name
    # SFTP / FTP fields
    host: Optional[str] = None
    port: Optional[str] = None
    username: Optional[str] = None
    password: Optional[str] = None
    private_key: Optional[str] = None
    protocol: Optional[str] = None  # sftp | ftp | ftps
    # Bucket / S3 fields
    bucket: Optional[str] = None
    region: Optional[str] = None
    access_key_id: Optional[str] = None
    secret_access_key: Optional[str] = None
    endpoint_url: Optional[str] = None
    # SMTP / Email fields
    from_address: Optional[str] = None
    smtp_port: Optional[str] = None
    extra: Dict[str, Any] = Field(default_factory=dict)  # Any additional params

    def is_configured(self) -> bool:
        """True if this environment has enough to attempt a connection.

        Mirrors the frontend's buildEnvConfig() gate — an environment counts
        as 'configured' once one of the primary endpoint fields is present.
        Used to enforce the product rule: a connection must have at least one
        of test/prod filled, but never requires both.
        """
        return bool(
            self.connection_string or self.url or self.host or self.bucket
        )


class ConnectionProfile(BaseModel):
    """A reusable connection with test & prod environment configs.

    Connections on the IT workflow surface are ORG-owned and shared across the
    IT team — there is no per-user connection. A workflow run (always org-owned)
    resolves any connection in the same org (tenant isolation by ``org_id`` is
    the hard boundary; see connection_resolver.resolve_connection). The
    Service-Account owner branch remains only for the legacy SA-owned-run case
    and is inert for org-owned workflows.
    """
    connection_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    # Legacy author shadow — kept as created-by audit, NOT used for authorization.
    user_id: str
    # Ownership / tenancy — the authorization scope. IT connections are "org".
    owner_type: Literal["service_account", "dept", "org"] = "org"
    owner_id: str = ""           # the owning SA / dept / org id (org_id for IT)
    org_id: str = ""             # owning tenant
    name: str  # e.g. "Sales Database", "Payment API"
    type: ConnectionType
    description: str = ""
    test: EnvironmentConfig = Field(default_factory=EnvironmentConfig)
    prod: EnvironmentConfig = Field(default_factory=EnvironmentConfig)
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class CreateConnectionRequest(BaseModel):
    name: str
    type: ConnectionType
    description: str = ""
    test: EnvironmentConfig = Field(default_factory=EnvironmentConfig)
    prod: EnvironmentConfig = Field(default_factory=EnvironmentConfig)

    @field_validator("test", "prod", mode="before")
    @classmethod
    def _null_env_to_empty(cls, v):
        """Treat an explicit null environment as an empty config.

        The UI sends `test`/`prod` as null when the user only fills one
        environment. A bare `default_factory` only applies when the field is
        *omitted*, so an explicit null would otherwise raise
        'Input should be a valid dictionary'. Coercing here lets the user save
        with only one environment configured.
        """
        return EnvironmentConfig() if v is None else v


class UpdateConnectionRequest(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    test: Optional[EnvironmentConfig] = None
    prod: Optional[EnvironmentConfig] = None


class ExecuteWorkflowRequestV2(BaseModel):
    """Extended execute request with environment selection."""
    variables: Dict[str, Any] = Field(default_factory=dict)
    environment: Literal["test", "prod"] = "test"
