# Copyright (c) 2026 Trustedwear Tech Private Limited (https://citra-ai.com)
# Author: Rohit Kumar Chandan
# SPDX-License-Identifier: BUSL-1.1
#
# Licensed under the Business Source License 1.1. Non-production use is granted;
# production use requires a commercial licence until the Change Date, after
# which this file converts to Apache-2.0. See LICENSE at the repository root.

"""
Workflow API Router
===================
CRUD operations for workflows + execution endpoints.
"""

from __future__ import annotations
import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import time
import uuid
from datetime import datetime
from typing import Any, Dict, List, Literal, Optional, Tuple

from fastapi import APIRouter, HTTPException, Request, Query, UploadFile, File, Form
from pydantic import BaseModel

from citra_auth import get_secure_user_id, Roles
from citra_mongo import get_async_mongo_client, MONGODB_DATABASE

from .models import (
    CreateWorkflowRequest, UpdateWorkflowRequest, ExecuteWorkflowRequest,
    DeployWorkflowRequest, RollbackWorkflowRequest, WorkflowVersion, WorkflowStatus,
    WorkflowDefinition, WorkflowExecution, WorkflowListItem, ExecutionStatus,
    WorkflowTemplate, NodeDefinition, EdgeDefinition,
    CreateConnectionRequest, UpdateConnectionRequest, ConnectionProfile,
    ExecuteWorkflowRequestV2,
    GenerateWorkflowRequest, RefineWorkflowRequest, EditNodeRequest, SaveUserTemplateRequest,
    AIChatRequest,
    NodeType,
    WorkflowVisibility,
    WorkflowNotifications,
)
from .ai_context import gather_ai_context, render_ai_context_sections
from .workflow_reference_validator import (
    validate_workflow_references, errors_to_suggestions, detect_setup_gaps,
)
from .nodes import get_all_schemas
from .nodes.agents import AVAILABLE_TOOLS
from .executor import WorkflowExecutor, ConcurrencyLimitError
from .connection_crypto import (
    encrypt_env_config,
    decrypt_env_config,
    assert_encrypted_envelope,
    SECRET_FIELDS,
)
from .config import (
    RATE_LIMIT_WEBHOOK, RATE_LIMIT_WINDOW,
    RATE_LIMIT_AI_PER_USER, RATE_LIMIT_AI_PER_ORG,
    PAGE_DEFAULT_WORKFLOWS, PAGE_DEFAULT_EXECUTIONS, PAGE_DEFAULT_APPROVALS, PAGE_MAX,
    MONGO_TEST_TIMEOUT_MS, HTTP_TIMEOUT_CONN_TEST,
    SECRET_MASK_LENGTH, WEBHOOK_TOKEN_GRACE_PERIOD, WEBHOOK_REPLAY_WINDOW,
    PROGRESS_CACHE_TTL,
)

logger = logging.getLogger(__name__)
router = APIRouter()

# ─── Helpers ───────────────────────────────────────────────────────────

def _db():
    client = get_async_mongo_client()
    return client[MONGODB_DATABASE]


_EXECUTION_INDEXES_READY = False


async def ensure_execution_indexes() -> None:
    """Create the index backing the per-workflow run-history query
    (``find({workflow_id}).sort(started_at, -1)``). Idempotent and safe to call
    on every startup — Mongo no-ops if the index already exists.
    """
    global _EXECUTION_INDEXES_READY
    if _EXECUTION_INDEXES_READY:
        return
    try:
        await _db()["WorkflowExecutions"].create_index(
            [("workflow_id", 1), ("started_at", -1)],
            name="idx_workflow_started",
        )
        # Deploy-lineage history: list newest-first per workflow, and enforce
        # one snapshot per (workflow, version_number) so a racing double-deploy
        # can't fork the lineage.
        await _db()["WorkflowVersions"].create_index(
            [("workflow_id", 1), ("version_number", -1)],
            name="idx_wfver_workflow_version",
            unique=True,
        )
        _EXECUTION_INDEXES_READY = True
    except Exception as exc:  # noqa: BLE001
        logger.warning("WorkflowExecutions index init failed: %s", exc)


# ─── In-memory rate limiter ───────────────────────────────────────────
from collections import defaultdict as _defaultdict

_rate_buckets: Dict[str, List[float]] = _defaultdict(list)


def _check_rate_limit(key: str, max_requests: int, window: int = RATE_LIMIT_WINDOW) -> bool:
    """Return True if the request should be allowed, False if rate-limited.

    In-process, per-replica. Fine for the webhook (best-effort abuse damping);
    do NOT use it where a GLOBAL ceiling matters — use the distributed limiter
    below for that.
    """
    now = time.time()
    bucket = _rate_buckets[key]
    # Prune expired entries
    _rate_buckets[key] = bucket = [t for t in bucket if now - t < window]
    if len(bucket) >= max_requests:
        return False
    bucket.append(now)
    return True


# Fixed-window counter shared across all replicas via the Redis-backed cache.
# Atomic INCR (+ EXPIRE on the first hit of a window) so concurrent requests on
# different replicas can't slip past the ceiling. Used where the limit must be
# GLOBAL — currently the expensive AI/LLM endpoints (prod-readiness HIGH #2).
_RATE_LIMIT_LUA = (
    "local c = redis.call('INCR', KEYS[1]) "
    "if c == 1 then redis.call('EXPIRE', KEYS[1], ARGV[1]) end "
    "return c"
)


def _check_rate_limit_distributed(key: str, max_requests: int, window: int = RATE_LIMIT_WINDOW) -> bool:
    """Global fixed-window limiter. Returns True if allowed, False if over the
    limit. Fails OPEN (allows + logs a warning) if the shared cache is
    unreachable — a Redis hiccup must not hard-block legitimate IT work; the
    in-process fallback is intentionally not used so the limit can't silently
    fragment per-replica."""
    try:
        from citra_cache import get_cache_manager
        count = get_cache_manager().eval(_RATE_LIMIT_LUA, 1, f"wf:rl:{key}", window)
        return int(count) <= max_requests
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Distributed rate limiter unavailable for key %r (%s) — allowing request",
            key, exc,
        )
        return True


def _enforce_ai_rate_limit(claims: Dict[str, Any]) -> None:
    """Throttle the expensive AI generation/editing endpoints per-user AND
    per-org against the shared counter, raising 429 when either ceiling is hit.
    Stops a runaway loop from running up unbounded inference cost."""
    user_id = claims.get("user_id") or "anon"
    org_id = claims.get("org_id") or "no-org"
    if not _check_rate_limit_distributed(f"ai:user:{user_id}", RATE_LIMIT_AI_PER_USER):
        raise HTTPException(
            status_code=429,
            detail=(
                f"AI request rate limit reached ({RATE_LIMIT_AI_PER_USER} per "
                f"{RATE_LIMIT_WINDOW}s). Please wait a moment and retry."
            ),
        )
    if not _check_rate_limit_distributed(f"ai:org:{org_id}", RATE_LIMIT_AI_PER_ORG):
        raise HTTPException(
            status_code=429,
            detail=(
                f"Your organisation has reached its AI request rate limit "
                f"({RATE_LIMIT_AI_PER_ORG} per {RATE_LIMIT_WINDOW}s). Retry shortly."
            ),
        )


def _serialize(doc) -> dict:
    """Recursively convert MongoDB types (ObjectId, datetime) to JSON-safe strings."""
    from bson import ObjectId
    from datetime import datetime
    if isinstance(doc, dict):
        return {k: _serialize(v) for k, v in doc.items()}
    if isinstance(doc, list):
        return [_serialize(v) for v in doc]
    if isinstance(doc, ObjectId):
        return str(doc)
    if isinstance(doc, datetime):
        return doc.isoformat()
    return doc


def _is_deployed(wf: Optional[dict]) -> bool:
    """Canonical deployed-state predicate, matching the existing inline check
    used across deploy/schedule logic (e.g. update_workflow's schedule reload).

    A workflow is "deployed" iff its status is DEPLOYED. ``is_active`` means
    *not archived* — it is NOT a deploy signal — so it must not be used here.
    """
    return bool(wf) and wf.get("status") == WorkflowStatus.DEPLOYED.value


def _compute_duration_ms(started_at, completed_at) -> Optional[int]:
    """Milliseconds between two datetimes, or None if either is missing.

    Tolerates already-serialized ISO strings as well as datetime objects so it
    is safe to call before/after ``_serialize``.
    """
    from datetime import datetime as _dt
    if not started_at or not completed_at:
        return None
    try:
        s = started_at if isinstance(started_at, _dt) else _dt.fromisoformat(str(started_at))
        c = completed_at if isinstance(completed_at, _dt) else _dt.fromisoformat(str(completed_at))
    except (ValueError, TypeError):
        return None
    delta_ms = int((c - s).total_seconds() * 1000)
    return delta_ms if delta_ms >= 0 else None


def _agent_mcp_tool_entries(raw: Any) -> List[Dict[str, Any]]:
    """Tolerantly parse an AI Agent node's ``mcp_tools`` config into a list of
    dicts. Used by dept-scope authz. Best-effort: malformed config yields [] —
    the runtime's _build_mcp_tool_registry is the loud validator at execution."""
    import json as _json
    if isinstance(raw, str):
        raw = raw.strip()
        if not raw:
            return []
        try:
            raw = _json.loads(raw)
        except _json.JSONDecodeError:
            return []
    if not isinstance(raw, list):
        return []
    return [e for e in raw if isinstance(e, dict)]


def _dept_scoped_node_types() -> set:
    """Set of NodeType.value strings for which the workflow router enforces
    dept-scope authz on edits. Sourced from the node registry so adding a
    new ``dept_scope_required = True`` class is enough."""
    from .nodes import get_registry
    out: set = set()
    for n_type, cls in get_registry().items():
        if getattr(cls, "dept_scope_required", False):
            out.add(getattr(n_type, "value", n_type))
    return out


def _jwt_claims(request: Request) -> Dict[str, Any]:
    """Read enterprise identity claims from request.state (populated by
    JWT auth middleware). Returns a uniform dict so callers don't have
    to handle missing attrs.
    """
    return {
        "user_id": getattr(request.state, "user_id", "") or "",
        "email": getattr(request.state, "email", "") or "",
        "org_id": getattr(request.state, "org_id", "") or "",
        "dept_ids": list(getattr(request.state, "dept_ids", []) or []),
        "roles": list(getattr(request.state, "roles", []) or []),
        "is_service_account": bool(getattr(request.state, "is_service_account", False)),
        "service_account_admin_of": list(
            getattr(request.state, "service_account_admin_of", []) or []),
        "service_account_member_of": list(
            getattr(request.state, "service_account_member_of", []) or []),
        # Auto-provisioned SAs from Citra-User-Service (JWT claims).
        "work_sa_id": getattr(request.state, "work_sa_id", "") or "",
        "personal_sa_id": getattr(request.state, "personal_sa_id", "") or "",
    }


# ── Workflow-surface RBAC ────────────────────────────────────────────────
# Workflows are an IT-owned surface. Access requires ANY of:
#   - super_admin
#   - org_admin
#   - IT-workflow                              (dedicated access flag)
#   - dept_admin AND dept_ids includes IT_DEPT_ID
# IT_DEPT_ID is the well-known IT department slug; defaults to "it" and is
# overridable via WORKFLOW_IT_DEPT_ID for deployments that use a different
# slug. The rule mirrors Citra-User-Service/src/middleware/authMiddleware.js
# requireWorkflowRole — keep them in sync.
WORKFLOW_ACCESS_ROLES = frozenset({Roles.SUPER_ADMIN, Roles.ORG_ADMIN, Roles.IT_WORKFLOW})
IT_DEPT_ID = (os.environ.get("WORKFLOW_IT_DEPT_ID") or "it").lower()


def _has_workflow_access(claims: Dict[str, Any]) -> bool:
    roles = set(claims.get("roles") or [])
    if WORKFLOW_ACCESS_ROLES & roles:
        return True
    if Roles.DEPT_ADMIN in roles:
        depts = [str(d).lower() for d in (claims.get("dept_ids") or [])]
        if IT_DEPT_ID in depts:
            return True
    return False


def _require_workflow_access(request: Request) -> Dict[str, Any]:
    """Single gate for the entire workflow surface. Returns claims if
    allowed, raises 403 otherwise. Use at the top of every workflow API
    handler before doing per-document checks.
    """
    claims = _jwt_claims(request)
    if not _has_workflow_access(claims):
        raise HTTPException(
            status_code=403,
            detail="workflow access requires IT-workflow, org_admin, super_admin, or IT-dept admin role",
        )
    if not claims.get("org_id"):
        raise HTTPException(
            status_code=403,
            detail="workflow access requires an org-scoped identity",
        )
    return claims


def _same_org_or_super(claims: Dict[str, Any], wf: Optional[dict]) -> bool:
    """True if caller may operate on this workflow under org-only
    ownership: super_admin bypasses; everyone else must share the
    workflow's org. `wf` may be None when the workflow has been deleted
    out from under an open page — caller should 404 in that case."""
    if wf is None:
        return False
    if Roles.SUPER_ADMIN in set(claims.get("roles") or []):
        return True
    return wf.get("org_id") == claims.get("org_id")


async def _authorize_execution(
    request: Request, execution_id: str, *, projection: Optional[dict] = None,
) -> Tuple[Dict[str, Any], dict]:
    """Look up an execution and authorise the caller against the parent
    workflow's org. Any user with workflow access in that org may view /
    approve / reject — execution data is shared across the IT team, not
    siloed to whoever pressed Run. Returns (claims, exec_doc); raises
    HTTPException on 403/404.
    """
    claims = _require_workflow_access(request)
    db = _db()
    exec_doc = await db["WorkflowExecutions"].find_one(
        {"execution_id": execution_id}, projection or None,
    )
    if not exec_doc:
        raise HTTPException(status_code=404, detail="Execution not found")
    wf = await db["Workflows"].find_one(
        {"workflow_id": exec_doc.get("workflow_id")},
        {"org_id": 1, "workflow_id": 1},
    )
    if not _same_org_or_super(claims, wf):
        raise HTTPException(status_code=403, detail="execution belongs to a different org")
    return claims, exec_doc


def _visibility_filter(request: Request) -> Dict[str, Any]:
    """Build the Mongo filter clause that limits a list query to workflows
    the caller is allowed to LIST/READ.

    Org-only ownership reduces this to a single rule:
      - super_admin: everything (cross-org break-glass)
      - any other workflow-access role: workflows in the caller's org
      - everyone else: zero rows
    """
    claims = _jwt_claims(request)
    base = {"is_active": True}
    roles = set(claims["roles"])

    if Roles.SUPER_ADMIN in roles:
        return base

    if _has_workflow_access(claims) and claims["org_id"]:
        return {**base, "org_id": claims["org_id"]}

    # No workflow access role — return a zero-match filter rather than
    # an empty $or (Mongo errors on empty $or).
    return {**base, "_no_access_": True}


def _check_workflow_action(
    workflow: dict, request: Request, action: str = "read"
) -> Optional[Tuple[int, str]]:
    """Check whether the request can perform `action` (read | run | edit)
    on a given workflow doc.

    Org-only ownership reduces this to a single rule for every action:
      - super_admin: always allowed
      - any other workflow-access role (org_admin, IT-workflow,
        dept_admin@IT-dept) AND workflow.org_id == caller.org_id: allowed
      - else: 403

    Returns None if allowed, else (status_code, message).
    """
    claims = _jwt_claims(request)
    roles = set(claims["roles"])

    if Roles.SUPER_ADMIN in roles:
        return None

    if not _has_workflow_access(claims):
        return (403, "workflow access requires IT-workflow, org_admin, super_admin, "
                     "or IT-dept admin role")

    if workflow.get("org_id") != claims["org_id"]:
        return (403, "workflow belongs to a different org")

    return None


async def _validate_deploy_environment(db, nodes: list, target_env: str) -> List[str]:
    """Deploy-time environment-safety guard. Returns human-readable errors
    (empty = OK). Two checks, both fail-loud (prod-readiness #7 / BLOCKER #1):

      1. No write-capable node may carry an inline connection string — those
         are not environment-isolated, so a workflow labelled ``test`` could
         commit to a production system.
      2. Every saved ``connection_id`` referenced by any node must have a
         config for ``target_env``, so the deployed run won't fail deep inside
         a node with a missing-environment error on its first fire.
    A referenced connection that cannot be found is left to the runtime
    ownership/resolution check rather than blocked here (avoids false deploys).
    """
    errors: List[str] = []
    checked: dict = {}
    for n in nodes:
        ntype = (n.get("type") if isinstance(n, dict) else getattr(n, "type", "")) or ""
        cfg = (n.get("config") if isinstance(n, dict) else getattr(n, "config", {})) or {}
        label = (n.get("label") if isinstance(n, dict) else getattr(n, "label", "")) or ntype or "?"
        inline = str(cfg.get("connection_string") or "").strip()
        conn_id = cfg.get("connection_id")
        if ntype in _WRITE_NODE_TYPES and inline and not conn_id:
            errors.append(
                f"Node '{label}' ({ntype}) uses an inline connection string, which "
                f"is not environment-isolated — attach a saved connection before "
                f"deploying so the run resolves the {target_env} credentials."
            )
        if conn_id and conn_id not in checked:
            conn = await db["WorkflowConnections"].find_one({"connection_id": conn_id})
            checked[conn_id] = True
            if conn is not None and not (conn.get(target_env) or {}):
                errors.append(
                    f"Connection '{conn.get('name', conn_id)}' (used by node '{label}') "
                    f"has no '{target_env}' configuration — add it before deploying to {target_env}."
                )
    return errors


def _validate_dag(nodes: list, edges: list) -> List[str]:
    """Validate workflow graph structure. Returns list of error messages (empty = valid)."""
    errors: List[str] = []
    if not nodes:
        return errors  # empty graph is valid at save time

    node_ids = {n.id if hasattr(n, "id") else n.get("id") for n in nodes}

    # Validate edges reference existing nodes
    for e in edges:
        src = e.source if hasattr(e, "source") else e.get("source")
        tgt = e.target if hasattr(e, "target") else e.get("target")
        if src not in node_ids:
            errors.append(f"Edge references unknown source node: {src}")
        if tgt not in node_ids:
            errors.append(f"Edge references unknown target node: {tgt}")

    # Build adjacency for cycle detection (Kahn's algorithm)
    from collections import deque, defaultdict
    adj = defaultdict(list)
    in_degree = defaultdict(int)
    for nid in node_ids:
        in_degree[nid] = 0
    for e in edges:
        src = e.source if hasattr(e, "source") else e.get("source")
        tgt = e.target if hasattr(e, "target") else e.get("target")
        adj[src].append(tgt)
        in_degree[tgt] += 1

    queue = deque(nid for nid in node_ids if in_degree[nid] == 0)
    visited = 0
    while queue:
        n = queue.popleft()
        visited += 1
        for child in adj[n]:
            in_degree[child] -= 1
            if in_degree[child] == 0:
                queue.append(child)

    if visited < len(node_ids):
        errors.append("Workflow contains a cycle — nodes must form a DAG")

    # Check for orphan nodes (no incoming or outgoing edges) — only warn if >1 node
    if len(node_ids) > 1:
        connected = set()
        for e in edges:
            connected.add(e.source if hasattr(e, "source") else e.get("source"))
            connected.add(e.target if hasattr(e, "target") else e.get("target"))
        orphans = node_ids - connected
        if orphans:
            errors.append(f"Orphan nodes not connected to any edge: {orphans}")

    return errors


# ─── Node Palette ─────────────────────────────────────────────────────

@router.get("/api/workflows/node-schemas")
async def list_node_schemas(request: Request):
    """Return all available node type schemas for the drag-and-drop palette."""
    _require_workflow_access(request)
    schemas = get_all_schemas()
    return {"schemas": [s.model_dump() for s in schemas]}


@router.get("/api/workflows/agent-tools")
async def list_agent_tools(request: Request):
    """Return the built-in tools an ai_agent node can be given.

    Restored after the original endpoint was removed with the dept-MCP
    discovery it also served. The built-in tools are real and unrelated to
    that: without this, NodeConfigPanel's tool picker caught the 404 and
    rendered an empty list, so the agent node looked like it had no tools at
    all. Sourced from AVAILABLE_TOOLS so the picker cannot drift from what the
    executor can actually dispatch.
    """
    _require_workflow_access(request)
    from .nodes.agents import AVAILABLE_TOOLS
    return {"tools": list(AVAILABLE_TOOLS.values())}


# ─── Execution Capacity (static path — must precede {workflow_id}) ─────

@router.get("/api/workflows/execution-capacity")
async def execution_capacity(request: Request):
    """Return current concurrent execution capacity."""
    _require_workflow_access(request)
    executor = WorkflowExecutor()
    return executor.get_capacity()


# ─── Agent Tools (static path — must precede {workflow_id}) ────────────

# GET /api/workflows/agent-tools REMOVED 2026-08-08 (PORTING.md §1, §6b).
#
# It listed the dept-MCP tools an AI-agent node could call, discovered through
# the Citra discovery service via services.enterprise_mcp_client. Neither the
# discovery service nor that client exists here.
#
# The replacement is the MCP Server node (nodes/mcp.py) with operation
# "list_tools": it asks the MCP server the user configured, over the public
# protocol, instead of a platform registry.

# GET /api/workflows/mcp-sources REMOVED 2026-08-08 (PORTING.md §1, §7).
#
# It answered "which dept-MCP sources can this org reach?" by querying the
# Citra discovery service. A standalone deployment has no such registry: an
# MCP endpoint is configured on the node by the person building the workflow.

# ─── AI Code Generation ───────────────────────────────────────────────

class GenerateCodeRequest(BaseModel):
    prompt: str
    input_schema: Optional[Dict[str, Any]] = None

@router.post("/api/workflows/generate-code")
async def generate_code(request: Request, body: GenerateCodeRequest):
    """Use LLM to generate Python code for a CodeBlock node."""
    _enforce_ai_rate_limit(_require_workflow_access(request))
    user_id = get_secure_user_id(request)

    from citra_llm.oss import llm_call

    system_prompt = (
        "You are a Python code generator for a sandboxed workflow engine.\n"
        "RULES:\n"
        "- The variable `data` (dict) contains the input from the previous node.\n"
        "- The variable `variables` (dict) contains workflow-level variables.\n"
        "- You MUST assign the final output to a variable called `result`.\n"
        "- NO imports are allowed.\n"
        "- Only these builtins are available: len, range, enumerate, zip, map, filter, "
        "sorted, reversed, min, max, sum, abs, round, str, int, float, bool, list, dict, "
        "set, tuple, isinstance, True, False, None, print.\n"
        "- Do NOT use eval, exec, compile, open, __import__, getattr, setattr, or any dunder attributes.\n"
        "- Return ONLY the Python code. No markdown fences, no explanations.\n"
    )

    if body.input_schema:
        system_prompt += f"\nExpected input schema: {body.input_schema}\n"

    raw = await llm_call(
        prompt=body.prompt,
        system=system_prompt,
        user_id=user_id,
        max_tokens=4000,
    )

    # Strip markdown code fences if present
    code = raw.strip()
    if code.startswith("```"):
        lines = code.split("\n")
        # Remove first line (```python or ```) and last line (```)
        if lines[-1].strip() == "```":
            lines = lines[1:-1]
        else:
            lines = lines[1:]
        code = "\n".join(lines)

    return {"code": code}


# ─── AI Workflow Generation ───────────────────────────────────────────

# Static framing — everything in here is tenant-agnostic. The dynamic
# node palette + connections are appended at
# request time by ``_build_ai_system_prompt`` so the AI sees only what
# the caller can actually use.
_WORKFLOW_GEN_PROMPT_FRAMING = """\
You are an expert workflow designer for Citra AI's visual workflow builder.
Your job is to generate a complete, valid workflow definition as JSON from a
user's natural language description, using ONLY the node types, connections,
and dept-MCP datasets enumerated below.

## Output Format

Return ONLY a valid JSON object with this structure:
{
  "name": "Workflow Name",
  "description": "Brief description of what this workflow does",
  "icon": "emoji",
  "tags": ["tag1", "tag2"],
  "nodes": [
    {
      "id": "unique_short_id",
      "type": "node_type",
      "label": "Human-readable label",
      "position": {"x": number, "y": number},
      "config": { ... node-specific config ... }
    }
  ],
  "edges": [
    {
      "id": "edge_id",
      "source": "source_node_id",
      "target": "target_node_id",
      "label": "optional label"
    }
  ],
  "variables": {},
  "needs_clarification": false,
  "reply": "A short conversational message to the user — see 'Talking to the user' below. Empty string when you have nothing to say.",
  "suggestions": ["Refinement YOU can make on the next turn", "..."],
  "prerequisites": ["A setup step only the USER can do outside this chat", "..."]
}

## Layout Rules
- Place nodes top-to-bottom: trigger at y=0, sources at y=200, processors at y=400, outputs at y=600
- Space nodes horizontally: 300px gap between parallel nodes
- First node at x=400
- Use short, unique IDs like "trigger1", "sql1", "agent1", "email1"

## Rules
- Always include exactly ONE trigger node
- Every node must be connected via edges (no orphan nodes)
- The graph must be a DAG (no cycles)
- For nodes that accept a `connection_id`, use ONLY ids from the "Available
  Connections" section below. Never invent a connection id.
- To call an external tool server, use the `mcp_server` node — it speaks the
  standard MCP JSON-RPC protocol and takes a server URL. It is read-only
  unless the author explicitly opts into writes.
- To search a vector database, use `vector_search` (Qdrant, Milvus, Weaviate,
  pgvector or Chroma). To build or refresh an index, use `vector_embed` to turn
  a text field into vectors first. To improve the ordering of retrieved
  results before they reach an agent, put a `reranker` node after the search.
- An `ai_agent` node does NOT call MCP servers or vector stores itself. Put
  `mcp_server` / `vector_search` / `reranker` UPSTREAM of the agent and let
  their output flow into it, then write the agent's prompts to use that data.
- If the user's intent doesn't map to anything available, add a
  'suggestions' item asking them to register the missing connection /
  source first; don't fabricate placeholders.
- For AI nodes, write detailed system_prompt and user_prompt using {{data}} for input data
- Model tier: AI agent and LLM-backed nodes (`ai_agent`, `llm_processor`,
  `rules_engine`, `classifier`, `extractor`, `summarizer`) take a `tier`
  config of "small" | "medium" | "large". Match the tier to the reasoning
  complexity of THAT node's task — do not put every node on the same tier:
    • "large"  — complex / multi-step reasoning, planning, tool use, nuanced
                 judgement, ambiguous or open-ended tasks, long-context
                 synthesis. Use for agents that drive decisions.
    • "medium" — moderate complexity: structured extraction, multi-class
                 classification, summarisation, routine transformation.
    • "small"  — simple, near-deterministic tasks: short rewrites, yes/no or
                 single-field checks, trivial formatting/labelling.
  The DEFAULT is "large" — if you omit `tier`, the node runs on the large
  model. Only set a smaller tier when the step is genuinely simpler, e.g.
  "config": { ..., "tier": "small" }. When unsure, leave it at "large".
- The "suggestions" array should contain 2-4 helpful follow-up refinements the user might want

## Talking to the user — `reply`, `suggestions` vs `prerequisites`

You have THREE ways to communicate, and you MUST use the right one. Mixing
them up is the single most common failure (e.g. telling the user to "register
a connection" as if it were a refinement you could perform).

- `reply` — a short, plain-language message to the user. Use it to:
    • SUMMARISE what you built. On a FRESH generation that produces a
      workflow, ALWAYS open `reply` with one or two sentences explaining how
      the flow works — what starts it, the key steps in order, and where it
      ends (e.g. "This runs on demand: it queries today's theft incidents,
      has an AI step write a structured report, then emails it. ").
    • ASK A CLARIFYING QUESTION when the request is ambiguous or you're
      missing a fact you need (which database? which recipients? what
      schedule?). Prefer asking over guessing when the answer materially
      changes the workflow.
    • EXPLAIN something you cannot do, or why the workflow is incomplete
      (e.g. "…but it can't run until a SQL connection and an SMTP connection
      exist — set those up in the Connections screen, then tell me their
      names and I'll wire them.").
    • Leave it "" only on a refine/edit that fully satisfies the request with
      nothing to summarise, ask, or explain.

- `needs_clarification` (boolean) — set this to TRUE only when the request is
  too vague to draft anything sensible (e.g. "automate my approvals" with no
  hint of source, condition, or action). In that case return EMPTY `nodes`
  and `edges`, put 1-3 specific questions in `reply`, and DON'T guess a graph.
  For a request you CAN make reasonable assumptions about, leave this FALSE,
  build your best-guess workflow, and note the assumptions/questions in
  `reply` instead — a half-built canvas the user can react to beats an
  interrogation.

- `suggestions` — follow-up refinements **YOU can perform** on the next turn
  if the user picks one (e.g. "Add a PDF export before the email", "Switch
  to a daily 6 PM schedule"). The UI shows these as one-click chips that get
  sent back to you. NEVER put a user-side setup action here.

- `prerequisites` — setup steps **only the USER can do**, OUTSIDE this chat,
  that the workflow needs before it can run: registering a connection in the
  Connections screen, registering a data source in Dept Sources, etc. These
  are shown as an informational checklist, NOT as clickable refinements.
  When a node is missing a required connection/source because none exists
  yet, put the setup step HERE and mention it in `reply` — do NOT phrase it
  as a `suggestion`.

CRITICAL: if a refinement request asks you to do something only the user can
do (e.g. "register a SQL connection"), do NOT silently return the same
workflow. Set `reply` to explain you can't do that and what the user should
do instead, and (if still relevant) keep it in `prerequisites`.

- Return ONLY the JSON. No markdown fences, no explanations.
"""


def _build_ai_system_prompt(context: Dict[str, Any]) -> str:
    """Compose the per-request system prompt: framing + dynamic context."""
    return _WORKFLOW_GEN_PROMPT_FRAMING + "\n\n" + render_ai_context_sections(context)


def _parse_workflow_json(raw: str) -> Dict[str, Any]:
    """Parse and validate AI-generated workflow JSON."""
    import json
    text = raw.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        if lines[-1].strip() == "```":
            lines = lines[1:-1]
        else:
            lines = lines[1:]
        text = "\n".join(lines)

    parsed = json.loads(text)

    # Validate DAG
    nodes_raw = parsed.get("nodes", [])
    edges_raw = parsed.get("edges", [])
    dag_errors = _validate_dag(nodes_raw, edges_raw)
    if dag_errors:
        raise ValueError(f"Invalid workflow graph: {'; '.join(dag_errors)}")

    return parsed


def _llm_author_with_repair(
    *,
    user_prompt: str,
    system_prompt: str,
    user_id: str,
    max_tokens: int,
    parse,
):
    """Run an AI authoring call and parse its output, with ONE self-correction
    retry on malformed output.

    The LLM occasionally emits invalid JSON, prose wrapped around the JSON, or
    a DAG-invalid graph (the GLM models in this stack are especially prone to
    this). Failing the whole turn on the first bad response is needlessly
    brittle, so we feed the parse/validation error back and retry once —
    mirroring the self-correction loop the MCP NL→SQL planner already uses. If
    the retry still fails, the parse exception propagates so the caller can
    surface a 422.

    ``parse`` is a callable taking the raw LLM string and returning the parsed
    object (or raising ``json.JSONDecodeError`` / ``ValueError``).
    """
    from citra_llm.oss import llm_call

    raw = llm_call(
        user_prompt=user_prompt,
        system_prompt=system_prompt,
        user_id=user_id,
        max_tokens=max_tokens,
    )
    try:
        return parse(raw)
    except (json.JSONDecodeError, ValueError) as exc:
        logger.warning(
            "AI authoring output invalid (%s); retrying once with the error "
            "fed back to the model.",
            exc,
        )
        repair_prompt = (
            f"{user_prompt}\n\n"
            f"YOUR PREVIOUS RESPONSE WAS REJECTED: {exc}\n"
            f"Return ONLY a single valid JSON object in the exact format "
            f"specified in the system prompt — no markdown code fences, and no "
            f"explanatory text before or after the JSON."
        )
        raw2 = llm_call(
            user_prompt=repair_prompt,
            system_prompt=system_prompt,
            user_id=user_id,
            max_tokens=max_tokens,
        )
        # A second failure propagates → caller returns 422.
        return parse(raw2)


async def _gather_request_context(
    request: Request, user_id: str, query: str = "",
) -> Dict[str, Any]:
    """Pull the tenant identity off the request and fetch AI context.

    ``query`` (the user's workflow prompt / edit request) is used to
    semantic-rank the injected MCP sources so a tenant with 100+ sources
    only surfaces the most relevant ~10 to the builder.
    """
    tenant_id = (
        getattr(request.state, "org_id", "")
        or getattr(request.state, "tenant_id", "")
        or user_id  # fallback for single-tenant dev setups
    )
    dept_ids = list(getattr(request.state, "dept_ids", []) or [])
    auth_header = request.headers.get("authorization") or request.headers.get("Authorization")
    return await gather_ai_context(
        user_id=user_id,
        tenant_id=tenant_id,
        dept_ids=dept_ids,
        auth_header=auth_header,
        query=query,
    )


# Bound how much prior chat we replay to the LLM so a long building session
# can't grow the prompt unbounded (each refine ALSO re-sends the full current
# workflow JSON, so history must stay lean). Most recent turns are kept; older
# ones collapse to a single marker. Overridable per-deployment.
MAX_CONVERSATION_MESSAGES = int(os.getenv("WF_BUILDER_MAX_CONVERSATION_MSGS", "16"))
MAX_CONVERSATION_MSG_CHARS = int(os.getenv("WF_BUILDER_MAX_CONVERSATION_MSG_CHARS", "4000"))


def _format_conversation(conversation: List[Dict[str, str]]) -> List[str]:
    """Render prior turns for the LLM, bounded to keep the prompt budget sane.

    Keeps only the most recent ``MAX_CONVERSATION_MESSAGES`` turns (older ones
    collapse to a single marker) and truncates any single oversized message.
    Without this, a long multi-turn build re-sends every prior turn plus the
    full workflow JSON each refine and eventually overruns the context window.
    """
    convo = list(conversation or [])
    dropped = 0
    if len(convo) > MAX_CONVERSATION_MESSAGES:
        dropped = len(convo) - MAX_CONVERSATION_MESSAGES
        convo = convo[-MAX_CONVERSATION_MESSAGES:]

    parts: List[str] = []
    if dropped:
        parts.append(f"[... {dropped} earlier message(s) omitted for brevity ...]")
    for msg in convo:
        role = msg.get("role", "user")
        content = msg.get("content", "") or ""
        if len(content) > MAX_CONVERSATION_MSG_CHARS:
            content = content[:MAX_CONVERSATION_MSG_CHARS] + " […truncated]"
        if role == "user":
            parts.append(f"User: {content}")
        elif role == "assistant":
            parts.append(f"Assistant: {content}")
    return parts


def _validation_response_payload(
    parsed: Dict[str, Any],
    context: Dict[str, Any],
    *,
    is_fresh: bool,
) -> Dict[str, Any]:
    """Run reference validation, fold the structured errors into the
    returned suggestions, and return the response dict (without the
    is-diff wrapping). Used by both generate and refine."""
    ref_errors = validate_workflow_references(
        workflow=parsed,
        connections=context.get("connections", []),
        is_fresh=is_fresh,
    )
    # `suggestions` = refinements the AI can perform (clickable chips).
    # `prerequisites` = user-side setup steps (register a connection/source).
    # Reference errors are blocking, user-side fixes — they belong with the
    # prerequisites, NOT the clickable refinements, so the user never clicks a
    # "register a connection" chip that loops back to a no-op refine.
    suggestions = list(parsed.get("suggestions") or [])
    prerequisites = list(parsed.get("prerequisites") or [])
    if ref_errors:
        prerequisites = list(errors_to_suggestions(ref_errors)) + prerequisites

    # Deterministic runnability gaps (connection_picker empty + no inline
    # config). The reference validator intentionally skips these, so without
    # this a workflow can validate "clean" yet be silently unrunnable — the
    # exact theft-report failure. We surface them as prerequisites regardless
    # of whether the LLM remembered to mention them, and de-dupe against the
    # LLM's own prerequisite strings so the checklist isn't doubled up.
    setup_gaps = detect_setup_gaps(parsed, context.get("connections", []))
    if setup_gaps:
        gap_msgs = [g["message"] for g in setup_gaps]
        existing_lc = {p.strip().lower() for p in prerequisites}
        gap_msgs = [m for m in gap_msgs if m.strip().lower() not in existing_lc]
        prerequisites = gap_msgs + prerequisites

    return {
        "name": parsed.get("name", "AI Generated Workflow"),
        "description": parsed.get("description", ""),
        "icon": parsed.get("icon", "🤖"),
        "tags": parsed.get("tags", []),
        "nodes": parsed.get("nodes", []),
        "edges": parsed.get("edges", []),
        "variables": parsed.get("variables", {}),
        # True when the AI chose to ask instead of guessing — or when it
        # returned no nodes but did leave a message (an implicit question).
        "needs_clarification": bool(parsed.get("needs_clarification"))
        or (not parsed.get("nodes") and bool((parsed.get("reply") or "").strip())),
        "reply": (parsed.get("reply") or "").strip(),
        "suggestions": suggestions,
        "prerequisites": prerequisites,
        "setup_gaps": setup_gaps,
        "validation": {
            "errors": ref_errors,
            "is_clean": len(ref_errors) == 0,
        },
    }


@router.post("/api/workflows/generate-workflow")
async def generate_workflow(request: Request, body: GenerateWorkflowRequest):
    """Use AI to generate a complete workflow definition from a natural
    language description.

    The system prompt is built per-request and includes:
      • the AI-visible node palette from the live registry
      • the caller's saved connections

    Output is reference-validated before return: every connection_id is
    checked against the caller's saved connections. Failures are surfaced as
    structured errors AND prepended to the human-readable suggestions list.
    """
    _enforce_ai_rate_limit(_require_workflow_access(request))
    user_id = get_secure_user_id(request)

    if not body.prompt or not body.prompt.strip():
        raise HTTPException(status_code=400, detail="Prompt is required")

    context = await _gather_request_context(request, user_id, query=body.prompt)
    system_prompt = _build_ai_system_prompt(context)

    user_message_parts = _format_conversation(body.conversation)
    user_message_parts.append(f"User: {body.prompt}")
    user_message = "\n\n".join(user_message_parts)

    try:
        parsed = _llm_author_with_repair(
            user_prompt=user_message,
            system_prompt=system_prompt,
            user_id=user_id,
            max_tokens=50000,
            parse=_parse_workflow_json,
        )
    except (json.JSONDecodeError, ValueError) as e:
        raise HTTPException(status_code=422, detail=f"AI generated invalid workflow JSON: {str(e)}")

    return _validation_response_payload(parsed, context, is_fresh=True)


def _diff_workflows(
    before: Dict[str, Any], after: Dict[str, Any],
) -> Dict[str, Any]:
    """Compute a node/edge-level patch from ``before`` → ``after``.

    Patch shape (the UI's diff applier consumes this):
        {
          "nodes_added":   [<node>, ...],
          "nodes_removed": ["<id>", ...],
          "nodes_updated": [{"id": ..., "type": ..., "label": ...,
                             "position": ..., "config": ...}, ...],
          "edges_added":   [<edge>, ...],
          "edges_removed": ["<edge_id>", ...],
          "variables":     {<full new variables dict, replace-all>}
        }
    A node is considered ``updated`` if its config/label/position/type
    differs but its id appears in both lists. Edges have no config
    beyond source/target/label, so we treat them as add-or-remove only.

    On top of the applier patch, human-review keys (additive — the UI
    applier ignores them):
        {
          "nodes_changed_fields": {"<id>": [{"path": "config.params.column",
                                             "before": ..., "after": ...}]},
          "variables_added":   {<name>: <value>},
          "variables_removed": {<name>: <value>},
          "variables_changed": [{"name": ..., "before": ..., "after": ...}],
        }
    """
    b_nodes = {n.get("id"): n for n in (before.get("nodes") or []) if n.get("id")}
    a_nodes = {n.get("id"): n for n in (after.get("nodes") or []) if n.get("id")}
    b_edges = {e.get("id") or _edge_key(e): e for e in (before.get("edges") or [])}
    a_edges = {e.get("id") or _edge_key(e): e for e in (after.get("edges") or [])}

    nodes_added = [a_nodes[i] for i in a_nodes if i not in b_nodes]
    nodes_removed = [i for i in b_nodes if i not in a_nodes]
    nodes_updated: List[Dict[str, Any]] = []
    nodes_changed_fields: Dict[str, List[Dict[str, Any]]] = {}
    for nid in a_nodes:
        if nid not in b_nodes:
            continue
        if _node_significantly_different(b_nodes[nid], a_nodes[nid]):
            nodes_updated.append(a_nodes[nid])
            changes = _field_changes(b_nodes[nid], a_nodes[nid])
            if changes:
                nodes_changed_fields[nid] = changes

    edges_added = [a_edges[k] for k in a_edges if k not in b_edges]
    edges_removed = [k for k in b_edges if k not in a_edges]

    b_vars = before.get("variables") or {}
    a_vars = after.get("variables") or {}
    variables_added = {k: a_vars[k] for k in a_vars if k not in b_vars}
    variables_removed = {k: b_vars[k] for k in b_vars if k not in a_vars}
    variables_changed = [
        {"name": k, "before": _review_value(b_vars[k]), "after": _review_value(a_vars[k])}
        for k in a_vars if k in b_vars and a_vars[k] != b_vars[k]
    ]

    return {
        "nodes_added": nodes_added,
        "nodes_removed": nodes_removed,
        "nodes_updated": nodes_updated,
        "edges_added": edges_added,
        "edges_removed": edges_removed,
        "variables": a_vars,
        "nodes_changed_fields": nodes_changed_fields,
        "variables_added": variables_added,
        "variables_removed": variables_removed,
        "variables_changed": variables_changed,
    }


# Values longer than this are truncated in review entries — the review panel
# shows WHAT changed, not the full payload (a code node's script can be KBs;
# the full value still travels in nodes_updated for the applier).
_REVIEW_VALUE_MAX_CHARS = 300


def _review_value(v: Any) -> Any:
    """A value as shown in the human review panel — JSON-safe and bounded."""
    if isinstance(v, str):
        if len(v) > _REVIEW_VALUE_MAX_CHARS:
            return v[:_REVIEW_VALUE_MAX_CHARS] + f"… (+{len(v) - _REVIEW_VALUE_MAX_CHARS} chars)"
        return v
    if isinstance(v, (dict, list)):
        text = json.dumps(v, default=str)
        if len(text) > _REVIEW_VALUE_MAX_CHARS:
            return text[:_REVIEW_VALUE_MAX_CHARS] + f"… (+{len(text) - _REVIEW_VALUE_MAX_CHARS} chars)"
        return v
    return v


def _field_changes(before_node: Dict[str, Any], after_node: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Field-level before→after entries for one updated node.

    Walks label/type and the config dict recursively, emitting dot-path
    entries ({"path": "config.params.column", "before": ..., "after": ...})
    so the review UI can say exactly what changed instead of dumping two
    whole node objects at the user.
    """
    changes: List[Dict[str, Any]] = []
    for key in ("type", "label"):
        if before_node.get(key) != after_node.get(key):
            changes.append({
                "path": key,
                "before": _review_value(before_node.get(key)),
                "after": _review_value(after_node.get(key)),
            })

    def _walk(b: Any, a: Any, path: str) -> None:
        if isinstance(b, dict) and isinstance(a, dict):
            for k in sorted(set(b) | set(a)):
                sub = f"{path}.{k}"
                if k not in b:
                    changes.append({"path": sub, "before": None, "after": _review_value(a[k])})
                elif k not in a:
                    changes.append({"path": sub, "before": _review_value(b[k]), "after": None})
                elif b[k] != a[k]:
                    _walk(b[k], a[k], sub)
            return
        # Scalars, lists, or type changes — one leaf entry. Lists are shown
        # whole: element-level list diffs read worse than before/after here.
        changes.append({"path": path, "before": _review_value(b), "after": _review_value(a)})

    b_cfg = before_node.get("config") or {}
    a_cfg = after_node.get("config") or {}
    if b_cfg != a_cfg:
        _walk(b_cfg, a_cfg, "config")
    return changes


def _edge_key(edge: Dict[str, Any]) -> str:
    return f"{edge.get('source','')}→{edge.get('target','')}@{edge.get('source_handle','') or edge.get('label','')}"


def _node_significantly_different(a: Dict[str, Any], b: Dict[str, Any]) -> bool:
    for k in ("type", "label", "config"):
        if a.get(k) != b.get(k):
            return True
    return False


@router.post("/api/workflows/generate-workflow/refine")
async def refine_workflow(request: Request, body: RefineWorkflowRequest):
    """Refine an existing AI-generated workflow based on user feedback.

    Returns a diff patch by default (``return_diff=True``) so the UI
    can apply nodes/edges added/removed/updated without wiping the
    user's manual edits between AI turns. Legacy clients can request
    the full workflow with ``return_diff=False`` in the body.
    """
    _enforce_ai_rate_limit(_require_workflow_access(request))
    user_id = get_secure_user_id(request)

    if not body.prompt or not body.prompt.strip():
        raise HTTPException(status_code=400, detail="Refinement prompt is required")

    context = await _gather_request_context(request, user_id, query=body.prompt)
    system_prompt = _build_ai_system_prompt(context)

    user_message_parts = _format_conversation(body.conversation)
    current_workflow_json = json.dumps(body.workflow, indent=2, default=str)
    user_message_parts.append(
        f"Current workflow JSON:\n{current_workflow_json}\n\n"
        f"User refinement request: {body.prompt}\n\n"
        f"Modify the workflow above based on the refinement request. "
        f"Return the COMPLETE updated workflow JSON (the server will "
        f"compute the diff before sending it to the canvas). Preserve "
        f"node ids the user already has unless the change is structural."
    )
    user_message = "\n\n".join(user_message_parts)

    try:
        parsed = _llm_author_with_repair(
            user_prompt=user_message,
            system_prompt=system_prompt,
            user_id=user_id,
            max_tokens=50000,
            parse=_parse_workflow_json,
        )
    except (json.JSONDecodeError, ValueError) as e:
        raise HTTPException(status_code=422, detail=f"AI generated invalid workflow JSON: {str(e)}")

    # ``is_fresh=False`` on refine so dept-flow internals that already
    # existed in the input workflow aren't flagged as E_FORBIDDEN.
    response = _validation_response_payload(parsed, context, is_fresh=False)

    if body.return_diff:
        diff = _diff_workflows(body.workflow, parsed)
        response["diff"] = diff
        # No-op guard: when a refine changes nothing, never return a silent
        # identical graph. Either the AI already explained why (reply set) —
        # keep it — or synthesise an explanation so the user isn't left
        # staring at an unchanged canvas wondering if it worked. The most
        # common cause is a refine request that's actually a user-side
        # prerequisite (e.g. "register a SQL connection").
        is_noop = not (
            diff.get("nodes_added") or diff.get("nodes_removed")
            or diff.get("nodes_updated") or diff.get("edges_added")
            or diff.get("edges_removed")
        )
        response["no_op"] = is_noop
        if is_noop and not response.get("reply"):
            prereqs = response.get("prerequisites") or []
            if prereqs:
                response["reply"] = (
                    "I didn't change the workflow because what's needed is a "
                    "setup step I can't do from here — see the checklist below. "
                    "Once that's done, tell me and I'll wire it in."
                )
            else:
                response["reply"] = (
                    "I didn't change anything — I couldn't tell what to adjust "
                    "from that request. Could you say which node or behaviour "
                    "you want changed?"
                )

    return response


@router.post("/api/workflows/ai-chat")
async def ai_chat(request: Request, body: AIChatRequest):
    """Agentic AI Assistant for the workflow builder (Server-Sent Events).

    Replaces the client-side generate/refine/edit-node routing: the model
    reads the user's message and ACTS — answers a question, returns a node's
    code, fixes one node, edits the workflow, or builds a new one — over a
    multi-round tool-calling loop.

    Streamed as SSE so the connection stays warm during the (possibly
    multi-minute) reasoning/tool rounds; a 15s heartbeat guarantees bytes keep
    flowing even during a long single LLM call. This is the fix for the old
    "Failed to fetch" (the single-shot path sent nothing for ~3 minutes and
    the gateway dropped the connection).

    SSE events (one JSON object per ``data:`` line):
      status     — round/tool progress (live working line)
      operation  — a PROPOSED node_edit or workflow change (UI shows Apply)
      validation — reference issues on a proposal
      done       — {message, operations}; terminal
      error      — {message}; terminal
    """
    from fastapi.responses import StreamingResponse
    from .ai_assistant import run_workflow_assistant

    claims = _require_workflow_access(request)
    _enforce_ai_rate_limit(claims)
    user_id = get_secure_user_id(request)
    user_email = claims.get("email") or ""

    if not body.prompt or not body.prompt.strip():
        raise HTTPException(status_code=400, detail="Prompt is required")

    context = await _gather_request_context(request, user_id, query=body.prompt)
    # So the assistant can fill the email node's `to` for "email me" requests.
    context["user_email"] = user_email or user_id
    conversation_block = "\n\n".join(_format_conversation(body.conversation))

    # Identity for the debug tools: run_workflow_test mints the workflow's org
    # execution token; the past-run tools scope reads to this workflow.
    wf_in = body.workflow or {}
    wf_id = wf_in.get("workflow_id") or wf_in.get("id")
    org_id = claims.get("org_id")
    dept_ids = list(claims.get("dept_ids") or [])

    async def _event_source():
        # Run the agent loop as a background task that pushes events onto a
        # queue; the generator drains it and emits a heartbeat whenever no
        # event has arrived for 15s, so the connection never goes idle.
        queue: asyncio.Queue = asyncio.Queue()
        _SENTINEL = object()

        async def _pump():
            try:
                async for event in run_workflow_assistant(
                    prompt=body.prompt,
                    workflow=body.workflow or {},
                    conversation_block=conversation_block,
                    focused_node_id=body.focused_node_id,
                    context=context,
                    user_id=user_id,
                    user_email=user_email,
                    workflow_id=wf_id,
                    org_id=org_id,
                    dept_ids=dept_ids,
                    author_email=user_email,
                ):
                    await queue.put(event)
            except Exception as exc:  # noqa: BLE001 — surface to the client
                logger.error("ai-chat pump failed: %s", exc, exc_info=True)
                await queue.put({"type": "error", "message": f"Assistant error: {exc}"})
            finally:
                await queue.put(_SENTINEL)

        task = asyncio.create_task(_pump())
        try:
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"  # SSE comment — keeps the connection warm
                    continue
                if event is _SENTINEL:
                    break
                yield f"data: {json.dumps(event, default=str)}\n\n"
        finally:
            if not task.done():
                task.cancel()

    return StreamingResponse(
        _event_source(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/api/workflows/edit-node")
async def edit_node_with_ai(request: Request, body: EditNodeRequest):
    """AI-edit ONE node in an existing workflow.

    The full workflow is sent for context but only the focused node's
    config is rewritten. Used by the per-node right-click 'Edit with AI'
    affordance. Returns the updated node + the reference errors that
    apply to it specifically.
    """
    _enforce_ai_rate_limit(_require_workflow_access(request))
    user_id = get_secure_user_id(request)
    if not body.prompt or not body.prompt.strip():
        raise HTTPException(status_code=400, detail="Prompt is required")
    if not body.node_id:
        raise HTTPException(status_code=400, detail="node_id is required")

    target_node: Optional[Dict[str, Any]] = None
    for n in body.workflow.get("nodes") or []:
        if n.get("id") == body.node_id:
            target_node = n
            break
    if target_node is None:
        raise HTTPException(
            status_code=404,
            detail=f"node_id '{body.node_id}' not found in the supplied workflow",
        )

    context = await _gather_request_context(request, user_id, query=body.prompt)

    # Focused system prompt — reuse the framing + context but explicitly
    # narrow the task. Same node-palette + catalogue + connections so
    # the model can reference real ids.
    focused_prompt = (
        _WORKFLOW_GEN_PROMPT_FRAMING
        + "\n\n"
        + render_ai_context_sections(context)
        + "\n\n"
        + "## Task — single-node edit\n"
        "You are editing exactly ONE node in an existing workflow. The full "
        "DAG is provided for context, but you MUST return only the updated "
        "node config (a JSON object), not the whole workflow.\n\n"
        "Output format:\n"
        '{ "node": { "id": "<same id>", "type": "<same or new type>", '
        '"label": "<string>", "position": {"x": ..., "y": ...}, '
        '"config": { ... } }, '
        '"rationale": "<one sentence>", "reply": "<clarifying question or '
        'explanation for the user, or empty>", "suggestions": ["...", "..."], '
        '"prerequisites": ["user-side setup step, or omit"] }\n\n'
        "Keep the same node id. Do not invent connection_ids, dataset_ids, "
        "or action_ids — only use values from the lists above. See the "
        "'Talking to the user' guidance above for reply / suggestions / "
        "prerequisites — if the edit needs a connection or source that "
        "doesn't exist yet, say so in `reply` and `prerequisites`, don't "
        "invent an id."
    )

    user_message_parts = _format_conversation(body.conversation)
    user_message_parts.append(
        f"Workflow for context:\n{json.dumps(body.workflow, indent=2, default=str)}\n\n"
        f"Focused node id: {body.node_id}\n"
        f"Focused node current value:\n{json.dumps(target_node, indent=2, default=str)}\n\n"
        f"User edit request: {body.prompt}"
    )
    user_message = "\n\n".join(user_message_parts)

    def _parse_node_edit(raw_text: str) -> Dict[str, Any]:
        text = raw_text.strip()
        if text.startswith("```"):
            lines = text.split("\n")
            text = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
        obj = json.loads(text)
        node_obj = obj.get("node") or {}
        if not isinstance(node_obj, dict) or node_obj.get("id") != body.node_id:
            raise ValueError(
                f"AI must return a node with id='{body.node_id}'; got {node_obj.get('id')!r}"
            )
        return obj

    try:
        parsed = _llm_author_with_repair(
            user_prompt=user_message,
            system_prompt=focused_prompt,
            user_id=user_id,
            max_tokens=16000,
            parse=_parse_node_edit,
        )
    except (json.JSONDecodeError, ValueError) as e:
        raise HTTPException(status_code=422, detail=f"AI returned invalid node JSON: {e}")
    new_node = parsed.get("node") or {}

    # Validate the returned node by splicing it into the workflow and
    # running the reference validator, then filtering errors to just
    # those mentioning this node id.
    spliced = dict(body.workflow)
    spliced_nodes = [
        new_node if n.get("id") == body.node_id else n
        for n in (body.workflow.get("nodes") or [])
    ]
    spliced["nodes"] = spliced_nodes
    ref_errors = validate_workflow_references(
        workflow=spliced,
        connections=context.get("connections", []),
        is_fresh=False,
    )
    node_errors = [e for e in ref_errors if e.get("node_id") == body.node_id]

    prerequisites = list(parsed.get("prerequisites") or [])
    if node_errors:
        prerequisites = list(errors_to_suggestions(node_errors)) + prerequisites

    return {
        "node": new_node,
        "rationale": parsed.get("rationale") or "",
        "reply": (parsed.get("reply") or "").strip(),
        "suggestions": parsed.get("suggestions") or [],
        "prerequisites": prerequisites,
        "validation": {
            "errors": node_errors,
            "is_clean": len(node_errors) == 0,
        },
    }


# ── Starter prompts for the unified AI Chat (Phase 3) ───────────────

# Replaces the separate "Templates" view in the workflow builder. The
# AI Chat panel fetches this list once when the canvas is empty and
# shows the prompts as one-click "Start with…" pills. Each entry seeds
# the chat input; the LLM does the actual authoring against the live
# catalogue + connections.
#
# These are generic enterprise automations an IT team builds for the
# company — finance, HR, claims, procurement. They are described in
# plain business terms; the LLM maps each step to concrete nodes
# (triggers, AI/rules steps, approvals, sinks) against the live
# catalogue. Keep them business-recognisable, not platform jargon.
_STARTER_PROMPTS: List[Dict[str, str]] = [
    {
        "label": "HR — job application screening",
        "prompt": (
            "For each new job application, evaluate the candidate against "
            "the role's qualification criteria and sort applicants by how "
            "well they fit. If the criteria are not met, write a rejection "
            "record to the SQL database. If the criteria are met, approve "
            "the application and pass it to the next hiring step for review."
        ),
    },
    {
        "label": "Finance — expense forgery analysis",
        "prompt": (
            "Analyse submitted company expense claims for signs of forgery "
            "or fraud — altered or duplicated receipts, amounts above "
            "policy, suspicious or new vendors. Flag suspect claims for a "
            "finance reviewer with the reason, and pass clean claims "
            "through for reimbursement. Record every decision for audit."
        ),
    },
    {
        "label": "Employee medical & HRA claims",
        "prompt": (
            "When an employee submits a medical or HRA reimbursement claim, "
            "validate the receipt and check it against policy limits and "
            "remaining balance. Auto-approve claims within policy, and "
            "escalate exceptions or missing documents to HR for review."
        ),
    },
    {
        "label": "Procurement — vendor invoice processing",
        "prompt": (
            "Ingest incoming vendor invoices, extract the line items, and "
            "match them against the corresponding purchase order. Flag "
            "mismatches or duplicates for a buyer to review, and queue clean, "
            "matched invoices for payment."
        ),
    },
    {
        "label": "Custom — describe your workflow",
        "prompt": "",
    },
]


@router.get("/api/workflows/starter-prompts")
async def starter_prompts(_: Request):
    """Return the list of seed prompts shown by the AI chat on an empty canvas."""
    return {"prompts": _STARTER_PROMPTS}


# ─── Connection Profiles ──────────────────────────────────────────────

def _mask_connection(doc: dict) -> dict:
    """Return connection doc with secrets masked for list/get responses.

    Masks every secret field (not just connection_string/url/headers) so the
    API never returns a stored credential — neither the old plaintext nor the
    encrypted ciphertext blob.
    """
    doc = _serialize(doc)
    for env_key in ("test", "prod"):
        env = doc.get(env_key, {})
        # connection_string / url get a short prefix peek for recognisability.
        for field in ("connection_string", "url"):
            val = env.get(field)
            if val:
                env[field] = val[:SECRET_MASK_LENGTH] + "••••••••" if len(val) > SECRET_MASK_LENGTH else "••••••••"
        # All other secret scalars are fully masked.
        for field in SECRET_FIELDS:
            if field in ("connection_string", "url"):
                continue
            if env.get(field):
                env[field] = "••••••••"
        if env.get("headers"):
            env["headers"] = {k: "••••••••" for k in env["headers"]}
    return doc


def _looks_masked_or_blank(val: Any) -> bool:
    """True if a value is blank or is a display mask (contains the ``•`` glyph).

    list/get responses mask secrets as ``••••••••``; no real credential contains
    that glyph, so its presence means the client echoed the mask rather than a
    fresh secret."""
    if not isinstance(val, str):
        return not val
    return (not val.strip()) or ("•" in val)


def _merge_env_preserving_secrets(
    incoming: Dict[str, Any], existing_encrypted: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Merge an incoming env config over the existing stored one, preserving any
    secret the client left blank or echoed back as the display mask.

    The list/get endpoints return secrets masked (``••••••••``). If the UI (or
    any API caller) sends a masked or blank secret back on update, re-encrypting
    it would store the MASK as the credential and silently break the connection
    on its next run. Here, every ``SECRET_FIELDS`` value (and ``headers``) that
    is blank or masked falls back to the previously stored, decrypted value;
    non-secret fields and freshly-typed secrets are taken from ``incoming``.
    """
    merged = dict(incoming or {})
    existing_plain = decrypt_env_config(existing_encrypted) if existing_encrypted else {}
    for field in SECRET_FIELDS:
        if _looks_masked_or_blank(merged.get(field)):
            prev = existing_plain.get(field)
            if prev:
                merged[field] = prev
            else:
                merged.pop(field, None)
    # Headers are masked per-key. An empty or all-masked headers dict means
    # "unchanged", so keep the stored headers (you can't blank-all via edit —
    # a rare case not worth risking silent header loss for).
    hdrs = merged.get("headers")
    if (not hdrs) or (
        isinstance(hdrs, dict) and all(_looks_masked_or_blank(v) for v in hdrs.values())
    ):
        prev_hdrs = existing_plain.get("headers")
        if prev_hdrs:
            merged["headers"] = prev_hdrs
    return merged


@router.post("/api/workflows/connections")
async def create_connection(request: Request, body: CreateConnectionRequest):
    """Create a new connection profile with test & prod configs.

    Workflow connections are an IT-managed surface — same role gate as
    workflows themselves.
    """
    _require_workflow_access(request)
    user_id = get_secure_user_id(request)
    db = _db()
    claims = _jwt_claims(request)

    # Product rule: at least one environment must be configured. Saving with
    # neither Test nor Production filled is invalid. Return a clean, readable
    # message (string detail) rather than a raw Pydantic validation structure.
    if not body.test.is_configured() and not body.prod.is_configured():
        raise HTTPException(
            status_code=422,
            detail="Please configure at least one environment: Test or Production.",
        )

    now = datetime.utcnow()
    # IT workflow connections are ORG-owned and shared across the IT team.
    # `user_id` is retained only as created-by provenance (the model documents
    # it as a non-authorization audit shadow); the org_id is the access scope.
    profile = ConnectionProfile(
        user_id=user_id,
        owner_type="org",
        owner_id=(claims.get("org_id") or "").strip(),
        org_id=(claims.get("org_id") or "").strip(),
        name=body.name,
        type=body.type,
        description=body.description,
        test=body.test,
        prod=body.prod,
        created_at=now,
        updated_at=now,
    )

    doc = profile.model_dump()
    # Encrypt secrets before storing, then fail-closed if anything secret would
    # still be persisted in cleartext.
    doc["test"] = encrypt_env_config(doc["test"])
    doc["prod"] = encrypt_env_config(doc["prod"])
    assert_encrypted_envelope(doc["test"])
    assert_encrypted_envelope(doc["prod"])

    await db["WorkflowConnections"].insert_one(doc)
    return {"connection_id": profile.connection_id, "message": "Connection created"}


@router.get("/api/workflows/connections")
async def list_connections(request: Request, type: Optional[str] = Query(None)):
    """List all connections for the ORG (secrets masked).

    The IT workflow surface is org-owned and shared across the IT team — every
    member with workflow access sees the same connections, matching how
    workflows and executions are scoped. `user_id` on a connection is only
    `created_by` provenance, never the access boundary.
    """
    claims = _require_workflow_access(request)
    org_id = claims.get("org_id") or ""
    db = _db()

    query: Dict[str, Any] = {"org_id": org_id}
    if type:
        normalized_type = "bucket" if str(type).strip().lower() == "s3" else str(type).strip().lower()
        query["type"] = normalized_type

    cursor = db["WorkflowConnections"].find(query).sort("created_at", -1)
    items = [_mask_connection(doc) async for doc in cursor]
    return {"connections": items}


@router.get("/api/workflows/connections/{connection_id}")
async def get_connection(request: Request, connection_id: str):
    """Get a single connection profile (secrets masked). Org-scoped."""
    claims = _require_workflow_access(request)
    org_id = claims.get("org_id") or ""
    db = _db()

    doc = await db["WorkflowConnections"].find_one(
        {"connection_id": connection_id, "org_id": org_id}
    )
    if not doc:
        raise HTTPException(status_code=404, detail="Connection not found")
    return _mask_connection(doc)


@router.put("/api/workflows/connections/{connection_id}")
async def update_connection(request: Request, connection_id: str, body: UpdateConnectionRequest):
    """Update a connection profile (org-scoped). Only provided fields change."""
    claims = _require_workflow_access(request)
    org_id = claims.get("org_id") or ""
    db = _db()

    doc = await db["WorkflowConnections"].find_one(
        {"connection_id": connection_id, "org_id": org_id}
    )
    if not doc:
        raise HTTPException(status_code=404, detail="Connection not found")

    updates: Dict[str, Any] = {"updated_at": datetime.utcnow()}
    if body.name is not None:
        updates["name"] = body.name
    if body.description is not None:
        updates["description"] = body.description
    if body.test is not None:
        merged_test = _merge_env_preserving_secrets(body.test.model_dump(), doc.get("test"))
        updates["test"] = encrypt_env_config(merged_test)
        assert_encrypted_envelope(updates["test"])
    if body.prod is not None:
        merged_prod = _merge_env_preserving_secrets(body.prod.model_dump(), doc.get("prod"))
        updates["prod"] = encrypt_env_config(merged_prod)
        assert_encrypted_envelope(updates["prod"])

    await db["WorkflowConnections"].update_one(
        {"connection_id": connection_id, "org_id": org_id},
        {"$set": updates},
    )

    # Invalidate cached schema so the next agent run re-discovers it
    try:
        from .schema_cache import invalidate_schema
        await invalidate_schema(connection_id)
    except Exception:
        pass  # non-critical

    return {"message": "Connection updated"}


@router.delete("/api/workflows/connections/{connection_id}")
async def delete_connection(request: Request, connection_id: str):
    """Delete a connection profile (org-scoped — any IT-team member may remove)."""
    claims = _require_workflow_access(request)
    org_id = claims.get("org_id") or ""
    db = _db()

    result = await db["WorkflowConnections"].delete_one(
        {"connection_id": connection_id, "org_id": org_id}
    )
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Connection not found")

    # Invalidate cached schema
    try:
        from .schema_cache import invalidate_schema
        await invalidate_schema(connection_id)
    except Exception:
        pass  # non-critical

    return {"message": "Connection deleted"}


def _sanitize_conn_error(exc: Exception) -> str:
    """Strip credentials from a connection-test error before returning it.

    Driver exceptions (SQLAlchemy / pymongo / paramiko / smtplib) routinely
    embed the full DSN — including the password — in ``str(exc)``. Scrub
    URI credentials and password-like ``key=value`` pairs so a test
    response can never leak a secret.
    """
    import re as _re
    msg = str(exc)
    # scheme://user:password@host  →  scheme://***@host
    msg = _re.sub(
        r'([a-zA-Z][a-zA-Z0-9+.\-]*://)[^/\s:@]+:[^/\s@]+@', r'\1***@', msg
    )
    # password= / pwd= / secret= / api_key= / token= …  →  key=***
    msg = _re.sub(
        r'(?i)\b(password|passwd|pwd|secret|api[_-]?key|token|'
        r'access[_-]?key|secret[_-]?key)\s*[=:]\s*\S+',
        r'\1=***', msg,
    )
    return msg[:500]


async def _run_test(conn_type: str, env_config: dict) -> dict:
    """Helper to perform the actual connectivity check for various types."""
    try:
        if conn_type == "sql":
            import sqlalchemy
            conn_str = env_config.get("connection_string", "")
            try:
                # Use a short timeout for the initial connection check
                engine = sqlalchemy.create_engine(conn_str, pool_pre_ping=True)
                with engine.connect() as conn:
                    # Basic connectivity check
                    conn.execute(sqlalchemy.text("SELECT 1"))
                engine.dispose()
            except ImportError as e:
                msg = str(e)
                if "MySQLdb" in msg:
                    return {
                        "success": False,
                        "message": "Missing 'MySQLdb' driver. For MySQL/MariaDB, please use the 'mysql+pymysql://' prefix in your connection string."
                    }
                raise
        elif conn_type == "mongo":
            from motor.motor_asyncio import AsyncIOMotorClient
            client = AsyncIOMotorClient(
                env_config.get("connection_string", ""), 
                serverSelectionTimeoutMS=MONGO_TEST_TIMEOUT_MS
            )
            db_name = env_config.get("database", "test")
            await client[db_name].command("ping")
            client.close()
        elif conn_type == "api":
            import httpx
            async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_CONN_TEST) as client:
                resp = await client.get(
                    env_config.get("url", ""),
                    headers=env_config.get("headers", {}),
                )
                resp.raise_for_status()
        elif conn_type == "bucket":
            import boto3
            from botocore.config import Config
            s3 = boto3.client(
                's3',
                aws_access_key_id=env_config.get("access_key_id"),
                aws_secret_access_key=env_config.get("secret_access_key"),
                region_name=env_config.get("region", "us-east-1"),
                endpoint_url=env_config.get("endpoint_url") or None,
                config=Config(connect_timeout=5, retries={'max_attempts': 0})
            )
            s3.list_objects_v2(Bucket=env_config.get("bucket"), MaxKeys=1)
        elif conn_type == "sftp":
            protocol = env_config.get("protocol", "sftp").lower()
            host = env_config.get("host", "")
            port = int(env_config.get("port", 22 if protocol == "sftp" else 21))
            username = env_config.get("username", "")
            password = env_config.get("password", "")

            if protocol in ("ftp", "ftps"):
                from ftplib import FTP, FTP_TLS
                ftp = FTP_TLS() if protocol == "ftps" else FTP()
                try:
                    ftp.connect(host, port, timeout=10)
                    ftp.login(username or "anonymous", password or "")
                    if protocol == "ftps":
                        ftp.prot_p()
                    ftp.quit()
                except Exception:
                    try:
                        ftp.close()
                    except Exception:
                        # Announce loudly; the real connection error is
                        # re-raised below and must not be masked.
                        logger.warning(
                            "FTP connection test: error closing connection "
                            "during cleanup",
                            exc_info=True,
                        )
                    raise
            else:
                import paramiko
                ssh = paramiko.SSHClient()
                ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                try:
                    if env_config.get("private_key"):
                        import io
                        pkey = paramiko.RSAKey.from_private_key(io.StringIO(env_config.get("private_key")))
                        ssh.connect(host, port=port, username=username, pkey=pkey, timeout=10)
                    else:
                        ssh.connect(host, port=port, username=username, password=password, timeout=10)
                finally:
                    ssh.close()
        elif conn_type == "smtp":
            import smtplib
            host = env_config.get("host", "")
            port = int(env_config.get("smtp_port") or env_config.get("port") or 587)
            username = env_config.get("username", "")
            password = env_config.get("password", "")
            if port == 465:
                smtp = smtplib.SMTP_SSL(host, port, timeout=10)
            else:
                smtp = smtplib.SMTP(host, port, timeout=10)
                smtp.ehlo()
                smtp.starttls()
                smtp.ehlo()
            if username and password:
                smtp.login(username, password)
            smtp.quit()
        else:
            return {"success": False, "message": f"Unknown connection type: {conn_type}"}

        return {"success": True, "message": "Connection successful"}
    except Exception as e:
        # Never echo the raw driver error — it can contain the DSN/password.
        return {"success": False, "message": _sanitize_conn_error(e)}


@router.post("/api/workflows/connections/{connection_id}/test")
async def test_connection(request: Request, connection_id: str, env: str = Query("test")):
    """Test connectivity for an existing connection's test or prod environment.

    Org-scoped — any IT-team member may test any of the org's connections.
    """
    claims = _require_workflow_access(request)
    org_id = claims.get("org_id") or ""
    db = _db()

    doc = await db["WorkflowConnections"].find_one(
        {"connection_id": connection_id, "org_id": org_id}
    )
    if not doc:
        raise HTTPException(status_code=404, detail="Connection not found")

    env_config = decrypt_env_config(doc.get(env, {}))
    return await _run_test(doc.get("type", ""), env_config)


@router.post("/api/workflows/connections/test-draft")
async def test_draft_connection(request: Request, body: Dict[str, Any]):
    """Test connectivity for an unsaved connection configuration."""
    _require_workflow_access(request)
    return await _run_test(body.get("type", ""), body.get("config", {}))


# ─── Execution detail routes (static prefix — must precede {workflow_id}) ─

@router.get("/api/workflows/executions/{execution_id}")
async def get_execution(request: Request, execution_id: str):
    """Get full execution details including node results. Visible to any
    workflow-access user in the parent workflow's org."""
    _claims, doc = await _authorize_execution(request, execution_id)
    return _serialize(doc)


@router.get("/api/workflows/executions/{execution_id}/download/{filename}")
async def download_execution_file(request: Request, execution_id: str, filename: str):
    """Download a file produced by a file_download node. Visible to any
    workflow-access user in the parent workflow's org."""
    from fastapi.responses import Response
    import re
    import base64

    _claims, doc = await _authorize_execution(
        request, execution_id,
        projection={"execution_id": 1, "workflow_id": 1, "node_results": 1},
    )

    # Validate filename (prevent path traversal)
    if not re.match(r'^[\w\-. ]+$', filename):
        raise HTTPException(status_code=400, detail="Invalid filename")

    # Find the download node result with matching filename
    content_b64 = None
    content_type = "application/octet-stream"
    for nid, nr in (doc.get("node_results") or {}).items():
        meta = (nr.get("output_data") or {}).get("meta", {})
        if meta.get("download") and meta.get("filename") == filename:
            content_b64 = meta.get("content_b64")
            content_type = meta.get("content_type", content_type)
            break

    if not content_b64:
        raise HTTPException(status_code=404, detail="Download file not found in execution results")

    file_bytes = base64.b64decode(content_b64)

    return Response(
        content=file_bytes,
        media_type=content_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/api/workflows/executions/{execution_id}/status")
async def get_execution_status(request: Request, execution_id: str):
    """Quick status check (reads from Redis cache first). Visible to any
    workflow-access user in the parent workflow's org."""
    claims, doc = await _authorize_execution(
        request, execution_id,
        projection={"execution_id": 1, "workflow_id": 1, "status": 1,
                    "error": 1, "current_node": 1},
    )

    # Cache lookup AFTER auth, scoped by execution_id; we still pull the
    # fresh status fields from the DB row we just authorised so callers
    # see the canonical state.
    from citra_cache import get_cache_manager
    import json
    cache = get_cache_manager()
    cached = cache.get(f"workflow:exec:{execution_id}")
    if cached:
        try:
            data = json.loads(cached)
            return data
        except Exception:
            pass
    return _serialize(doc)


async def _enqueue_resume(
    *, request: Request, execution_id: str, approved: bool,
) -> dict:
    """Shared logic for approve / reject — enqueue a workflow.resume job.

    Any user with workflow access in the parent workflow's org may
    approve or reject. The original executor (`exec.user_id`) is still
    used as the resume identity for downstream auth, but the deciding
    actor is recorded separately for the audit trail.
    """
    claims, doc = await _authorize_execution(request, execution_id)
    doc.pop("_id", None)
    execution = WorkflowExecution(**doc)
    if execution.status != ExecutionStatus.PAUSED:
        raise HTTPException(status_code=400, detail="Execution is not paused")

    db = _db()
    resume_user_id = doc.get("user_id") or claims["user_id"]
    decider = claims["email"] or claims["user_id"]

    # Atomic compare-and-set PAUSED → RESUMING. This is the concurrency guard,
    # NOT the read-then-check above (which is only a fast-path 400): a UI
    # double-click or two officers approving the same item would otherwise both
    # pass the check, both enqueue a resume job, and both execute the governed
    # post-approval write. Only the update that actually flipped the doc
    # (modified_count == 1) proceeds to enqueue (prod-readiness HIGH #10).
    from datetime import datetime as _dt
    cas = await db["WorkflowExecutions"].update_one(
        {"execution_id": execution_id, "status": ExecutionStatus.PAUSED.value},
        {"$set": {
            "status": "resuming",
            "resumed_at": _dt.utcnow(),
            "resumed_by": decider,
            "resume_decision": "approved" if approved else "rejected",
        }},
    )
    if cas.modified_count != 1:
        # Lost the race — another approver/click already resolved this approval.
        # Idempotent no-op: do not enqueue a second resume.
        raise HTTPException(
            status_code=409,
            detail="This approval has already been resolved.",
        )

    try:
        from citra_queue import enqueue as _wq_enqueue  # type: ignore
        job_id = _wq_enqueue("workflow.resume", {
            "execution_id": execution_id,
            "workflow_id": execution.workflow_id,
            "user_id": resume_user_id,
            "approved": approved,
        }, tenant_id=resume_user_id, request_id=execution_id)
    except Exception as exc:  # noqa: BLE001
        logger.error(f"workflow.resume enqueue failed: {exc}")
        # Roll back the status flip — the resume didn't actually start.
        await db["WorkflowExecutions"].update_one(
            {"execution_id": execution_id},
            {"$set": {"status": ExecutionStatus.PAUSED.value}},
        )
        raise HTTPException(status_code=503, detail=f"Worker queue unavailable: {exc}")

    return {
        "execution_id": execution_id,
        "job_id": job_id,
        "status": "resuming",
    }


@router.post("/api/workflows/executions/{execution_id}/approve")
async def approve_execution(request: Request, execution_id: str):
    """Approve a paused (human-approval) execution.

    Any workflow-access user in the parent workflow's org may approve —
    not just the user who initiated the run. The decider is logged on
    the execution row for audit.
    """
    return await _enqueue_resume(
        request=request, execution_id=execution_id, approved=True,
    )


@router.post("/api/workflows/executions/{execution_id}/reject")
async def reject_execution(request: Request, execution_id: str):
    """Reject a paused execution. Same auth as /approve."""
    return await _enqueue_resume(
        request=request, execution_id=execution_id, approved=False,
    )


@router.post("/api/workflows/executions/{execution_id}/cancel")
async def cancel_execution(request: Request, execution_id: str):
    """Request cancellation of a non-terminal execution.

    Cooperative, NOT a forced kill. For a running/resuming/pending run we set
    a Redis flag (plus a durable cancel_requested marker on the Mongo doc) that
    the executor polls at each node boundary, then stops the run as CANCELLED
    without tearing a node's writes half-way — so a long-running node finishes
    or times out, but no further nodes start. A run PAUSED for approval has no
    worker attached, so it is flipped to CANCELLED directly via compare-and-set.

    Same auth as approve/reject: any workflow-access user in the parent
    workflow's org. The decider is recorded for audit.
    """
    claims, doc = await _authorize_execution(request, execution_id)
    status = str(doc.get("status") or "").lower()
    decider = claims["email"] or claims["user_id"]

    terminal = {
        ExecutionStatus.COMPLETED.value, ExecutionStatus.FAILED.value,
        ExecutionStatus.TIMED_OUT.value, ExecutionStatus.CANCELLED.value,
    }
    if status in terminal:
        raise HTTPException(
            status_code=409,
            detail=f"Execution already finished (status={status}); nothing to cancel.",
        )

    from datetime import datetime as _dt
    db = _db()
    now = _dt.utcnow()
    cancel_flag_key = f"workflow:cancel:{execution_id}"
    progress_key = f"workflow:exec:{execution_id}"

    # Durable marker FIRST — survives a worker restart and is honoured by the
    # crash-resume sweep, so a cancelled run can never be resurrected even if
    # the Redis flag expires before a stalled worker is reaped.
    await db["WorkflowExecutions"].update_one(
        {"execution_id": execution_id},
        {"$set": {
            "cancel_requested": True,
            "cancel_requested_by": decider,
            "cancel_requested_at": now,
        }},
    )

    # Raise the cooperative flag the executor polls — for EVERY live state,
    # including paused. If a concurrent approve wins the CAS race below and the
    # run resumes, the flag still stops it at its first post-approval node.
    flag_set = False
    try:
        from citra_cache import get_cache_manager
        get_cache_manager().set(cancel_flag_key, "1", ex=PROGRESS_CACHE_TTL)
        flag_set = True
    except Exception as exc:  # noqa: BLE001
        logger.error("Cancel: failed to set cancel flag for %s: %s", execution_id, exc)
        # A running/resuming/pending run is only reachable via the Redis flag —
        # without it we genuinely cannot stop the worker, so fail loud. A paused
        # run is resolved below via Mongo and does not need the flag.
        if status != ExecutionStatus.PAUSED.value:
            raise HTTPException(
                status_code=503,
                detail=f"Could not signal cancellation (cache unavailable): {exc}",
            )

    # A PAUSED run has no worker polling the flag — resolve it immediately via
    # CAS so a concurrent approve/reject/cancel can't also fire (one winner).
    if status == ExecutionStatus.PAUSED.value:
        cas = await db["WorkflowExecutions"].update_one(
            {"execution_id": execution_id, "status": ExecutionStatus.PAUSED.value},
            {"$set": {
                "status": ExecutionStatus.CANCELLED.value,
                "error": "Execution cancelled by user",
                "completed_at": now,
                "cancelled_by": decider,
            }},
        )
        if getattr(cas, "modified_count", 0) == 1:
            # The status endpoint prefers the Redis progress cache over the DB —
            # drop the stale cached row so it falls through to the fresh
            # CANCELLED doc, and clear the now-spent cancel flag.
            try:
                get_cache_manager().delete(progress_key, cancel_flag_key)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Cancel: cache cleanup failed for %s: %s", execution_id, exc)
            logger.info("Execution %s CANCELLED (was paused) by %s", execution_id, decider)
            return {"execution_id": execution_id, "status": "cancelled"}
        # Lost the CAS: the run was just approved/rejected. If the flag is set,
        # the resumed run will stop at its next node boundary.
        if flag_set:
            logger.info("Cancel raced a resume for %s; flag set, run will stop", execution_id)
            return {"execution_id": execution_id, "status": "cancelling"}
        raise HTTPException(
            status_code=409,
            detail="This execution was just resolved by another action.",
        )

    logger.info("Cancel requested for execution %s by %s (status=%s)",
                execution_id, decider, status)
    return {"execution_id": execution_id, "status": "cancelling"}


# ─── Approval Queue ───────────────────────────────────────────────────

async def _approval_filter_for_org(claims: Dict[str, Any]) -> Dict[str, Any]:
    """Build a Mongo filter matching approvals whose parent workflow is
    in the caller's org. Resolves workflow_ids once and uses an $in clause."""
    if Roles.SUPER_ADMIN in set(claims.get("roles") or []):
        return {}
    db = _db()
    wf_cursor = db["Workflows"].find(
        {"org_id": claims["org_id"]}, {"workflow_id": 1, "_id": 0},
    )
    wf_ids = [d["workflow_id"] async for d in wf_cursor if d.get("workflow_id")]
    if not wf_ids:
        return {"_no_match_": True}
    return {"workflow_id": {"$in": wf_ids}}


@router.get("/api/workflows/approvals")
async def list_pending_approvals(request: Request):
    """List pending approvals across every workflow in the caller's org.
    Any workflow-access user sees the org-wide queue."""
    claims = _require_workflow_access(request)
    db = _db()
    base = await _approval_filter_for_org(claims)
    if base.get("_no_match_"):
        return {"approvals": [], "total": 0}
    query = {**base, "resolution": None}
    cursor = db["WorkflowApprovals"].find(query).sort("created_at", -1)
    items = [_serialize(doc) async for doc in cursor]
    return {"approvals": items, "total": len(items)}


@router.get("/api/workflows/approvals/all")
async def list_all_approvals(request: Request, skip: int = Query(default=0, ge=0), limit: int = Query(default=PAGE_DEFAULT_APPROVALS, ge=1, le=PAGE_MAX)):
    """List every approval (resolved + pending) across every workflow in
    the caller's org. Used for audit."""
    claims = _require_workflow_access(request)
    db = _db()
    base = await _approval_filter_for_org(claims)
    if base.get("_no_match_"):
        return {"approvals": [], "total": 0}
    cursor = db["WorkflowApprovals"].find(base).sort("created_at", -1).skip(skip).limit(limit)
    items = [_serialize(doc) async for doc in cursor]
    total = await db["WorkflowApprovals"].count_documents(base)
    return {"approvals": items, "total": total}


# ─── Webhook Trigger ───────────────────────────────────────────────────

@router.post("/api/workflows/webhook/{webhook_token}")
async def webhook_trigger(webhook_token: str, request: Request):
    """Receive external webhook and trigger the matching deployed workflow.
    No JWT required — auth is via the unique webhook_token."""

    if not _check_rate_limit(f"webhook:{webhook_token}", RATE_LIMIT_WEBHOOK):
        raise HTTPException(status_code=429, detail="Webhook rate limit exceeded")

    db = _db()

    wf_doc = await db["Workflows"].find_one({
        "webhook_token": webhook_token,
        "status": WorkflowStatus.DEPLOYED.value,
        "is_active": True,
    })
    # Grace period: accept recently-rotated previous token (5 min window)
    if not wf_doc:
        wf_doc = await db["Workflows"].find_one({
            "previous_webhook_token": webhook_token,
            "status": WorkflowStatus.DEPLOYED.value,
            "is_active": True,
        })
        if wf_doc:
            rotated_at = wf_doc.get("token_rotated_at")
            if rotated_at and (datetime.utcnow() - rotated_at).total_seconds() > WEBHOOK_TOKEN_GRACE_PERIOD:
                wf_doc = None  # grace period expired
    if not wf_doc:
        raise HTTPException(status_code=404, detail="Webhook not found or workflow not deployed")

    # ── HMAC signature verification (MANDATORY) ──
    # A deployed webhook must never be triggerable by knowledge of the URL
    # token alone. Workflows deployed before HMAC was mandatory carry no
    # webhook_secret — reject them (fail closed) until re-deployed.
    webhook_secret = wf_doc.get("webhook_secret")
    raw_body = await request.body()
    if not webhook_secret:
        raise HTTPException(
            status_code=401,
            detail="This webhook has no signing secret — re-deploy the "
                   "workflow to provision one. Unsigned calls are rejected.",
        )
    sig_header = request.headers.get("X-Webhook-Signature", "")
    ts_header = request.headers.get("X-Webhook-Timestamp", "")

    if not sig_header:
        raise HTTPException(status_code=401, detail="Missing X-Webhook-Signature header")

    # Replay protection: reject timestamps older than 5 minutes
    try:
        ts = int(ts_header)
        if abs(time.time() - ts) > WEBHOOK_REPLAY_WINDOW:
            raise HTTPException(status_code=401, detail="Webhook timestamp expired")
    except (ValueError, TypeError):
        raise HTTPException(status_code=401, detail="Invalid or missing X-Webhook-Timestamp")

    # Compute expected HMAC-SHA256
    signing_payload = f"{ts_header}.".encode() + raw_body
    expected = hmac.new(
        webhook_secret.encode(), signing_payload, hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(expected, sig_header):
        raise HTTPException(status_code=401, detail="Invalid webhook signature")

    wf_doc.pop("_id", None)
    workflow = WorkflowDefinition(**wf_doc)

    # Parse incoming payload as trigger data. An empty body is a valid
    # no-payload trigger ({}); a non-empty body that is NOT valid JSON is a
    # caller error — reject it loudly with 400 rather than silently running
    # the workflow with an empty trigger payload.
    if raw_body.strip():
        try:
            payload = json.loads(raw_body)
        except (ValueError, json.JSONDecodeError) as exc:
            raise HTTPException(
                status_code=400,
                detail=f"Webhook body is not valid JSON: {exc}",
            ) from exc
    else:
        payload = {}

    # Webhook triggers have no caller JWT — mint an org-scoped system
    # token from the workflow doc so AI agent nodes can authenticate to
    # dept-MCPs with per-source filtering. Identity is bound to the
    # workflow's org, not its author, so the run survives the author
    # leaving the org.
    #
    # This token is REQUIRED for any agent/dept-MCP node to authenticate.
    # If minting fails we must NOT silently continue with no token — the run
    # would either bypass auth or fail deep inside a node with an opaque
    # error. Fail loud here so the cause is obvious.
    from citra_auth import mint_workflow_org_token
    webhook_jwt = mint_workflow_org_token(
        workflow_id=workflow.workflow_id,
        org_id=workflow.org_id,
        dept_ids=workflow.dept_ids,
        author_email=workflow.author_email or workflow.user_id,
    )
    if not webhook_jwt:
        raise HTTPException(
            status_code=500,
            detail="Failed to mint workflow system token for webhook trigger; "
                   "refusing to run the workflow without an auth context.",
        )

    # Kill switch: block webhook-triggered runs when a halt covers this
    # workflow's scope. Checked AFTER HMAC verification so an unsigned caller
    # can't probe halt state.
    await _enforce_workflow_not_halted(wf_doc)

    # Generate execution_id + pre-create the execution doc so the caller
    # can poll for status. Status starts at "queued"; Worker flips to
    # "running" when it picks up the job.
    import uuid as _uuid
    from datetime import datetime as _dt
    execution_id = str(_uuid.uuid4())
    db = _db()
    # Authoritative run environment set at deploy. Fall back to "prod" for
    # unmigrated deployed docs — the webhook only fires for DEPLOYED workflows,
    # which are live by definition (prod-readiness HIGH #6).
    run_env = getattr(workflow, "run_environment", None) or "prod"
    await db["WorkflowExecutions"].insert_one({
        "execution_id": execution_id,
        "workflow_id": workflow.workflow_id,
        "user_id": workflow.user_id,
        "status": "queued",
        "trigger_data": payload,
        "trigger_type": "webhook",
        "environment": run_env,
        "started_at": _dt.utcnow(),
        "node_results": {},
        "current_node": None,
        "error": None,
    })

    # Enqueue to Citra-Worker. The webhook now returns immediately with
    # 202 Accepted semantics — external callers should poll
    # /api/workflows/executions/{id}/status for completion. Synchronous
    # blocking on the webhook request was a stability risk (a long
    # workflow held the gunicorn worker open for the entire run).
    try:
        from citra_queue import enqueue as _wq_enqueue  # type: ignore
        job_id = _wq_enqueue("workflow.run", {
            "workflow_id": workflow.workflow_id,
            "user_id": workflow.user_id,
            "trigger_data": payload,
            "execution_id": execution_id,
            "environment": run_env,
            "jwt_token": webhook_jwt,
        }, tenant_id=workflow.user_id, request_id=execution_id)
    except Exception as exc:  # noqa: BLE001
        logger.error(f"webhook enqueue failed: {exc}")
        await db["WorkflowExecutions"].update_one(
            {"execution_id": execution_id},
            {"$set": {
                "status": "failed",
                "error": f"failed to enqueue job: {exc}",
                "completed_at": _dt.utcnow(),
            }},
        )
        raise HTTPException(status_code=503, detail=f"Worker queue unavailable: {exc}")

    return {
        "execution_id": execution_id,
        "job_id": job_id,
        "status": "queued",
    }


# ─── Templates (static prefix — must precede {workflow_id}) ────────────

@router.get("/api/workflows/templates")
async def list_templates(request: Request):
    """Return available workflow templates (system + user's personal templates)."""
    _require_workflow_access(request)
    user_id = get_secure_user_id(request)
    db = _db()

    # System templates
    templates = _get_system_templates()
    listing = []
    for t in templates:
        listing.append({
            "template_id": t["template_id"],
            "name": t["name"],
            "description": t["description"],
            "category": t["category"],
            "icon": t["icon"],
            "tags": t["tags"],
            "node_count": len(t["nodes"]),
            "is_system": True,
        })

    # User's personal templates
    cursor = db["UserTemplates"].find({"user_id": user_id}).sort("created_at", -1)
    async for doc in cursor:
        doc = _serialize(doc)
        listing.append({
            "template_id": doc["template_id"],
            "name": doc["name"],
            "description": doc.get("description", ""),
            "category": doc.get("category", "custom"),
            "icon": doc.get("icon", "⚙️"),
            "tags": doc.get("tags", []),
            "node_count": len(doc.get("nodes", [])),
            "is_system": False,
        })

    return {"templates": listing}


@router.get("/api/workflows/templates/{template_id}")
async def get_template(request: Request, template_id: str):
    """Get full template details (system or user template)."""
    _require_workflow_access(request)
    user_id = get_secure_user_id(request)
    db = _db()

    # Check system templates first
    templates = _get_system_templates()
    for t in templates:
        if t["template_id"] == template_id:
            return t

    # Check user templates
    user_tpl = await db["UserTemplates"].find_one({
        "template_id": template_id,
        "user_id": user_id,
    })
    if user_tpl:
        return _serialize(user_tpl)

    raise HTTPException(status_code=404, detail="Template not found")


@router.post("/api/workflows/templates/{template_id}/create")
async def create_from_template(request: Request, template_id: str):
    """Create a new workflow from a template (system or user template).
    Same role gate as POST /api/workflows; ownership is forced to org.
    """
    claims = _require_workflow_access(request)
    user_id = get_secure_user_id(request)
    db = _db()

    # Check system templates
    template = None
    for t in _get_system_templates():
        if t["template_id"] == template_id:
            template = t
            break

    # Check user templates
    if not template:
        user_tpl = await db["UserTemplates"].find_one({
            "template_id": template_id,
            "user_id": user_id,
        })
        if user_tpl:
            template = _serialize(user_tpl)

    if not template:
        raise HTTPException(status_code=404, detail="Template not found")

    workflow_id = str(uuid.uuid4())
    now = datetime.utcnow()
    org_id = claims["org_id"]

    doc = WorkflowDefinition(
        workflow_id=workflow_id,
        author_user_id=user_id,
        author_email=claims["email"] or user_id,
        author_at=now,
        owner_type="org",
        owner_id=org_id,
        user_id=user_id,
        org_id=org_id,
        dept_ids=list(claims["dept_ids"]),
        lifecycle_stage="org_managed",
        name=template["name"],
        description=template["description"],
        nodes=[NodeDefinition(**n) for n in template["nodes"]],
        edges=[EdgeDefinition(**e) for e in template["edges"]],
        variables=template.get("variables", {}),
        created_at=now,
        updated_at=now,
    ).model_dump()

    await db["Workflows"].insert_one(doc)
    return {"workflow_id": workflow_id, "message": f"Workflow created from template '{template['name']}'"}


# ─── User Templates (personal saved templates) ────────────────────────

@router.post("/api/workflows/user-templates")
async def save_user_template(request: Request, body: SaveUserTemplateRequest):
    """Save a workflow as a personal reusable template."""
    _require_workflow_access(request)
    user_id = get_secure_user_id(request)
    db = _db()

    if not body.name or not body.name.strip():
        raise HTTPException(status_code=400, detail="Template name is required")

    # Validate DAG if nodes/edges provided
    if body.nodes and body.edges:
        dag_errors = _validate_dag(body.nodes, body.edges)
        if dag_errors:
            raise HTTPException(status_code=400, detail=f"Invalid workflow graph: {'; '.join(dag_errors)}")

    template_id = str(uuid.uuid4())
    now = datetime.utcnow()

    doc = {
        "template_id": template_id,
        "user_id": user_id,
        "name": body.name.strip(),
        "description": body.description,
        "icon": body.icon,
        "category": body.category,
        "tags": body.tags,
        "nodes": [n.model_dump() for n in body.nodes],
        "edges": [e.model_dump() for e in body.edges],
        "variables": body.variables,
        "is_system": False,
        "created_at": now,
        "updated_at": now,
    }

    await db["UserTemplates"].insert_one(doc)
    return {"template_id": template_id, "message": f"Template '{body.name}' saved"}


@router.get("/api/workflows/user-templates")
async def list_user_templates(request: Request):
    """List the current user's saved templates."""
    _require_workflow_access(request)
    user_id = get_secure_user_id(request)
    db = _db()

    cursor = db["UserTemplates"].find(
        {"user_id": user_id},
    ).sort("created_at", -1)

    templates = []
    async for doc in cursor:
        doc = _serialize(doc)
        templates.append({
            "template_id": doc["template_id"],
            "name": doc["name"],
            "description": doc.get("description", ""),
            "category": doc.get("category", "custom"),
            "icon": doc.get("icon", "⚙️"),
            "tags": doc.get("tags", []),
            "node_count": len(doc.get("nodes", [])),
            "is_system": False,
            "created_at": doc.get("created_at"),
        })
    return {"templates": templates}


@router.delete("/api/workflows/user-templates/{template_id}")
async def delete_user_template(request: Request, template_id: str):
    """Delete a personal template."""
    _require_workflow_access(request)
    user_id = get_secure_user_id(request)
    db = _db()

    result = await db["UserTemplates"].delete_one({
        "template_id": template_id,
        "user_id": user_id,
    })

    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Template not found")
    return {"message": "Template deleted"}


# ─── Workflow CRUD ─────────────────────────────────────────────────────

def _validate_create_ownership(claims: Dict[str, Any], body: CreateWorkflowRequest) -> Dict[str, Any]:
    """Resolve ownership for a new workflow.

    Workflows are always owned by the org. Any owner_type / owner_id in
    the request body is ignored — the caller's org is the owner. Role
    gating has already happened in _require_workflow_access(); this
    function just produces the canonical ownership payload.
    """
    org_id = claims["org_id"]
    if not org_id:
        raise HTTPException(
            status_code=403,
            detail="workflow creation requires an org-scoped identity",
        )
    return {
        "owner_type": "org",
        "owner_id": org_id,
        "org_id": org_id,
        "dept_ids": list(body.dept_ids or claims["dept_ids"]),
    }


@router.post("/api/workflows")
async def create_workflow(request: Request, body: CreateWorkflowRequest):
    """Create a new workflow definition.

    Workflows are an IT-owned surface. The caller must hold one of:
    IT-workflow, org_admin, super_admin, or dept_admin scoped to the IT
    department. The workflow is always owned by the caller's org —
    owner_type/owner_id in the request body are ignored.

    On-behalf-of: when called by a platform service (e.g.
    smart-app-service publishing a Smart App for a BA), the system
    token carries ``on_behalf_of_user_id`` / ``on_behalf_of_email``
    claims. The workflow's ``author_user_id`` / ``author_email`` are
    then set from those claims, so audit trail and provenance reflect
    the actual BA, not the system identity. Required when the
    workflow_kind is ``smart_app_action``.
    """
    claims = _require_workflow_access(request)
    user_id = get_secure_user_id(request)
    db = _db()

    # Validate DAG structure
    dag_errors = _validate_dag(body.nodes, body.edges)
    if dag_errors:
        raise HTTPException(status_code=400, detail=f"Invalid workflow graph: {'; '.join(dag_errors)}")

    ownership = _validate_create_ownership(claims, body)

    # smart_app_action workflows MUST link to an app
    workflow_kind = body.workflow_kind or "ad_hoc"
    if workflow_kind == "smart_app_action" and not body.linked_smart_app_id:
        raise HTTPException(
            status_code=400,
            detail="workflow_kind='smart_app_action' requires linked_smart_app_id",
        )

    workflow_id = str(uuid.uuid4())
    now = datetime.utcnow()

    # Author resolution: prefer on_behalf_of_* claims (set by system tokens
    # from mint_system_workflow_token), so the BA is recorded as the
    # workflow's author even when smart-app-service made the call.
    on_behalf_user = (claims.get("on_behalf_of_user_id") or "").strip()
    on_behalf_email = (claims.get("on_behalf_of_email") or "").strip()
    author_user_id = on_behalf_user or user_id
    author_email = on_behalf_email or claims["email"] or user_id

    doc = WorkflowDefinition(
        workflow_id=workflow_id,
        author_user_id=author_user_id,
        author_email=author_email,
        author_at=now,
        owner_type="org",
        owner_id=ownership["owner_id"],
        # Legacy user_id field — used by the scheduler self-lookup as a
        # composite key. Use the actual author so cron job ↔ workflow doc
        # registration stays consistent.
        user_id=author_user_id,
        org_id=ownership["org_id"],
        dept_ids=ownership["dept_ids"],
        workflow_kind=workflow_kind,
        linked_smart_app_id=body.linked_smart_app_id,
        visibility=body.visibility or WorkflowVisibility(),
        notifications=body.notifications or WorkflowNotifications(),
        lifecycle_stage="org_managed",
        name=body.name,
        description=body.description,
        nodes=body.nodes,
        edges=body.edges,
        schedule=body.schedule,
        variables=body.variables or {},
        created_at=now,
        updated_at=now,
    ).model_dump()

    await db["Workflows"].insert_one(doc)
    return {
        "workflow_id": workflow_id,
        "owner_type": "org",
        "owner_id": ownership["owner_id"],
        "workflow_kind": workflow_kind,
        "message": "Workflow created",
    }


@router.get("/api/workflows")
async def list_workflows(
    request: Request,
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=PAGE_DEFAULT_WORKFLOWS, ge=1, le=PAGE_MAX),
    include_smart_app_action: bool = Query(default=False),
    linked_smart_app_id: Optional[str] = Query(default=None),
    search: Optional[str] = Query(default=None, max_length=100),
):
    """List IT-managed workflows in the caller's org.

    The default list EXCLUDES ``workflow_kind="smart_app_action"`` rows —
    those are implementation details of Smart Apps, managed via the Smart
    App's own UI, and would otherwise clutter the IT workflow builder
    surface. They remain visible via:
      - this endpoint with ``include_smart_app_action=true`` (debugging),
      - ``GET /api/admin/workflows`` (admin lens; always returns all kinds),
      - direct ``GET /api/workflows/{workflow_id}`` (when you know the id),
      - filtered by ``linked_smart_app_id=<app_id>`` (smart-app-service
        fetching a specific app's workflows).

    Caller must hold IT-workflow, org_admin, super_admin, or dept_admin
    scoped to the IT department; everyone else gets 403.
    """
    claims = _require_workflow_access(request)
    db = _db()

    PROJECT = {
        "workflow_id": 1, "name": 1, "description": 1, "status": 1,
        "created_at": 1, "updated_at": 1, "deployed_at": 1, "is_active": 1,
        "version": 1, "schedule": 1, "webhook_token": 1,
        "owner_type": 1, "owner_id": 1, "org_id": 1, "dept_ids": 1,
        "author_user_id": 1, "author_email": 1,
        "workflow_kind": 1, "linked_smart_app_id": 1, "visibility": 1,
        "node_count": {"$cond": {"if": {"$isArray": "$nodes"}, "then": {"$size": "$nodes"}, "else": 0}},
        "edge_count": {"$cond": {"if": {"$isArray": "$edges"}, "then": {"$size": "$edges"}, "else": 0}},
    }

    match: Dict[str, Any] = {"org_id": claims["org_id"], "is_active": {"$ne": False}}
    if linked_smart_app_id:
        # Explicit Smart App scope — return all kinds, do not exclude.
        match["linked_smart_app_id"] = linked_smart_app_id
    elif not include_smart_app_action:
        # Default IT view — hide Smart-App-generated workflows.
        match["workflow_kind"] = {"$ne": "smart_app_action"}

    # Optional name/description search. Layered on top of the org / kind /
    # smart-app filters above so it composes with them — and because it lives
    # in ``match`` it applies to BOTH the page query and the ``total`` count,
    # keeping the count label accurate for the filtered set. The term is
    # trimmed, length-capped (via Query max_length), and re.escape'd so user
    # input is matched literally, never interpreted as a regex.
    search_term = (search or "").strip()
    if search_term:
        pattern = re.escape(search_term)
        match["$or"] = [
            {"name": {"$regex": pattern, "$options": "i"}},
            {"description": {"$regex": pattern, "$options": "i"}},
        ]

    pipeline = [
        {"$match": match},
        {"$sort": {"updated_at": -1}},
        {"$skip": skip},
        {"$limit": limit},
        {"$project": PROJECT},
    ]
    items = [_serialize(doc) async for doc in db["Workflows"].aggregate(pipeline)]
    total = await db["Workflows"].count_documents(match)
    has_more = skip + len(items) < total
    return {"workflows": items, "total": total, "has_more": has_more, "scope": "org"}


@router.get("/api/workflows/{workflow_id}")
async def get_workflow(request: Request, workflow_id: str):
    """Get full workflow definition (visibility-checked)."""
    _ = get_secure_user_id(request)
    db = _db()

    doc = await db["Workflows"].find_one({"workflow_id": workflow_id})
    if not doc:
        raise HTTPException(status_code=404, detail="Workflow not found")
    denial = _check_workflow_action(doc, request, action="read")
    if denial:
        raise HTTPException(status_code=denial[0], detail=denial[1])
    return _serialize(doc)


@router.put("/api/workflows/{workflow_id}")
async def update_workflow(request: Request, workflow_id: str, body: UpdateWorkflowRequest):
    """Update workflow definition (nodes, edges, config)."""
    _ = get_secure_user_id(request)
    db = _db()

    # Authorize against the SA ownership model — NOT the legacy user_id
    # field. An SA admin/member or org_admin may edit a workflow owned by
    # their SA; the original author does NOT retain edit rights after the
    # workflow is transferred to another SA / dept / org.
    doc = await db["Workflows"].find_one({"workflow_id": workflow_id})
    if not doc:
        raise HTTPException(status_code=404, detail="Workflow not found")
    denial = _check_workflow_action(doc, request, action="edit")
    if denial:
        raise HTTPException(status_code=denial[0], detail=denial[1])
    # The workflow's owner identity — passed to the scheduler so a
    # scheduled run executes as the owner, not whoever last edited it.
    owner_user_id = doc.get("user_id") or ""

    # Optimistic-concurrency guard: reject a save built on a stale copy
    # (two tabs, or AI-apply racing a manual edit) rather than letting it
    # silently overwrite newer server state.
    if body.expected_version is not None and doc.get("version") != body.expected_version:
        raise HTTPException(
            status_code=409,
            detail=(
                f"This workflow changed since you loaded it "
                f"(you have v{body.expected_version}, current is "
                f"v{doc.get('version')}). Reload to get the latest, then "
                f"re-apply your changes."
            ),
        )

    # dept_source nomenclature + dept-scope authz enforcement removed 2026-08-08:
    # both validated Citra dept_sources / per-dept Milvus collections, and both
    # the sink nodes and the registry they guarded are gone (PORTING.md §1).

    if body.nodes is not None or body.edges is not None:
        nodes_for_validation = (
            [n.model_dump() for n in body.nodes]
            if body.nodes is not None
            else doc.get("nodes", [])
        )
        edges_for_validation = (
            [e.model_dump() for e in body.edges]
            if body.edges is not None
            else doc.get("edges", [])
        )
        dag_errors = _validate_dag(nodes_for_validation, edges_for_validation)
        if dag_errors:
            raise HTTPException(status_code=400, detail="; ".join(dag_errors))

    update_fields: Dict[str, Any] = {"updated_at": datetime.utcnow()}
    if body.name is not None:
        update_fields["name"] = body.name
    if body.description is not None:
        update_fields["description"] = body.description
    if body.nodes is not None:
        update_fields["nodes"] = [n.model_dump() for n in body.nodes]
    if body.edges is not None:
        update_fields["edges"] = [e.model_dump() for e in body.edges]
    if body.variables is not None:
        update_fields["variables"] = body.variables
    if body.schedule is not None:
        update_fields["schedule"] = body.schedule.model_dump()
    if body.notifications is not None:
        update_fields["notifications"] = body.notifications.model_dump()
    if body.max_run_llm_calls is not None:
        # A positive value sets a per-workflow per-run LLM-call cap; <= 0 clears
        # the override back to the global default. The value is clamped to the
        # admin hard-max at run time, so no validation is needed here.
        update_fields["max_run_llm_calls"] = (
            int(body.max_run_llm_calls) if body.max_run_llm_calls > 0 else None
        )

    result = await db["Workflows"].update_one(
        {"workflow_id": workflow_id},
        {"$set": update_fields, "$inc": {"version": 1}},
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Workflow not found")

    # If schedule was changed on a deployed workflow, hot-reload the cron job
    if body.schedule is not None:
        doc = await db["Workflows"].find_one({"workflow_id": workflow_id})
        if doc and doc.get("status") == WorkflowStatus.DEPLOYED.value:
            try:
                from .scheduler import scheduler_manager, WorkflowSchedulerManager
                schedule = doc.get("schedule", {})
                if schedule.get("enabled") and schedule.get("cron_expression"):
                    scheduler_manager.register_workflow(workflow_id, owner_user_id, schedule, workflow_name=doc.get("name", ""))
                    await WorkflowSchedulerManager.publish_schedule_refresh(
                        workflow_id, owner_user_id, schedule, workflow_name=doc.get("name", ""), action="register"
                    )
                    logger.info(f"⚡ Hot-reloaded cron schedule for deployed workflow {workflow_id}")
                else:
                    scheduler_manager.unregister_workflow(workflow_id)
                    await WorkflowSchedulerManager.publish_schedule_refresh(
                        workflow_id, owner_user_id, {}, action="unregister"
                    )
                    logger.info(f"⚡ Removed cron schedule for deployed workflow {workflow_id} (disabled)")
            except Exception as e:
                logger.warning(f"⚡ Failed to hot-reload schedule for {workflow_id}: {e}")

    return {"message": "Workflow updated"}


@router.delete("/api/workflows/{workflow_id}")
async def delete_workflow(request: Request, workflow_id: str):
    """Soft-delete a workflow."""
    user_id = get_secure_user_id(request)
    db = _db()

    doc = await db["Workflows"].find_one({"workflow_id": workflow_id})
    if not doc:
        raise HTTPException(status_code=404, detail="Workflow not found")
    denied = _check_workflow_action(doc, request, action="edit")
    if denied is not None:
        raise HTTPException(status_code=denied[0], detail=denied[1])

    result = await db["Workflows"].update_one(
        {"workflow_id": workflow_id},
        {"$set": {"is_active": False, "updated_at": datetime.utcnow()}},
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Workflow not found")

    # Remove cron schedule to prevent ghost jobs
    try:
        from .scheduler import scheduler_manager
        scheduler_manager.unregister_workflow(workflow_id)
    except Exception as e:
        logger.warning(f"⚡ Failed to unregister schedule on delete for {workflow_id}: {e}")

    return {"message": "Workflow deleted"}


@router.get("/api/admin/workflows")
async def admin_list_workflows(request: Request, limit: int = 200):
    """Admin audit list of every workflow in the caller's org.

    Under org-only ownership this returns the same set as GET /api/workflows
    — there's no separate "admin scope" any more. Kept for back-compat
    with UIs that still call this path; consider migrating them to
    GET /api/workflows.
    """
    claims = _require_workflow_access(request)
    roles = set(claims["roles"])
    db = _db()

    query: dict = {"is_active": {"$ne": False}}
    if Roles.SUPER_ADMIN not in roles:
        query["org_id"] = claims["org_id"]

    cur = db["Workflows"].find(query).limit(int(limit)).sort("updated_at", -1)
    out = []
    async for doc in cur:
        doc.pop("_id", None)
        out.append({
            "workflow_id": doc.get("workflow_id"),
            "name": doc.get("name"),
            "owner_type": doc.get("owner_type"),
            "owner_id": doc.get("owner_id"),
            "org_id": doc.get("org_id"),
            "dept_ids": doc.get("dept_ids") or [],
            "status": doc.get("status"),
            "updated_at": doc.get("updated_at"),
            "version": doc.get("version"),
        })
    return {"count": len(out), "workflows": out}


# ── Workflow Automation Control (kill switches + schedule control) ───────────
class WorkflowSchedulePatch(BaseModel):
    enabled: Optional[bool] = None
    cron_expression: Optional[str] = None


class WorkflowHaltRequest(BaseModel):
    scope_type: str  # global | org | dept
    scope_id: Optional[str] = None
    enabled: bool
    reason: Optional[str] = ""


def _wf_control_col():
    return _db()["workflow_control"]


async def _enforce_workflow_not_halted(doc: dict) -> None:
    """Raise 503 if a halt (global/org/dept) covers this workflow. Used on the
    manual /execute and webhook run paths — the scheduler has its own check."""
    from . import workflow_control
    org = doc.get("org_id") or ""
    dept_tokens = [f"{org}:{str(d).lower()}" for d in (doc.get("dept_ids") or [])]
    halt = await workflow_control.get_halt(_wf_control_col(), org_id=org, dept_tokens=dept_tokens)
    if halt:
        raise HTTPException(
            status_code=503,
            detail=f"Workflow runs are halted at {halt.get('scope_type')} scope: {halt.get('reason') or 'paused by an administrator'}",
        )


@router.get("/api/admin/workflow-automation")
async def admin_workflow_automation(request: Request, limit: int = 500):
    """Control-panel list: every workflow in scope with schedule, enabled,
    status, next_run + last_run, plus an incidents feed. super_admin → all orgs;
    else the caller's org."""
    from datetime import datetime as _dt, timezone as _tz
    from . import workflow_control
    claims = _require_workflow_access(request)
    roles = set(claims.get("roles") or [])
    db = _db()
    query: dict = {"is_active": {"$ne": False}}
    if Roles.SUPER_ADMIN not in roles:
        query["org_id"] = claims["org_id"]
    now = _dt.now(_tz.utc)

    def _next(cron):
        try:
            if cron:
                from croniter import croniter
                return croniter(cron, now).get_next(_dt).isoformat()
        except Exception:
            return None
        return None

    out = []
    async for doc in db["Workflows"].find(query).limit(int(limit)).sort("updated_at", -1):
        wid = doc.get("workflow_id")
        sched = doc.get("schedule") or {}
        cron = sched.get("cron_expression")
        last = None
        try:
            lr = await db["WorkflowExecutions"].find({"workflow_id": wid}).sort("started_at", -1).to_list(length=1)
            if lr:
                last = {"at": lr[0].get("started_at"), "status": lr[0].get("status"), "trigger": lr[0].get("trigger_type")}
        except Exception:
            last = None
        out.append({
            "workflow_id": wid, "name": doc.get("name"),
            "status": doc.get("status"), "org_id": doc.get("org_id"),
            "dept_ids": doc.get("dept_ids") or [],
            "schedule_enabled": bool(sched.get("enabled")),
            "cron_expression": cron,
            "next_run": _next(cron) if (sched.get("enabled") and cron) else None,
            "last_run": last,
            "deployed": doc.get("status") == WorkflowStatus.DEPLOYED.value,
        })
    incidents = []
    try:
        wids = [w["workflow_id"] for w in out]
        name_by = {w["workflow_id"]: w["name"] for w in out}
        recent = await db["WorkflowExecutions"].find(
            {"workflow_id": {"$in": wids}, "status": {"$in": ["failed", "error"]}}
        ).sort("started_at", -1).to_list(length=25)
        for r in recent:
            err = r.get("error")
            incidents.append({
                "workflow_id": r.get("workflow_id"), "name": name_by.get(r.get("workflow_id")),
                "status": r.get("status"), "at": r.get("started_at"),
                "error": err.get("message") if isinstance(err, dict) else err,
            })
    except Exception:
        incidents = []
    return {"workflows": out, "incidents": incidents}


@router.patch("/api/workflows/{workflow_id}/schedule")
async def patch_workflow_schedule(request: Request, workflow_id: str, body: WorkflowSchedulePatch):
    """Start/stop a workflow's schedule or change its cron — from the control
    panel. Updates schedule.enabled / cron_expression and hot-reloads the
    scheduler (register/unregister + pub/sub refresh)."""
    claims = _require_workflow_access(request)
    db = _db()
    doc = await db["Workflows"].find_one({"workflow_id": workflow_id})
    if not doc:
        raise HTTPException(status_code=404, detail="Workflow not found")
    if Roles.SUPER_ADMIN not in set(claims.get("roles") or []) and doc.get("org_id") != claims.get("org_id"):
        raise HTTPException(status_code=403, detail="workflow is not in your org")
    sched = dict(doc.get("schedule") or {})
    if body.enabled is not None:
        sched["enabled"] = bool(body.enabled)
    if body.cron_expression is not None:
        sched["cron_expression"] = body.cron_expression.strip() or None
    await db["Workflows"].update_one(
        {"workflow_id": workflow_id}, {"$set": {"schedule": sched}, "$inc": {"version": 1}}
    )
    if doc.get("status") == WorkflowStatus.DEPLOYED.value:
        try:
            from .scheduler import scheduler_manager, WorkflowSchedulerManager
            owner_user_id = doc.get("user_id") or doc.get("owner_id") or ""
            if sched.get("enabled") and sched.get("cron_expression"):
                scheduler_manager.register_workflow(workflow_id, owner_user_id, sched, workflow_name=doc.get("name", ""))
                await WorkflowSchedulerManager.publish_schedule_refresh(
                    workflow_id, owner_user_id, sched, workflow_name=doc.get("name", ""), action="register"
                )
            else:
                scheduler_manager.unregister_workflow(workflow_id)
                await WorkflowSchedulerManager.publish_schedule_refresh(
                    workflow_id, owner_user_id, {}, action="unregister"
                )
        except Exception as e:
            logger.warning(f"⚡ schedule hot-reload failed for {workflow_id}: {e}")
    return {"ok": True, "schedule": sched}


@router.get("/api/admin/workflow-halt")
async def get_workflow_halt(request: Request):
    """Active workflow halts (global/org/dept)."""
    claims = _require_workflow_access(request)
    from . import workflow_control
    controls = await workflow_control.list_controls(_wf_control_col())
    # Scope: super sees all; everyone else sees only global + their own org /
    # dept controls — no leaking other tenants' halt reasons/actors.
    roles = set(claims.get("roles") or [])
    if "super_admin" not in roles:
        org = claims.get("org_id") or ""
        dept_prefix = f"{org}:"
        controls = [
            c for c in controls
            if c.get("scope_type") == "global"
            or (c.get("scope_type") == "org" and c.get("scope_id") == org)
            or (c.get("scope_type") == "dept" and str(c.get("scope_id") or "").startswith(dept_prefix))
        ]
    return {"controls": controls}


@router.post("/api/admin/workflow-halt")
async def set_workflow_halt(request: Request, body: WorkflowHaltRequest):
    """Stop-all / resume-all (global) or org/dept halt for the workflow engine.
    global → super_admin; org → org_admin(own)/super; dept → IT dept_admin of
    that org-qualified dept (or org/super)."""
    from . import workflow_control
    claims = _require_workflow_access(request)
    roles = set(claims.get("roles") or [])
    org_id = claims.get("org_id")
    dept_ids = [str(d).lower() for d in (claims.get("dept_ids") or [])]
    is_super = Roles.SUPER_ADMIN in roles
    is_org = Roles.ORG_ADMIN in roles
    st = body.scope_type
    if st == "global":
        if not is_super:
            raise HTTPException(status_code=403, detail="global halt requires super_admin")
        scope_id = None
    elif st == "org":
        if not (is_super or is_org):
            raise HTTPException(status_code=403, detail="org halt requires org_admin")
        if not is_super and body.scope_id not in (None, org_id):
            raise HTTPException(status_code=403, detail="can only halt your own org")
        scope_id = body.scope_id or org_id
    elif st == "dept":
        sid = body.scope_id or ""
        if ":" not in sid:
            raise HTTPException(status_code=400, detail="dept scope_id must be '<org_id>:<dept_id>'")
        d_org, d_dept = sid.split(":", 1)
        if is_super or (is_org and d_org == org_id) or (Roles.DEPT_ADMIN in roles and d_org == org_id and d_dept.lower() in dept_ids):
            # Store the dept segment NORMALIZED (lowercase) so it matches the
            # enforcement tokens (built lowercase from workflow.dept_ids). Storing
            # verbatim while enforcing lowercase = a halt that never fires.
            scope_id = f"{d_org}:{d_dept.lower()}"
        else:
            raise HTTPException(status_code=403, detail="can only halt a dept you administer")
    else:
        raise HTTPException(status_code=400, detail="scope_type must be global|org|dept")
    actor = claims.get("email") or claims.get("user_id") or "unknown"
    doc = await workflow_control.set_control(
        _wf_control_col(), scope_type=st, scope_id=scope_id,
        enabled=bool(body.enabled), actor=actor, reason=body.reason or "",
    )
    logger.warning(
        f"[workflow-kill-switch] {'HALT' if body.enabled else 'CLEAR'} scope={st}:{scope_id} by={actor}"
    )
    return {"ok": True, "control": doc}


@router.post("/api/admin/workflows/{workflow_id}/reassign")
async def admin_reassign_workflow(request: Request, workflow_id: str):
    """REMOVED. Workflow ownership is fixed to the caller's org and
    cannot be reassigned. Endpoint preserved at 410 Gone so older clients
    fail loudly rather than silently no-op."""
    raise HTTPException(
        status_code=410,
        detail="workflow ownership is fixed to the org and cannot be reassigned",
    )


# ─── Phase B: Lifecycle transitions ────────────────────────────────────
# Each transition is an explicit endpoint (not a hidden state change).
# Authorisation rules summarised:
#   share / unshare        → owner OR org_admin OR super_admin
#   transfer (owner-init.) → owner OR org_admin (within org)
#   claim-for-dept         → dept_admin of a dept the workflow already
#                            sits in, OR org_admin
#   escalate-to-org        → org_admin OR super_admin
#   archive                → owner OR org_admin OR super_admin
#   restore                → org_admin OR super_admin
#   inheritance-policy     → owner-user (only — SAs/depts/orgs don't decay)
# All transitions append an audit entry to previous_owners or
# lifecycle_audit so the resource history stays reconstructable.


def _audit_entry(claims: Dict[str, Any], action: str, **extra) -> Dict[str, Any]:
    """Build a standard lifecycle audit entry."""
    entry = {
        "action": action,
        "by": claims["email"] or claims["user_id"],
        "at": datetime.utcnow(),
    }
    entry.update(extra)
    return entry


class ShareRequest(BaseModel):
    add: List[Dict[str, str]] = []
    remove: List[str] = []
    set_visibility: Optional[Dict[str, str]] = None


@router.post("/api/workflows/{workflow_id}/share")
async def share_workflow(request: Request, workflow_id: str, body: ShareRequest):
    """REMOVED: Per-resource collaborator grants are not supported.

    Under SA-only ownership, granting access to a person is done by adding
    them to the owning service account's `members` (read+run) or `admins`
    (edit). One place, applies to every resource the SA owns.

    Call POST /api/admin/service-accounts/{sa_id} with admins/members
    updates instead. This endpoint returns 410 Gone to force callers
    onto the SA-based flow.
    """
    raise HTTPException(
        status_code=410,
        detail=(
            "Per-workflow collaborators are no longer supported. To share a "
            "workflow, add the user to the owning service account's members "
            "(read+run) or admins (edit) via "
            "POST /api/admin/service-accounts/{sa_id}."
        ),
    )


class TransferRequest(BaseModel):
    new_owner_type: Literal["service_account", "dept", "org"]
    new_owner_id: str
    reason: Optional[str] = None


@router.post("/api/workflows/{workflow_id}/transfer")
async def transfer_workflow(request: Request, workflow_id: str, body: TransferRequest):
    """REMOVED. Workflow ownership is fixed to the caller's org."""
    raise HTTPException(
        status_code=410,
        detail="workflow ownership is fixed to the org and cannot be transferred",
    )


class ClaimForDeptRequest(BaseModel):
    dept_id: str
    reason: Optional[str] = None


@router.post("/api/workflows/{workflow_id}/claim-for-dept")
async def claim_workflow_for_dept(request: Request, workflow_id: str, body: ClaimForDeptRequest):
    """REMOVED. Workflows cannot be claimed by a dept — every workflow
    is org-owned."""
    raise HTTPException(
        status_code=410,
        detail="workflows are org-owned; per-dept ownership is no longer supported",
    )


class EscalateRequest(BaseModel):
    reason: Optional[str] = None


@router.post("/api/workflows/{workflow_id}/escalate-to-org")
async def escalate_workflow_to_org(request: Request, workflow_id: str, body: EscalateRequest):
    """REMOVED. Workflows are org-owned by default — escalation is a no-op."""
    raise HTTPException(
        status_code=410,
        detail="workflows are already org-owned by default; escalation is a no-op",
    )


class ArchiveRequest(BaseModel):
    reason: Optional[str] = None


@router.post("/api/workflows/{workflow_id}/archive")
async def archive_workflow(request: Request, workflow_id: str, body: ArchiveRequest):
    """Soft-archive a workflow: lifecycle_stage → archived, status flipped
    to inactive. Any workflow-access role in the workflow's org may
    archive. Reversible via /restore.
    """
    claims = _require_workflow_access(request)
    db = _db()
    wf = await db["Workflows"].find_one({"workflow_id": workflow_id})
    if not wf:
        raise HTTPException(status_code=404, detail="Workflow not found")
    if wf.get("org_id") != claims["org_id"] and Roles.SUPER_ADMIN not in set(claims["roles"]):
        raise HTTPException(status_code=403, detail="workflow outside your org")

    if wf.get("lifecycle_stage") == "archived":
        return {"ok": True, "workflow_id": workflow_id, "lifecycle_stage": "archived", "already_archived": True}

    now = datetime.utcnow()
    prev_stage = wf.get("lifecycle_stage", "personal")
    await db["Workflows"].update_one(
        {"workflow_id": workflow_id},
        {
            "$set": {
                "lifecycle_stage": "archived",
                "previous_lifecycle_stage": prev_stage,
                "archived_at": now,
                "archived_by": claims["email"] or claims["user_id"],
                "status": "inactive",
                "updated_at": now,
            },
            "$push": {
                "lifecycle_audit": _audit_entry(
                    claims, "archive", previous_stage=prev_stage, reason=body.reason or ""
                ),
            },
        },
    )
    return {
        "ok": True,
        "workflow_id": workflow_id,
        "lifecycle_stage": "archived",
        "previous_lifecycle_stage": prev_stage,
    }


@router.post("/api/workflows/{workflow_id}/restore")
async def restore_workflow(request: Request, workflow_id: str):
    """Restore an archived workflow to its previous lifecycle_stage.

    Any workflow-access role in the workflow's org may restore.
    """
    claims = _require_workflow_access(request)
    db = _db()
    wf = await db["Workflows"].find_one({"workflow_id": workflow_id})
    if not wf:
        raise HTTPException(status_code=404, detail="Workflow not found")
    if not _same_org_or_super(claims, wf):
        raise HTTPException(status_code=403, detail="workflow outside your org")
    if wf.get("lifecycle_stage") != "archived":
        raise HTTPException(status_code=400, detail="workflow is not archived")

    prev_stage = wf.get("previous_lifecycle_stage") or "personal"
    now = datetime.utcnow()
    await db["Workflows"].update_one(
        {"workflow_id": workflow_id},
        {
            "$set": {
                "lifecycle_stage": prev_stage,
                "status": "active",
                "restored_at": now,
                "restored_by": claims["email"] or claims["user_id"],
                "updated_at": now,
            },
            "$unset": {"archived_at": "", "archived_by": "", "previous_lifecycle_stage": ""},
            "$push": {
                "lifecycle_audit": _audit_entry(
                    claims, "restore", restored_to=prev_stage
                ),
            },
        },
    )
    return {"ok": True, "workflow_id": workflow_id, "lifecycle_stage": prev_stage}


class InheritancePolicyRequest(BaseModel):
    inheritance_policy: Literal[
        "archive", "transfer_to_sa", "transfer_to_dept", "transfer_to_org", "delete_after_grace",
    ]
    inheritance_target: Optional[str] = None
    inheritance_grace_days: Optional[int] = None


@router.post("/api/workflows/{workflow_id}/inheritance-policy")
async def set_workflow_inheritance_policy(
    request: Request, workflow_id: str, body: InheritancePolicyRequest
):
    """REMOVED. Workflows are org-owned and do not inherit on user
    deactivation — they simply remain with the org."""
    raise HTTPException(
        status_code=410,
        detail="workflows are org-owned; no inheritance policy is required",
    )


@router.post("/api/workflows/{workflow_id}/duplicate")
async def duplicate_workflow(request: Request, workflow_id: str):
    """Clone a workflow. Same role gate as create; clone is always
    org-owned by the caller's org (even if the original is a legacy row
    with a non-org owner_type)."""
    claims = _require_workflow_access(request)
    user_id = get_secure_user_id(request)
    db = _db()

    original = await db["Workflows"].find_one({"workflow_id": workflow_id})
    if not original:
        raise HTTPException(status_code=404, detail="Workflow not found")
    denial = _check_workflow_action(original, request, action="read")
    if denial:
        raise HTTPException(status_code=denial[0], detail=denial[1])

    new_id = str(uuid.uuid4())
    now = datetime.utcnow()
    org_id = claims["org_id"]
    clone = {k: v for k, v in original.items() if k != "_id"}
    clone.update({
        "workflow_id": new_id,
        "name": f"{original['name']} (Copy)",
        "version": 1,
        "owner_type": "org",
        "owner_id": org_id,
        "org_id": org_id,
        "lifecycle_stage": "org_managed",
        "author_user_id": user_id,
        "author_email": claims["email"] or user_id,
        "author_at": now,
        "previous_owners": [],
        "created_at": now,
        "updated_at": now,
    })
    await db["Workflows"].insert_one(clone)
    return {"workflow_id": new_id, "message": "Workflow duplicated"}


# ─── Deploy lineage: snapshots + rollback ─────────────────────────────


# How many deploy snapshots to retain per workflow. The lineage is a
# rollback safety-net, not an archive — keep a small bounded window so the
# WorkflowVersions collection can't grow without limit under frequent
# redeploys. The currently-live version is ALWAYS retained even if it falls
# outside this window (it is the baseline a rollback/compare runs against).
# Overridable per deployment via WF_MAX_VERSIONS (floored at 1).
MAX_WORKFLOW_VERSIONS = max(1, int(os.environ.get("WF_MAX_VERSIONS", "3")))


async def _prune_old_versions(
    db, workflow_id: str, *, keep: int, protect_version: Optional[int] = None,
) -> List[int]:
    """Delete deploy snapshots beyond the newest `keep`, NEVER removing
    `protect_version` (the live/just-written one). Returns the version_numbers
    that were pruned. Self-contained so both deploy and rollback share one
    retention rule.
    """
    cursor = db["WorkflowVersions"].find(
        {"workflow_id": workflow_id}, projection={"version_number": 1},
    ).sort("version_number", -1)
    nums = [int(v["version_number"]) async for v in cursor if v.get("version_number") is not None]

    survivors = set(nums[:keep])
    if protect_version is not None:
        survivors.add(int(protect_version))
    to_delete = [n for n in nums if n not in survivors]
    if to_delete:
        await db["WorkflowVersions"].delete_many(
            {"workflow_id": workflow_id, "version_number": {"$in": to_delete}},
        )
    return to_delete


def _graph_content_hash(doc: dict) -> str:
    """Stable sha256 over the parts of a workflow that define WHAT it does:
    nodes, edges, schedule, variables. Order-independent (sorted keys) so two
    semantically-identical graphs hash equal. Used to label "vN == vM" and to
    let a redeploy notice the graph is unchanged.
    """
    payload = {
        "nodes": doc.get("nodes", []),
        "edges": doc.get("edges", []),
        "schedule": doc.get("schedule", {}) or {},
        "variables": doc.get("variables", {}) or {},
    }
    canonical = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


async def _append_version_snapshot(
    db, doc: dict, claims: Dict[str, Any], *,
    source: str, note: str, run_environment: str,
    restored_from_version: Optional[int] = None,
) -> int:
    """Append an immutable WorkflowVersions snapshot of `doc`'s current graph
    and return the new monotonic version_number. The unique index on
    (workflow_id, version_number) makes a racing double-deploy fail loud on
    the second insert rather than silently forking the lineage.
    """
    workflow_id = doc["workflow_id"]
    # Next number = current high-water mark + 1. The unique index is the real
    # guard; this read just picks the value.
    latest = await db["WorkflowVersions"].find_one(
        {"workflow_id": workflow_id},
        sort=[("version_number", -1)],
        projection={"version_number": 1},
    )
    next_number = int((latest or {}).get("version_number", 0)) + 1

    snapshot = WorkflowVersion(
        workflow_id=workflow_id,
        version_number=next_number,
        name=doc.get("name", ""),
        description=doc.get("description", ""),
        nodes=doc.get("nodes", []),
        edges=doc.get("edges", []),
        schedule=doc.get("schedule", {}) or {},
        variables=doc.get("variables", {}) or {},
        run_environment=run_environment,
        max_run_llm_calls=doc.get("max_run_llm_calls"),
        content_hash=_graph_content_hash(doc),
        source=source,
        restored_from_version=restored_from_version,
        note=note or "",
        deployed_by=claims.get("user_id", "") or "",
        deployed_by_email=claims.get("email", "") or claims.get("user_id", "") or "",
        org_id=doc.get("org_id", "") or claims.get("org_id", "") or "",
        created_at=datetime.utcnow(),
    )
    await db["WorkflowVersions"].insert_one(snapshot.model_dump())
    # Enforce the retention window. The version we just wrote is the newest, so
    # it always survives; protect it explicitly anyway.
    await _prune_old_versions(
        db, workflow_id, keep=MAX_WORKFLOW_VERSIONS, protect_version=next_number,
    )
    return next_number


def _version_summary(v: dict) -> dict:
    """Lightweight projection for the history list (no full graph payload)."""
    return {
        "version_number": v.get("version_number"),
        "source": v.get("source", "deploy"),
        "restored_from_version": v.get("restored_from_version"),
        "note": v.get("note", ""),
        "run_environment": v.get("run_environment", "prod"),
        "content_hash": v.get("content_hash", ""),
        "node_count": len(v.get("nodes", []) or []),
        "deployed_by": v.get("deployed_by", ""),
        "deployed_by_email": v.get("deployed_by_email", ""),
        "created_at": v.get("created_at"),
    }


# ─── Deploy / Undeploy ────────────────────────────────────────────────

@router.post("/api/workflows/{workflow_id}/deploy")
async def deploy_workflow(request: Request, workflow_id: str, body: DeployWorkflowRequest):
    """Deploy or undeploy a workflow.

    Deploy: validates the workflow has nodes, generates webhook_token,
    registers cron schedule if configured. Sets status to DEPLOYED.

    Undeploy: removes cron schedule, sets status back to DRAFT.
    """
    get_secure_user_id(request)  # authn gate (raises 401 if unauthenticated)
    claims = _jwt_claims(request)
    db = _db()

    doc = await db["Workflows"].find_one({"workflow_id": workflow_id})
    if not doc:
        raise HTTPException(status_code=404, detail="Workflow not found")
    # Deploy/undeploy is a privileged mutation — authorize against the SA
    # ownership model, not the legacy user_id field.
    denial = _check_workflow_action(doc, request, action="edit")
    if denial:
        raise HTTPException(status_code=denial[0], detail=denial[1])
    owner_user_id = doc.get("user_id") or ""

    if body.action == "deploy":
        # Validation — must have at least one node
        nodes = doc.get("nodes", [])
        edges = doc.get("edges", [])
        if not nodes:
            raise HTTPException(status_code=400, detail="Cannot deploy a workflow with no nodes")

        # Validate DAG structure before deploying
        dag_errors = _validate_dag(nodes, edges)
        if dag_errors:
            raise HTTPException(status_code=400, detail=f"Cannot deploy: {'; '.join(dag_errors)}")

        # Deploy = go live = run unattended against PROD. Pin the authoritative
        # run environment so the scheduler/webhook can never silently disagree
        # (prod-readiness BLOCKER #1 / HIGH #6). Validate, before committing the
        # deploy, that the workflow is environment-safe for that target:
        #   - no write node may use an inline (non-env-isolated) connection (#7)
        #   - every referenced saved connection has the target env's config
        # Fail loud here rather than deep inside a node at the first cron fire.
        target_env = "prod"
        env_errors = await _validate_deploy_environment(db, nodes, target_env)
        if env_errors:
            raise HTTPException(status_code=400, detail="Cannot deploy: " + " ".join(env_errors))

        # If this workflow auto-runs on a cron schedule, validate it NOW and
        # fail loud — an invalid cron used to deploy "successfully" then
        # silently never register, and there was no cap on fire frequency
        # (an every-minute unattended run against prod). Reject syntax errors
        # and schedules that breach the safe minimum interval before go-live.
        schedule = doc.get("schedule", {}) or {}
        if schedule.get("enabled") and schedule.get("cron_expression"):
            from .scheduler import validate_cron_schedule
            cron_errors = validate_cron_schedule(
                schedule.get("cron_expression"), schedule.get("timezone"),
            )
            if cron_errors:
                raise HTTPException(status_code=400, detail="Cannot deploy: " + " ".join(cron_errors))

        # Generate webhook token + HMAC secret if a webhook_trigger node
        # exists. The secret makes HMAC verification MANDATORY — a deployed
        # webhook must never be triggerable by knowledge of the URL token
        # alone (see webhook_trigger).
        #
        # SAFE-DEPLOY: PRESERVE an existing token across a redeploy. The old
        # behaviour minted a fresh token on every deploy, so an undeploy →
        # redeploy (or just redeploying after an edit) silently broke every
        # external caller still POSTing to the previous URL. A redeploy is the
        # routine "ship my change" path and must not rotate the secret. Token
        # rotation is an explicit, deliberate action via
        # /rotate-webhook-token. Only mint a token here when one does not yet
        # exist (first deploy of a webhook workflow).
        has_webhook = any(n.get("type") == "webhook_trigger" for n in nodes)
        webhook_token = doc.get("webhook_token") or (secrets.token_urlsafe(32) if has_webhook else None)
        webhook_secret = doc.get("webhook_secret") or (secrets.token_urlsafe(32) if has_webhook else None)

        now = datetime.utcnow()
        # Append the immutable snapshot of EXACTLY what is going live BEFORE
        # flipping status, and pin the live doc to its version number. If the
        # snapshot insert fails (e.g. a racing concurrent deploy hits the
        # unique index) we fail loud and never half-deploy.
        new_version = await _append_version_snapshot(
            db, doc, claims,
            source="deploy", note=body.note, run_environment=target_env,
        )

        update = {
            "status": WorkflowStatus.DEPLOYED.value,
            "run_environment": target_env,
            "deployed_at": now,
            "updated_at": now,
            "deployed_version": new_version,
            "last_deploy_note": body.note or "",
        }
        if webhook_token:
            update["webhook_token"] = webhook_token
        if webhook_secret:
            update["webhook_secret"] = webhook_secret

        await db["Workflows"].update_one(
            {"workflow_id": workflow_id},
            {"$set": update},
        )

        # Register cron schedule if enabled
        schedule = doc.get("schedule", {})
        if schedule.get("enabled") and schedule.get("cron_expression"):
            from .scheduler import scheduler_manager, WorkflowSchedulerManager
            scheduler_manager.register_workflow(workflow_id, owner_user_id, schedule, workflow_name=doc.get("name", ""))
            # Notify scheduler replica via Redis pub/sub (no-op if this IS the scheduler)
            await WorkflowSchedulerManager.publish_schedule_refresh(
                workflow_id, owner_user_id, schedule, workflow_name=doc.get("name", ""), action="register"
            )

        result = {
            "message": "Workflow deployed",
            "status": "deployed",
            "deployed_version": new_version,
        }
        if webhook_token:
            result["webhook_url"] = f"/api/workflows/webhook/{webhook_token}"
        return result

    else:  # undeploy
        now = datetime.utcnow()
        await db["Workflows"].update_one(
            {"workflow_id": workflow_id},
            {"$set": {"status": WorkflowStatus.DRAFT.value, "updated_at": now}},
        )

        # Remove cron schedule
        from .scheduler import scheduler_manager, WorkflowSchedulerManager
        scheduler_manager.unregister_workflow(workflow_id)
        # Notify scheduler replica via Redis pub/sub
        await WorkflowSchedulerManager.publish_schedule_refresh(
            workflow_id, owner_user_id, {}, action="unregister"
        )

        return {"message": "Workflow undeployed", "status": "draft"}


# ─── Version history + rollback ───────────────────────────────────────

@router.get("/api/workflows/{workflow_id}/versions")
async def list_workflow_versions(request: Request, workflow_id: str):
    """List the deploy lineage (newest first) for a workflow.

    Returns lightweight summaries (no full graph) plus the currently-live
    version number so the UI can mark "running now" and offer rollback.
    """
    _ = _require_workflow_access(request)
    db = _db()

    doc = await db["Workflows"].find_one({"workflow_id": workflow_id})
    if not doc:
        raise HTTPException(status_code=404, detail="Workflow not found")
    denial = _check_workflow_action(doc, request, action="read")
    if denial:
        raise HTTPException(status_code=denial[0], detail=denial[1])

    cursor = db["WorkflowVersions"].find({"workflow_id": workflow_id}).sort("version_number", -1)
    versions = [_version_summary(v) async for v in cursor]
    return {
        "workflow_id": workflow_id,
        "deployed_version": doc.get("deployed_version"),
        "status": doc.get("status"),
        "max_versions": MAX_WORKFLOW_VERSIONS,
        "versions": versions,
    }


@router.get("/api/workflows/{workflow_id}/versions/{version_number}")
async def get_workflow_version(request: Request, workflow_id: str, version_number: int):
    """Fetch one immutable snapshot in full (including its graph) so the UI
    can preview exactly what a rollback would restore."""
    _ = _require_workflow_access(request)
    db = _db()

    doc = await db["Workflows"].find_one({"workflow_id": workflow_id})
    if not doc:
        raise HTTPException(status_code=404, detail="Workflow not found")
    denial = _check_workflow_action(doc, request, action="read")
    if denial:
        raise HTTPException(status_code=denial[0], detail=denial[1])

    snap = await db["WorkflowVersions"].find_one(
        {"workflow_id": workflow_id, "version_number": version_number},
    )
    if not snap:
        raise HTTPException(status_code=404, detail="Version not found")
    snap.pop("_id", None)
    return snap


@router.get("/api/workflows/{workflow_id}/versions/{version_number}/diff")
async def diff_workflow_version(
    request: Request, workflow_id: str, version_number: int,
    against: str = "current",
):
    """Structured diff showing what restoring ``version_number`` would change.

    ``against=current`` (default) diffs the LIVE graph → the snapshot, i.e.
    exactly the patch a rollback to that version applies. ``against=<int>``
    diffs snapshot <int> → snapshot ``version_number`` (lineage compare).
    Same shape as the AI assistant's proposal diff, so the UI renders both
    with one review component.
    """
    _ = _require_workflow_access(request)
    db = _db()

    doc = await db["Workflows"].find_one({"workflow_id": workflow_id})
    if not doc:
        raise HTTPException(status_code=404, detail="Workflow not found")
    denial = _check_workflow_action(doc, request, action="read")
    if denial:
        raise HTTPException(status_code=denial[0], detail=denial[1])

    snap = await db["WorkflowVersions"].find_one(
        {"workflow_id": workflow_id, "version_number": version_number},
    )
    if not snap:
        raise HTTPException(status_code=404, detail="Version not found")

    if against == "current":
        baseline: Dict[str, Any] = doc
        baseline_label = "current"
    else:
        try:
            against_num = int(against)
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail="'against' must be 'current' or a version number",
            )
        baseline = await db["WorkflowVersions"].find_one(
            {"workflow_id": workflow_id, "version_number": against_num},
        )
        if not baseline:
            raise HTTPException(status_code=404, detail=f"Version {against_num} not found")
        baseline_label = f"v{against_num}"

    diff = _diff_workflows(
        {"nodes": baseline.get("nodes") or [], "edges": baseline.get("edges") or [],
         "variables": baseline.get("variables") or {}},
        {"nodes": snap.get("nodes") or [], "edges": snap.get("edges") or [],
         "variables": snap.get("variables") or {}},
    )
    return {
        "workflow_id": workflow_id,
        "version_number": version_number,
        "against": baseline_label,
        "diff": diff,
    }


@router.delete("/api/workflows/{workflow_id}/versions/{version_number}")
async def delete_workflow_version(request: Request, workflow_id: str, version_number: int):
    """Manually prune a single deploy snapshot from the history (UI-managed
    cleanup). The currently-live version (``deployed_version``) cannot be
    deleted — it is the baseline a rollback/redeploy compares against — so the
    operator can always recover the running logic.
    """
    _ = _require_workflow_access(request)
    db = _db()

    doc = await db["Workflows"].find_one({"workflow_id": workflow_id})
    if not doc:
        raise HTTPException(status_code=404, detail="Workflow not found")
    denial = _check_workflow_action(doc, request, action="edit")
    if denial:
        raise HTTPException(status_code=denial[0], detail=denial[1])

    if doc.get("deployed_version") == version_number:
        raise HTTPException(
            status_code=400,
            detail="Cannot delete the current version — roll back or deploy a different version first",
        )

    res = await db["WorkflowVersions"].delete_one(
        {"workflow_id": workflow_id, "version_number": version_number},
    )
    if not getattr(res, "deleted_count", 0):
        raise HTTPException(status_code=404, detail="Version not found")
    return {"message": f"Deleted v{version_number}", "deleted_version": version_number}


@router.post("/api/workflows/{workflow_id}/rollback")
async def rollback_workflow(request: Request, workflow_id: str, body: RollbackWorkflowRequest):
    """Restore a previously-deployed version's graph onto the live workflow.

    The restore is append-only: the target snapshot's graph is copied back
    onto the live doc, and a NEW snapshot (source="rollback") is recorded so
    the lineage and the rollback action itself stay auditable — we never
    delete or rewrite history.

    If the workflow is currently DEPLOYED, the restored graph is re-validated
    (DAG + env-safety + cron) and its schedule re-registered, so the live
    automation immediately runs the rolled-back logic. If it is in DRAFT,
    the graph is restored but the workflow is left undeployed (the operator
    deploys it explicitly).
    """
    get_secure_user_id(request)  # authn gate (raises 401 if unauthenticated)
    claims = _jwt_claims(request)
    db = _db()

    doc = await db["Workflows"].find_one({"workflow_id": workflow_id})
    if not doc:
        raise HTTPException(status_code=404, detail="Workflow not found")
    denial = _check_workflow_action(doc, request, action="edit")
    if denial:
        raise HTTPException(status_code=denial[0], detail=denial[1])
    owner_user_id = doc.get("user_id") or ""

    target = await db["WorkflowVersions"].find_one(
        {"workflow_id": workflow_id, "version_number": body.version_number},
    )
    if not target:
        raise HTTPException(status_code=404, detail=f"Version {body.version_number} not found")

    nodes = target.get("nodes", []) or []
    edges = target.get("edges", []) or []
    if not nodes:
        raise HTTPException(status_code=400, detail="Cannot roll back to an empty version")

    # Re-validate the restored graph — an old version can reference a
    # connection that has since lost its prod config, or a node type that has
    # since been retired. Fail loud now, not at the next cron fire.
    dag_errors = _validate_dag(nodes, edges)
    if dag_errors:
        raise HTTPException(status_code=400, detail=f"Cannot roll back: {'; '.join(dag_errors)}")

    is_live = doc.get("status") == WorkflowStatus.DEPLOYED.value
    target_env = doc.get("run_environment") or "prod"
    if is_live:
        env_errors = await _validate_deploy_environment(db, nodes, target_env)
        if env_errors:
            raise HTTPException(status_code=400, detail="Cannot roll back: " + " ".join(env_errors))

    restored_schedule = target.get("schedule", {}) or {}
    if is_live and restored_schedule.get("enabled") and restored_schedule.get("cron_expression"):
        from .scheduler import validate_cron_schedule
        cron_errors = validate_cron_schedule(
            restored_schedule.get("cron_expression"), restored_schedule.get("timezone"),
        )
        if cron_errors:
            raise HTTPException(status_code=400, detail="Cannot roll back: " + " ".join(cron_errors))

    now = datetime.utcnow()
    note = body.note or f"Rolled back to v{body.version_number}"
    # Restore the graph onto the live doc. Bump the edit `version` so any open
    # editor working off a stale copy is forced to reconcile (optimistic
    # concurrency), exactly like a normal save.
    restore_fields = {
        "nodes": nodes,
        "edges": edges,
        "schedule": restored_schedule,
        "variables": target.get("variables", {}) or {},
        "max_run_llm_calls": target.get("max_run_llm_calls"),
        "updated_at": now,
    }

    # Only a rollback of a LIVE workflow advances the deploy lineage — that is
    # a new thing going to prod and must be snapshotted + audited. Rolling back
    # an UNDEPLOYED (draft) workflow just restores its editable graph, exactly
    # like any other edit, and is not a deploy; the operator deploys it
    # explicitly afterwards (which snapshots it then). Keeping the lineage to
    # deploys-only is what makes "deployed_version" mean "what's running".
    new_version = None
    if is_live:
        merged = {**doc, **restore_fields}  # snapshot the restored graph
        new_version = await _append_version_snapshot(
            db, merged, claims,
            source="rollback", note=note, run_environment=target_env,
            restored_from_version=body.version_number,
        )
        restore_fields["deployed_version"] = new_version
        restore_fields["last_deploy_note"] = note
        restore_fields["deployed_at"] = now

    await db["Workflows"].update_one(
        {"workflow_id": workflow_id},
        {"$set": restore_fields, "$inc": {"version": 1}},
    )

    # Re-sync the cron registration to the restored schedule when live: the
    # rolled-back version may enable/disable/retime the schedule.
    if is_live:
        from .scheduler import scheduler_manager, WorkflowSchedulerManager
        if restored_schedule.get("enabled") and restored_schedule.get("cron_expression"):
            scheduler_manager.register_workflow(
                workflow_id, owner_user_id, restored_schedule, workflow_name=doc.get("name", ""),
            )
            await WorkflowSchedulerManager.publish_schedule_refresh(
                workflow_id, owner_user_id, restored_schedule,
                workflow_name=doc.get("name", ""), action="register",
            )
        else:
            scheduler_manager.unregister_workflow(workflow_id)
            await WorkflowSchedulerManager.publish_schedule_refresh(
                workflow_id, owner_user_id, {}, action="unregister",
            )

    return {
        "message": f"Rolled back to v{body.version_number}",
        "restored_from_version": body.version_number,
        "new_version": new_version,
        "deployed_version": new_version if is_live else doc.get("deployed_version"),
        "status": doc.get("status"),
        "live": is_live,
    }


@router.post("/api/workflows/{workflow_id}/rotate-webhook-token")
async def rotate_webhook_token(request: Request, workflow_id: str):
    """Rotate the webhook token for a deployed workflow.

    Generates a new token and stores the old one with a grace period
    so in-flight requests using the old token still work briefly.
    """
    _ = get_secure_user_id(request)
    db = _db()

    doc = await db["Workflows"].find_one({"workflow_id": workflow_id})
    if not doc:
        raise HTTPException(status_code=404, detail="Workflow not found")
    denial = _check_workflow_action(doc, request, action="edit")
    if denial:
        raise HTTPException(status_code=denial[0], detail=denial[1])
    if not doc.get("webhook_token"):
        raise HTTPException(status_code=400, detail="Workflow has no webhook token to rotate")

    old_token = doc["webhook_token"]
    new_token = secrets.token_urlsafe(32)
    new_secret = secrets.token_urlsafe(32)
    now = datetime.utcnow()

    await db["Workflows"].update_one(
        {"workflow_id": workflow_id},
        {"$set": {
            "webhook_token": new_token,
            "webhook_secret": new_secret,
            "previous_webhook_token": old_token,
            "token_rotated_at": now,
            "updated_at": now,
        }},
    )

    return {
        "message": "Webhook token rotated",
        "webhook_url": f"/api/workflows/webhook/{new_token}",
        "webhook_secret": new_secret,
        "note": "Use X-Webhook-Signature and X-Webhook-Timestamp headers for HMAC verification",
    }


# ─── Scheduler health ───────────────────────────────────────────────

@router.get("/api/workflows/scheduler/health")
async def scheduler_health(request: Request):
    """Return the current scheduler leader state and per-workflow fire metrics.

    Does NOT require authentication so monitoring systems (Prometheus, uptime
    checkers) can poll it without credentials.  Sensitive data is not exposed.
    """
    from .scheduler import scheduler_manager
    from .config import SCHEDULER_INSTANCE_ID, SCHEDULER_LEADER_KEY

    cache = None
    leader_id = None
    metrics: dict = {}
    try:
        from citra_cache import get_cache_manager
        cache = get_cache_manager()
        if cache.use_redis:
            leader_id = cache.get(SCHEDULER_LEADER_KEY)
    except Exception:
        pass

    # Collect per-workflow metrics from Redis
    registered_jobs = scheduler_manager.get_registered_jobs()
    if cache and cache.use_redis:
        for wf_id in registered_jobs:
            try:
                last_fire   = cache.get(f"scheduler:metrics:{wf_id}:last_fire")
                last_status = cache.get(f"scheduler:metrics:{wf_id}:last_status")
                fire_count  = cache.get(f"scheduler:metrics:{wf_id}:fire_count")
                if last_fire or last_status:
                    metrics[wf_id] = {
                        "last_fire": last_fire,
                        "last_status": last_status,
                        "fire_count": int(fire_count) if fire_count else 0,
                    }
            except Exception:
                pass

    import time as _time
    uptime_s = None
    if scheduler_manager._started_at is not None:
        uptime_s = round(_time.monotonic() - scheduler_manager._started_at, 1)

    return {
        "instance_id": SCHEDULER_INSTANCE_ID,
        "is_leader": scheduler_manager._is_leader,
        "leader_id": leader_id,
        "registered_jobs": registered_jobs,
        "job_count": len(registered_jobs),
        "uptime_s": uptime_s,
        "workflow_metrics": metrics,
    }


# ─── Maintenance: orphaned workflow-media reclamation ──────────────────
# Workflow nodes stash binary media (uploaded images, fetched PDFs, recorded
# audio) in the GridFS bucket ``workflow_blobs`` and pass it between nodes by
# reference. Every run sweeps its own media on each terminal exit (executor
# ._sweep_blobs_if_terminal). But a run whose worker is hard-killed (OOM,
# container kill, redeploy) before any terminal handler executes leaves its
# media behind — as do legacy runs that predate the per-run sweep. This admin
# tool finds and reclaims that orphaned media on demand.
#
# A blob is an ORPHAN when its owning execution is terminal (completed / failed
# / cancelled / timed_out) or no longer exists. Media for a still-live run
# (pending / running / paused-for-approval) is NEVER touched — that run may yet
# resume and consume it.
_ALIVE_EXEC_STATUSES = {"pending", "running", "resuming", "paused", "waiting_approval"}


def _blob_older_than(upload_date: Any, *, hours: int) -> bool:
    """True if a blob's GridFS uploadDate is older than ``hours``.

    Used as an age guard so media with no execution id at all (rare/legacy) is
    only reaped once it's old enough to be sure it isn't mid-upload for an
    in-flight run. A missing/unparseable date is treated as 'not old' (keep) —
    the conservative choice that never deletes on ambiguity.
    """
    if not upload_date:
        return False
    try:
        from datetime import timezone, timedelta
        ud = upload_date
        if ud.tzinfo is None:
            ud = ud.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - ud) > timedelta(hours=hours)
    except Exception:
        return False


async def _scan_blob_orphans(claims: Dict[str, Any], *, delete: bool) -> Dict[str, Any]:
    """Scan ``workflow_blobs`` for orphaned media; optionally delete it.

    Org scope: super_admin sees the whole bucket; every other admin only sees
    and reclaims media belonging to their own org's workflows. A blob that
    cannot be attributed to an org (its run record is gone) is reported under
    ``skipped_unattributable`` and left for a super_admin to reap.
    """
    from .blob_store import list_blob_files, delete_blob

    roles = set(claims.get("roles") or [])
    is_super = Roles.SUPER_ADMIN in roles
    org_id = claims.get("org_id")
    db = _db()

    # Build this org's workflow-id set once (skipped for super_admin).
    org_wf_ids: Optional[set] = None
    if not is_super:
        org_wf_ids = set()
        async for doc in db["Workflows"].find({"org_id": org_id}, {"workflow_id": 1}):
            if doc.get("workflow_id"):
                org_wf_ids.add(doc["workflow_id"])

    blobs = await list_blob_files()

    # Resolve the status + owning workflow of every referenced run in one query.
    exec_ids = sorted({b["execution_id"] for b in blobs if b["execution_id"]})
    exec_by_id: Dict[str, Dict[str, Any]] = {}
    if exec_ids:
        async for ex in db["WorkflowExecutions"].find(
            {"execution_id": {"$in": exec_ids}},
            {"execution_id": 1, "status": 1, "workflow_id": 1},
        ):
            exec_by_id[ex["execution_id"]] = ex

    scanned = len(blobs)
    live_kept = 0
    orphan_count = 0
    orphan_bytes = 0
    skipped_unattributable = 0
    deleted = 0
    deleted_bytes = 0

    for b in blobs:
        exec_id = b["execution_id"]
        ex = exec_by_id.get(exec_id)

        # Org scoping for non-super admins.
        if org_wf_ids is not None:
            wf_id = (ex or {}).get("workflow_id")
            if ex is None or not wf_id:
                # Can't attribute → only super_admin may reap it.
                skipped_unattributable += 1
                continue
            if wf_id not in org_wf_ids:
                continue  # another org's media — invisible to this admin

        if ex is not None:
            status = str(ex.get("status") or "").lower()
            if status in _ALIVE_EXEC_STATUSES:
                live_kept += 1
                continue
            # terminal status → orphan
        else:
            # No execution record. A blob that names a (now-missing) run is an
            # orphan; one with no run id at all is reaped only once it's old
            # enough not to be mid-upload for an in-flight run.
            if not exec_id and not _blob_older_than(b.get("upload_date"), hours=24):
                live_kept += 1
                continue

        orphan_count += 1
        orphan_bytes += b["size"]
        if delete:
            if await delete_blob(b["id"]):
                deleted += 1
                deleted_bytes += b["size"]

    return {
        "scanned": scanned,
        "live_kept": live_kept,
        "orphans": orphan_count,
        "orphan_bytes": orphan_bytes,
        "skipped_unattributable": skipped_unattributable,
        "deleted": deleted,
        "deleted_bytes": deleted_bytes,
        "dry_run": not delete,
    }


@router.get("/api/workflows/maintenance/blob-usage")
async def maintenance_blob_usage(request: Request):
    """Dry-run scan of workflow media: how many blobs are stored and how many
    are orphaned (reclaimable). Admin-only. Deletes NOTHING."""
    claims = _require_workflow_access(request)
    return await _scan_blob_orphans(claims, delete=False)


@router.post("/api/workflows/maintenance/blob-sweep")
async def maintenance_blob_sweep(request: Request):
    """Reclaim orphaned workflow media — delete every GridFS blob whose owning
    run is terminal or gone. Media for a live/paused run is never touched.
    Admin-only."""
    claims = _require_workflow_access(request)
    result = await _scan_blob_orphans(claims, delete=True)
    logger.info(
        "Maintenance blob-sweep by %s: deleted %d/%d orphan blob(s), %d bytes reclaimed",
        claims.get("email") or claims.get("user_id"),
        result["deleted"], result["orphans"], result["deleted_bytes"],
    )
    return result


# ─── Execution ─────────────────────────────────────────────────────────

@router.post("/api/workflows/{workflow_id}/execute")
async def execute_workflow(request: Request, workflow_id: str, body: ExecuteWorkflowRequestV2 = None):
    """Execute a workflow now. Specify environment='test' or 'prod'.

    Enqueues a `workflow.run` job for Citra-Worker; returns immediately
    with an execution_id. The UI polls /status for progress.

    Execution itself runs in the Citra-Worker process — Citra-Service
    no longer keeps long-running work in its event loop.
    """
    user_id = get_secure_user_id(request)

    db = _db()

    doc = await db["Workflows"].find_one({"workflow_id": workflow_id})
    if not doc:
        raise HTTPException(status_code=404, detail="Workflow not found")
    # Authorise the CALLER to trigger this workflow (get_secure_user_id above +
    # run-permission on this specific workflow here).
    denied = _check_workflow_action(doc, request, action="run")
    if denied is not None:
        raise HTTPException(status_code=denied[0], detail=denied[1])

    # Kill switch: block a manual run when a halt covers this workflow's scope.
    await _enforce_workflow_not_halted(doc)

    doc.pop("_id", None)
    workflow = WorkflowDefinition(**doc)

    # The workflow runs at ORG level. Its agent/MCP nodes execute with the
    # WORKFLOW's own org-scoped identity (org_admin within the workflow's org —
    # full access to that single tenant's sources), NOT the triggering user's
    # scope. The caller is only authorised to *trigger* (checked above); the
    # execution identity is the workflow's org. This is the SAME token the
    # scheduler and webhook paths mint, so manual, cron and webhook runs all
    # see the identical org catalogue.
    from citra_auth import mint_workflow_org_token
    system_jwt = mint_workflow_org_token(
        workflow_id=workflow.workflow_id,
        org_id=workflow.org_id,
        dept_ids=workflow.dept_ids,
        author_email=getattr(workflow, "author_email", None) or workflow.user_id,
    )
    if not system_jwt:
        raise HTTPException(
            status_code=500,
            detail="Failed to mint the workflow's org-level execution token "
                   "(workflow needs an org_id and JWT_SECRET must be set); "
                   "refusing to run without an execution identity.",
        )

    environment = (body.environment if body else "test") or "test"
    trigger_data = (body.variables if body else None) or {}

    # Generate execution_id upfront so the UI can poll immediately.
    import uuid as _uuid
    from datetime import datetime as _dt
    execution_id = str(_uuid.uuid4())

    # Pre-create execution document with status=queued. Worker flips to
    # running when it picks up the job.
    await db["WorkflowExecutions"].insert_one({
        "execution_id": execution_id,
        "workflow_id": workflow_id,
        "user_id": user_id,
        "status": "queued",
        "trigger_data": trigger_data,
        "trigger_type": "manual",
        "environment": environment,
        "started_at": _dt.utcnow(),
        "node_results": {},
        "current_node": None,
        "error": None,
    })

    # Enqueue to Citra-Worker. Worker handler `workflow.run` loads the
    # WorkflowDefinition from Mongo and calls WorkflowExecutor.execute().
    try:
        from citra_queue import enqueue as _wq_enqueue  # type: ignore
        job_id = _wq_enqueue("workflow.run", {
            "workflow_id": workflow_id,
            "user_id": user_id,
            "trigger_data": trigger_data,
            "execution_id": execution_id,
            "environment": environment,
            "jwt_token": system_jwt,
        }, tenant_id=user_id, request_id=execution_id)
    except Exception as exc:  # noqa: BLE001
        logger.error(f"manual /execute enqueue failed: {exc}")
        # Mark the pre-created execution as failed so the UI sees something.
        await db["WorkflowExecutions"].update_one(
            {"execution_id": execution_id},
            {"$set": {
                "status": "failed",
                "error": f"failed to enqueue job: {exc}",
                "completed_at": _dt.utcnow(),
            }},
        )
        raise HTTPException(status_code=503, detail=f"Worker queue unavailable: {exc}")

    logger.info(
        f"⚡ enqueued manual execution {execution_id} (job={job_id}) for workflow {workflow_id}"
    )
    return {
        "execution_id": execution_id,
        "job_id": job_id,
        "status": "queued",
        "message": "Execution queued",
    }


@router.post("/api/workflows/{workflow_id}/execute-with-file")
async def execute_workflow_with_file(
    request: Request,
    workflow_id: str,
    file: UploadFile = File(...),
    environment: str = Form("test"),
):
    """Trigger a workflow with an uploaded file (image / audio / document).

    The file is stored once in the blob store and its ``{"_blob": {...}}``
    descriptor is placed into the run's trigger_data, so the trigger node emits
    it as a blob item that OCR / Transcribe / Vision nodes downstream consume.
    Mirrors the manual /execute path (enqueues a workflow.run job).
    """
    user_id = get_secure_user_id(request)
    db = _db()

    doc = await db["Workflows"].find_one({"workflow_id": workflow_id})
    if not doc:
        raise HTTPException(status_code=404, detail="Workflow not found")
    denied = _check_workflow_action(doc, request, action="run")
    if denied is not None:
        raise HTTPException(status_code=denied[0], detail=denied[1])

    # Kill switch: block a manual run when a halt covers this workflow's scope.
    await _enforce_workflow_not_halted(doc)

    doc.pop("_id", None)
    workflow = WorkflowDefinition(**doc)

    from citra_auth import mint_workflow_org_token
    system_jwt = mint_workflow_org_token(
        workflow_id=workflow.workflow_id,
        org_id=workflow.org_id,
        dept_ids=workflow.dept_ids,
        author_email=getattr(workflow, "author_email", None) or workflow.user_id,
    )
    if not system_jwt:
        raise HTTPException(
            status_code=500,
            detail="Failed to mint the workflow's org-level execution token.",
        )

    import uuid as _uuid
    from datetime import datetime as _dt
    execution_id = str(_uuid.uuid4())

    # Read + store the uploaded file as a blob (by reference, not inline).
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="Uploaded file is empty.")
    from .blob_store import put_blob
    descriptor = await put_blob(
        data,
        mime=file.content_type or "application/octet-stream",
        filename=file.filename or "upload",
        execution_id=execution_id,
    )
    # trigger_data carries the blob descriptor → manual/start trigger emits it
    # as an item (is_blob True), so the first multimodal node receives it.
    trigger_data = dict(descriptor)
    environment = environment or "test"

    await db["WorkflowExecutions"].insert_one({
        "execution_id": execution_id,
        "workflow_id": workflow_id,
        "user_id": user_id,
        "status": "queued",
        "trigger_data": trigger_data,
        "trigger_type": "manual",
        "environment": environment,
        "started_at": _dt.utcnow(),
        "node_results": {},
        "current_node": None,
        "error": None,
    })

    try:
        from citra_queue import enqueue as _wq_enqueue  # type: ignore
        job_id = _wq_enqueue("workflow.run", {
            "workflow_id": workflow_id,
            "user_id": user_id,
            "trigger_data": trigger_data,
            "execution_id": execution_id,
            "environment": environment,
            "jwt_token": system_jwt,
        }, tenant_id=user_id, request_id=execution_id)
    except Exception as exc:  # noqa: BLE001
        logger.error(f"execute-with-file enqueue failed: {exc}")
        await db["WorkflowExecutions"].update_one(
            {"execution_id": execution_id},
            {"$set": {"status": "failed",
                      "error": f"failed to enqueue job: {exc}",
                      "completed_at": _dt.utcnow()}},
        )
        raise HTTPException(status_code=503, detail=f"Worker queue unavailable: {exc}")

    logger.info(
        "⚡ enqueued file-upload execution %s (job=%s, %d bytes, %s) for workflow %s",
        execution_id, job_id, len(data), file.content_type, workflow_id,
    )
    return {
        "execution_id": execution_id,
        "job_id": job_id,
        "status": "queued",
        "blob": descriptor["_blob"],
        "message": "Execution queued with uploaded file",
    }


@router.get("/api/workflows/{workflow_id}/executions")
async def list_executions(
    request: Request,
    workflow_id: str,
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=PAGE_DEFAULT_EXECUTIONS, ge=1, le=PAGE_MAX),
    status: Optional[str] = Query(default=None),
):
    """List execution history for a workflow (most recent first).

    Returns only lightweight summary fields per run — never ``node_results``,
    which can carry large ``output_data``/base64 file content. The full per-node
    detail is available from ``GET /api/workflows/executions/{execution_id}``.
    """
    _ = get_secure_user_id(request)
    db = _db()

    # Authorize against the workflow's SA ownership, not the legacy
    # user_id on each execution doc — anyone who may read the workflow
    # may see its run history.
    workflow = await db["Workflows"].find_one({"workflow_id": workflow_id})
    if not workflow:
        raise HTTPException(status_code=404, detail="Workflow not found")
    denial = _check_workflow_action(workflow, request, action="read")
    if denial:
        raise HTTPException(status_code=denial[0], detail=denial[1])

    query: Dict[str, Any] = {"workflow_id": workflow_id}
    if status:
        query["status"] = status

    cursor = db["WorkflowExecutions"].find(
        query,
        {
            "_id": 0,
            "execution_id": 1, "workflow_id": 1, "status": 1,
            "environment": 1, "trigger_type": 1, "trigger": 1,
            "started_at": 1, "completed_at": 1,
            "current_node": 1, "paused_at_node": 1, "error": 1,
        },
    ).sort("started_at", -1).skip(skip).limit(limit)

    items = []
    async for doc in cursor:
        duration_ms = _compute_duration_ms(doc.get("started_at"), doc.get("completed_at"))
        row = _serialize(doc)
        # Normalize the trigger field name (model field is ``trigger``; docs are
        # written with key ``trigger_type``). Expose a single ``trigger_type``.
        row["trigger_type"] = doc.get("trigger_type") or doc.get("trigger") or "manual"
        row.pop("trigger", None)
        row["duration_ms"] = duration_ms
        items.append(row)

    return {
        "workflow": {
            "workflow_id": workflow.get("workflow_id"),
            "name": workflow.get("name"),
            "is_deployed": _is_deployed(workflow),
        },
        "executions": items,
    }


# ─── Templates ─────────────────────────────────────────────────────────

def _get_system_templates() -> List[Dict]:
    """Return built-in workflow templates."""
    return [
        {
            "template_id": "tpl-research-agent",
            "name": "Research Agent",
            "description": "An AI agent that researches a topic using web search and produces a structured report",
            "category": "ai_agents",
            "icon": "🔬",
            "tags": ["research", "ai", "report"],
            "nodes": [
                {"id": "start", "type": "start_node", "label": "Start",
                 "position": {"x": 50, "y": 200},
                 "config": {"input_schema": [
                     {"name": "topic", "type": "string", "required": True, "label": "Research Topic"},
                     {"name": "depth", "type": "string", "required": False, "default": "detailed", "label": "Depth"},
                 ]}},
                {"id": "agent", "type": "ai_agent", "label": "Research Agent",
                 "position": {"x": 350, "y": 200},
                 "config": {
                     "agent_name": "Research Agent",
                     "system_prompt": "You are a thorough research assistant. Use web search to gather information, then synthesize a comprehensive report.",
                     "user_prompt": "Research this topic: {{topic}}\nDepth: {{depth}}\n\nUse web search to find current information. Produce a structured report.",
                     "model": None,
                     "tier": "large",
                     "tools": ["web_search"],
                     "output_schema": {"type": "object", "properties": {"title": {"type": "string"}, "summary": {"type": "string"}, "findings": {"type": "array"}, "sources": {"type": "array"}}},
                 }},
            ],
            "edges": [
                {"id": "e1", "source": "start", "target": "agent"},
            ],
        },
        {
            "template_id": "tpl-data-pipeline",
            "name": "Data Processing Pipeline",
            "description": "Fetch data from an API, transform it, classify with AI, and export results",
            "category": "data",
            "icon": "📊",
            "tags": ["data", "etl", "classification"],
            "nodes": [
                {"id": "trigger", "type": "manual_trigger", "label": "Manual Trigger",
                 "position": {"x": 50, "y": 200}, "config": {}},
                {"id": "source", "type": "api_source", "label": "Fetch Data",
                 "position": {"x": 280, "y": 200}, "config": {"url": "", "method": "GET"}},
                {"id": "transform", "type": "data_transform", "label": "Clean & Filter",
                 "position": {"x": 510, "y": 200}, "config": {"operation": "filter", "params": {}}},
                {"id": "classify", "type": "classifier", "label": "AI Classifier",
                 "position": {"x": 740, "y": 200}, "config": {"labels": "high_priority, medium, low", "model": None}},
                {"id": "export", "type": "excel_export", "label": "Export to Excel",
                 "position": {"x": 970, "y": 200}, "config": {"filename": "classified_data.xlsx"}},
            ],
            "edges": [
                {"id": "e1", "source": "trigger", "target": "source"},
                {"id": "e2", "source": "source", "target": "transform"},
                {"id": "e3", "source": "transform", "target": "classify"},
                {"id": "e4", "source": "classify", "target": "export"},
            ],
        },
        {
            "template_id": "tpl-approval-workflow",
            "name": "Review & Approval",
            "description": "Process data, check conditions, require human approval for flagged items",
            "category": "business",
            "icon": "✅",
            "tags": ["approval", "review", "compliance"],
            "nodes": [
                {"id": "start", "type": "start_node", "label": "Start",
                 "position": {"x": 50, "y": 200},
                 "config": {"input_schema": [
                     {"name": "data", "type": "json", "required": True, "label": "Records to Review"},
                 ]}},
                {"id": "validate", "type": "validator", "label": "Validate Records",
                 "position": {"x": 300, "y": 200}, "config": {}},
                {"id": "check", "type": "condition", "label": "All Valid?",
                 "position": {"x": 550, "y": 200}, "config": {"field": "invalid_count", "operator": "==", "value": "0"}},
                {"id": "approve", "type": "human_approval", "label": "Manager Approval",
                 "position": {"x": 800, "y": 100}, "config": {"message": "Please review validated records"}},
                {"id": "export", "type": "email_sender", "label": "Send Report",
                 "position": {"x": 1050, "y": 100}, "config": {}},
                {"id": "fix", "type": "ai_agent", "label": "Auto-Fix Agent",
                 "position": {"x": 800, "y": 300},
                 "config": {
                     "system_prompt": "You fix data quality issues. Return corrected records.",
                     "user_prompt": "Fix these records: {{data}}",
                     "model": None, "tier": "large", "tools": [],
                 }},
            ],
            "edges": [
                {"id": "e1", "source": "start", "target": "validate"},
                {"id": "e2", "source": "validate", "target": "check"},
                {"id": "e3", "source": "check", "target": "approve", "source_handle": "true"},
                {"id": "e4", "source": "check", "target": "fix", "source_handle": "false"},
                {"id": "e5", "source": "approve", "target": "export"},
            ],
        },
        {
            "template_id": "tpl-multi-agent",
            "name": "Multi-Agent Pipeline",
            "description": "Chain multiple AI agents with a router to handle different task types",
            "category": "ai_agents",
            "icon": "🤖",
            "tags": ["multi-agent", "routing", "ai"],
            "nodes": [
                {"id": "start", "type": "start_node", "label": "Start",
                 "position": {"x": 50, "y": 250},
                 "config": {"input_schema": [
                     {"name": "task", "type": "string", "required": True, "label": "Task Description"},
                     {"name": "category", "type": "string", "required": True, "label": "Task Category"},
                 ]}},
                {"id": "router", "type": "switch_router", "label": "Task Router",
                 "position": {"x": 300, "y": 250},
                 "config": {"field": "category", "routes": [
                     {"label": "Analysis", "value": "analysis"},
                     {"label": "Writing", "value": "writing"},
                     {"label": "Default", "value": "__default__"},
                 ]}},
                {"id": "analyst", "type": "ai_agent", "label": "Analyst Agent",
                 "position": {"x": 600, "y": 100},
                 "config": {
                     "agent_name": "Analyst",
                     "system_prompt": "You are a data analyst. Analyze the task and provide insights.",
                     "user_prompt": "{{task}}",
                     "model": None, "tier": "large", "tools": ["web_search", "code_execute"],
                 }},
                {"id": "writer", "type": "ai_agent", "label": "Writer Agent",
                 "position": {"x": 600, "y": 300},
                 "config": {
                     "agent_name": "Writer",
                     "system_prompt": "You are a professional writer. Draft content based on the task.",
                     "user_prompt": "{{task}}",
                     "model": None, "tier": "large", "tools": ["web_search"],
                 }},
                {"id": "general", "type": "ai_agent", "label": "General Agent",
                 "position": {"x": 600, "y": 500},
                 "config": {
                     "agent_name": "General",
                     "system_prompt": "You are a helpful assistant. Complete the task.",
                     "user_prompt": "{{task}}",
                     "model": None, "tier": "large", "tools": [],
                 }},
            ],
            "edges": [
                {"id": "e1", "source": "start", "target": "router"},
                {"id": "e2", "source": "router", "target": "analyst", "source_handle": "out-0"},
                {"id": "e3", "source": "router", "target": "writer", "source_handle": "out-1"},
                {"id": "e4", "source": "router", "target": "general", "source_handle": "out-2"},
            ],
        },
        {
            "template_id": "tpl-document-processor",
            "name": "Document Intelligence",
            "description": "Extract structured data from documents using AI and summarize",
            "category": "ai_agents",
            "icon": "📄",
            "tags": ["extraction", "documents", "ai"],
            "nodes": [
                {"id": "start", "type": "start_node", "label": "Start",
                 "position": {"x": 50, "y": 200},
                 "config": {"input_schema": [
                     {"name": "document_text", "type": "string", "required": True, "label": "Document Text"},
                 ]}},
                {"id": "extract", "type": "ai_agent", "label": "Extractor Agent",
                 "position": {"x": 350, "y": 200},
                 "config": {
                     "agent_name": "Document Extractor",
                     "system_prompt": "Extract structured data from the document. Be precise and thorough.",
                     "user_prompt": "Extract all key information from this document:\n\n{{document_text}}",
                     "model": None,
                     "tier": "large",
                     "tools": [],
                     "output_schema": {"type": "object", "properties": {"entities": {"type": "array"}, "dates": {"type": "array"}, "amounts": {"type": "array"}, "summary": {"type": "string"}}},
                 }},
                {"id": "summarize", "type": "summarizer", "label": "Summarize",
                 "position": {"x": 650, "y": 200}, "config": {"model": None, "max_length": 300}},
            ],
            "edges": [
                {"id": "e1", "source": "start", "target": "extract"},
                {"id": "e2", "source": "extract", "target": "summarize"},
            ],
        },
        {
            "template_id": "tpl-expense-analytics",
            "name": "Expense Analytics & Cost Savings",
            "description": "Analyze organizational expenses from SQL Server, identify spending patterns, flag policy violations, and recommend cost savings — delivered as a PDF report and Excel breakdown",
            "category": "business",
            "icon": "💰",
            "tags": ["expense", "analytics", "cost-savings", "finance", "report"],
            "variables": {"period": "last_month"},
            "nodes": [
                {"id": "trigger", "type": "scheduled_trigger", "label": "Monthly Schedule",
                 "position": {"x": 50, "y": 250},
                 "config": {
                     "cron": "0 8 1 * *",
                     "description": "Runs on the 1st of every month at 8 AM UTC",
                 }},
                {"id": "fetch-expenses", "type": "sql_source", "label": "Fetch Expenses",
                 "position": {"x": 300, "y": 250},
                 "config": {
                     "query": "SELECT e.expense_id, e.employee_name, e.department, e.category, e.vendor, e.amount, e.currency, e.expense_date, e.description, e.approval_status FROM expenses e WHERE e.expense_date >= DATEADD(month, -1, CAST(GETDATE() AS date)) AND e.expense_date < CAST(GETDATE() AS date) ORDER BY e.amount DESC",
                     "max_rows": 1000,
                 }},
                {"id": "analyze", "type": "ai_agent", "label": "Expense Analyst Agent",
                 "position": {"x": 580, "y": 250},
                 "config": {
                     "agent_name": "Expense Analyst",
                     "system_prompt": "You are a senior financial analyst specializing in corporate expense management. Analyze the expense data provided, identify spending patterns, flag potential policy violations (e.g. duplicate charges, unusually high amounts, unapproved vendors), break down costs by department and category, rank top vendors by spend, and propose concrete cost-saving recommendations with projected annual savings. Use the code_execute tool for calculations and the sql_query tool if you need additional data from the database.",
                     "user_prompt": "Analyze the following expense data for period {{period}}:\n\n{{data}}\n\nProduce a comprehensive expense analytics report.",
                     "model": None,
                     "tier": "large",
                     "tools": ["code_execute", "sql_query"],
                     "max_iterations": 15,
                     "output_schema": {
                         "type": "object",
                         "properties": {
                             "report_title": {"type": "string"},
                             "period": {"type": "string"},
                             "executive_summary": {"type": "string"},
                             "total_spend": {"type": "number"},
                             "employee_count": {"type": "integer"},
                             "avg_spend_per_employee": {"type": "number"},
                             "department_breakdown": {
                                 "type": "array",
                                 "items": {"type": "object", "properties": {
                                     "department": {"type": "string"},
                                     "total": {"type": "number"},
                                     "pct_of_total": {"type": "number"},
                                     "trend": {"type": "string"},
                                 }},
                             },
                             "category_breakdown": {
                                 "type": "array",
                                 "items": {"type": "object", "properties": {
                                     "category": {"type": "string"},
                                     "total": {"type": "number"},
                                     "pct_of_total": {"type": "number"},
                                 }},
                             },
                             "top_vendors": {
                                 "type": "array",
                                 "items": {"type": "object", "properties": {
                                     "vendor": {"type": "string"},
                                     "total": {"type": "number"},
                                     "transaction_count": {"type": "integer"},
                                 }},
                             },
                             "cost_saving_recommendations": {
                                 "type": "array",
                                 "items": {"type": "object", "properties": {
                                     "recommendation": {"type": "string"},
                                     "estimated_annual_savings": {"type": "number"},
                                     "priority": {"type": "string", "enum": ["high", "medium", "low"]},
                                 }},
                             },
                             "policy_violations": {
                                 "type": "array",
                                 "items": {"type": "object", "properties": {
                                     "expense_id": {"type": "string"},
                                     "employee_name": {"type": "string"},
                                     "violation_type": {"type": "string"},
                                     "details": {"type": "string"},
                                 }},
                             },
                             "projected_annual_savings": {"type": "number"},
                         },
                     },
                 }},
                {"id": "split", "type": "parallel_split", "label": "Split Outputs",
                 "position": {"x": 860, "y": 250}, "config": {}},
                {"id": "pdf-report", "type": "pdf_export", "label": "PDF Report",
                 "position": {"x": 1120, "y": 130},
                 "config": {
                     "filename": "expense_report_{{period}}.pdf",
                     "title": "Expense Analytics Report",
                 }},
                {"id": "excel-export", "type": "excel_export", "label": "Excel Breakdown",
                 "position": {"x": 1120, "y": 370},
                 "config": {
                     "filename": "expense_breakdown_{{period}}.xlsx",
                 }},
            ],
            "edges": [
                {"id": "e1", "source": "trigger", "target": "fetch-expenses"},
                {"id": "e2", "source": "fetch-expenses", "target": "analyze"},
                {"id": "e3", "source": "analyze", "target": "split"},
                {"id": "e4", "source": "split", "target": "pdf-report"},
                {"id": "e5", "source": "split", "target": "excel-export"},
            ],
        },
    ]


