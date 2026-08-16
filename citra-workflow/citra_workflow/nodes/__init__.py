"""
Base Node & Node Registry
=========================
Plugin-based node system. Each node type inherits BaseNode and registers itself.
"""

from __future__ import annotations
import asyncio
import logging
import os
import random
import re
import time
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional, Type
from datetime import datetime

from ..models import (
    NodeType, NodeCategory, NodeSchema, NodeFieldSchema,
    NodeExecutionResult, NodeExecutionStatus,
)

logger = logging.getLogger(__name__)

_VAR_PATTERN = re.compile(r"\{\{(\w+)\}\}")


def sanitize_remote_path(path: str) -> str:
    """Normalize a remote file path and reject path-traversal attempts.

    Collapses ``..`` segments via ``posixpath.normpath``, then rejects any
    result that escapes the original root (starts with ``..`` or is absolute
    when a relative path was given).
    """
    import posixpath
    if not path:
        return path
    normalized = posixpath.normpath(path)
    # Block traversal: normalized path must never start with ".."
    if normalized.startswith("..") or "/../" in path:
        raise ValueError(f"Path traversal detected in path: {path!r}")
    return normalized


def interpolate_variables(text: str, variables: Dict[str, Any]) -> str:
    """Replace ``{{varname}}`` placeholders in *text* with values from *variables*.

    Only plain ``\\w+`` names are supported — no nested expressions.
    If a placeholder has no matching variable it is left unchanged.
    """
    if not text or not variables:
        return text

    def _replacer(match: re.Match) -> str:
        key = match.group(1)
        if key in variables:
            return str(variables[key])
        return match.group(0)  # leave unresolved placeholders as-is

    return _VAR_PATTERN.sub(_replacer, text)


def sql_parameterize(query: str, variables: Dict[str, Any]) -> tuple:
    """Convert ``{{varname}}`` placeholders to SQLAlchemy ``:varname`` bind params.

    Returns ``(parameterized_query, bind_dict)`` so the query can be executed
    safely via ``conn.execute(text(query), bind_dict)``.
    Unresolved placeholders (no matching variable) are left as literal text.

    Handles the common mistake of quoting placeholders:
    ``WHERE col = '{{name}}'`` → ``WHERE col = :name`` (quotes stripped).
    SQLAlchemy's bind mechanism adds quoting internally, so user-supplied
    quotes would cause double-quoting and a SQL syntax error.
    """
    if not query or not variables:
        return query, {}

    binds: Dict[str, Any] = {}

    # First pass: replace *quoted* placeholders  '{{name}}' or "{{name}}"
    _QUOTED_VAR = re.compile(r"""(['"])(\{\{(\w+)\}\})\1""")

    def _quoted_replacer(match: re.Match) -> str:
        key = match.group(3)
        if key in variables:
            binds[key] = variables[key]
            return f":{key}"
        return match.group(0)

    parameterized = _QUOTED_VAR.sub(_quoted_replacer, query)

    # Second pass: replace remaining unquoted {{name}} placeholders
    def _replacer(match: re.Match) -> str:
        key = match.group(1)
        if key in variables:
            binds[key] = variables[key]
            return f":{key}"
        return match.group(0)

    parameterized = _VAR_PATTERN.sub(_replacer, parameterized)
    return parameterized, binds


class NodeContext:
    """Execution context passed to each node."""

    def __init__(
        self,
        node_id: str,
        node_config: Dict[str, Any],
        input_data: Any = None,
        variables: Optional[Dict[str, Any]] = None,
        user_id: str = "",
        execution_id: str = "",
        environment: str = "test",
        workflow_context: Optional[Dict[str, Any]] = None,
        jwt_token: Optional[str] = None,
        workflow_id: str = "",
        workflow_name: str = "",
        owner_type: str = "",
        owner_id: str = "",
        org_id: str = "",
        dept_ids: Optional[List[str]] = None,
        author_user_id: str = "",
        linked_smart_app_id: Optional[str] = None,
        app_slug: str = "",
    ):
        self.node_id = node_id
        self.config = node_config
        self.input_data = input_data
        self.variables = variables or {}
        self.user_id = user_id
        self.execution_id = execution_id
        self.environment = environment  # "test" or "prod"
        self.workflow_context = workflow_context  # upstream schema context (AI_AGENT only)
        # Caller's JWT — used by AI agent nodes to mint short-lived service
        # tokens for dept-MCP calls. Empty/None for scheduled (cron) runs;
        # in that case dept-MCPs receive only the SERVICE_API_KEY and per-
        # source visibility is degraded to "no JWT claims".
        self.jwt_token = jwt_token
        # Workflow ownership (stamped onto rows by mongo_writer and used by
        # any node that needs to know "who owns the run").
        self.workflow_id = workflow_id
        self.workflow_name = workflow_name
        self.owner_type = owner_type
        self.owner_id = owner_id
        self.org_id = org_id
        self.dept_ids = dept_ids or []
        self.author_user_id = author_user_id
        self.linked_smart_app_id = linked_smart_app_id
        self.app_slug = app_slug

    @property
    def items(self) -> List[Dict[str, Any]]:
        """Primary data from the upstream node — always a list of dicts."""
        if isinstance(self.input_data, dict):
            val = self.input_data.get("items")
            if isinstance(val, list):
                return val
            return []
        if isinstance(self.input_data, list):
            return self.input_data
        return []

    @property
    def meta(self) -> Dict[str, Any]:
        """Operational metadata from the upstream node."""
        if isinstance(self.input_data, dict):
            m = self.input_data.get("meta")
            if isinstance(m, dict):
                return m
        return {}


# Backoff hardening: cap the per-retry delay so an exponential schedule can't
# balloon into multi-minute sleeps, and add jitter so many items retrying in
# lockstep don't form a thundering herd against the same downstream.
RETRY_MAX_DELAY_SECONDS = float(os.getenv("WF_RETRY_MAX_DELAY_SECONDS", "60"))
RETRY_JITTER_FRACTION = float(os.getenv("WF_RETRY_JITTER_FRACTION", "0.2"))


def _compute_backoff_delay(attempt: int, base_delay: float, mode: str) -> float:
    """Delay before the next retry. Exponential or fixed, clamped to
    ``RETRY_MAX_DELAY_SECONDS`` and spread by ±``RETRY_JITTER_FRACTION`` jitter
    so retries don't synchronise into a herd. ``attempt`` is 0-based."""
    raw = base_delay * (2 ** attempt) if mode == "exponential" else base_delay
    raw = min(raw, RETRY_MAX_DELAY_SECONDS)
    if RETRY_JITTER_FRACTION > 0 and raw > 0:
        spread = raw * RETRY_JITTER_FRACTION
        raw = max(0.0, raw + random.uniform(-spread, spread))
    return raw


class BaseNode(ABC):
    """Abstract base for all workflow nodes."""

    node_type: NodeType
    category: NodeCategory
    label: str = ""
    description: str = ""
    icon: str = "⚙️"
    color: str = "#6366f1"
    # Soft authz flag: when True, the workflow PUT validator requires the
    # caller's JWT dept_ids to include this node's config['dept_id']. Default
    # False so generic nodes are unaffected.
    dept_scope_required: bool = False
    # AI authoring flag. Default True — the node is included in the AI
    # workflow-generation system prompt and can be created by the AI.
    # Set False for internal nodes the registry generates on behalf of the
    # platform (e.g. dept-flow chunk_embed / vector_sink / structured_schema_sink
    # — registered by the Dept Sources screen, never authored by the AI but
    # preserved during refinement).
    ai_visible: bool = True
    # ai_authoring_hint is a short, AI-only one-liner that supplements the
    # human description with authoring guidance (e.g. "Pick dataset_id and
    # action_id from the AVAILABLE DEPT MCP CATALOGUE section"). Empty by
    # default.
    ai_authoring_hint: str = ""

    @abstractmethod
    async def execute(self, ctx: NodeContext) -> Any:
        """Run the node logic and return output data."""
        ...

    @staticmethod
    def _make_output(items: List[Dict[str, Any]], **meta) -> Dict[str, Any]:
        """Build the universal node output envelope.

        Every node MUST return this shape so downstream nodes can
        always read ``ctx.items`` for data and ``ctx.meta`` for metadata.
        """
        return {"items": items, "meta": meta}

    # ── Binary-blob helpers ─────────────────────────────────────────────
    # Media (image/audio/file bytes) is stored once in GridFS and passed
    # between nodes by reference as a {"_blob": {...}} item, never inline.
    async def put_blob(self, ctx: "NodeContext", data: bytes, *,
                       mime: str = "application/octet-stream",
                       filename: str = "blob") -> Dict[str, Any]:
        """Store bytes and return the ``{"_blob": {...}}`` descriptor item."""
        from ..blob_store import put_blob
        return await put_blob(
            data, mime=mime, filename=filename,
            execution_id=getattr(ctx, "execution_id", "") or "",
        )

    async def get_blob(self, item: Dict[str, Any]) -> bytes:
        """Fetch the bytes behind a ``{"_blob": {...}}`` reference item."""
        from ..blob_store import get_blob
        return await get_blob(item)

    @staticmethod
    def is_blob(item: Any) -> bool:
        from ..blob_store import is_blob
        return is_blob(item)

    @staticmethod
    def _extract_items(input_data: Any) -> List[Any]:
        """Normalize any input into a flat list of items.

        Reads the canonical ``items`` key from the universal envelope.
        Falls back to ``records`` for any legacy callers, plain lists, and dicts.
        """
        if input_data is None:
            return []
        if isinstance(input_data, list):
            return input_data
        if isinstance(input_data, dict):
            for key in ("items", "records"):
                if key in input_data and isinstance(input_data[key], list):
                    return input_data[key]
            # Single dict that isn't a container → treat as one item
            return [input_data]
        return [input_data]

    def validate_config(self, config: Dict[str, Any]) -> List[str]:
        """Return list of validation errors (empty = valid)."""
        errors: List[str] = []
        for field in self.get_fields():
            if field.required and not config.get(field.name):
                errors.append(f"'{field.label}' is required")
        return errors

    @classmethod
    def get_fields(cls) -> List[NodeFieldSchema]:
        """Override to declare config fields for the UI form."""
        return []

    @staticmethod
    def _retry_fields() -> List[NodeFieldSchema]:
        """Shared retry-config fields appended to every node's schema."""
        return [
            NodeFieldSchema(
                name="max_retries",
                label="Max Retries",
                type="number",
                default=0,
                help_text="Number of retry attempts on failure (0 = no retry)",
            ),
            NodeFieldSchema(
                name="retry_delay_seconds",
                label="Retry Delay (seconds)",
                type="number",
                default=5,
                visible_when={"field": "max_retries", "operator": ">", "value": 0},
                help_text="Seconds to wait between retries",
            ),
            NodeFieldSchema(
                name="retry_backoff",
                label="Retry Backoff",
                type="select",
                default="fixed",
                options=[
                    {"label": "Fixed delay", "value": "fixed"},
                    {"label": "Exponential (delay × 2^attempt)", "value": "exponential"},
                ],
                visible_when={"field": "max_retries", "operator": ">", "value": 0},
                help_text="How delay increases between retries",
            ),
        ]

    @classmethod
    def get_schema(cls) -> NodeSchema:
        return NodeSchema(
            type=cls.node_type,
            category=cls.category,
            label=cls.label,
            description=cls.description,
            icon=cls.icon,
            color=cls.color,
            fields=cls.get_fields() + BaseNode._retry_fields(),
            dept_scope_required=getattr(cls, "dept_scope_required", False),
        )

    async def run(self, ctx: NodeContext) -> NodeExecutionResult:
        """Wrapper that captures timing, errors, retries, and returns NodeExecutionResult."""
        started = datetime.utcnow()
        t0 = time.time()

        def _number_or_default(value, default, cast):
            if value in (None, ""):
                return default
            try:
                return cast(value)
            except (TypeError, ValueError):
                return default

        max_retries = _number_or_default(ctx.config.get("max_retries"), 0, int)
        retry_delay = max(0, _number_or_default(ctx.config.get("retry_delay_seconds"), 5.0, float))
        backoff = ctx.config.get("retry_backoff") or "fixed"
        retry_errors: List[str] = []

        # Side-effecting nodes (writers, senders, external POSTs) must NOT be
        # blindly auto-retried: a failure *after* a partial write commits
        # would duplicate the row / email / external call. Auto-retry is only
        # safe for read/transform nodes. OUTPUT-category nodes and any node
        # marked ``side_effecting = True`` are pinned to a single attempt —
        # unless the node explicitly declares ``retryable = True`` (i.e. its
        # write is idempotent).
        is_side_effecting = (
            getattr(self, "category", None) == NodeCategory.OUTPUT
            or getattr(self, "side_effecting", False)
        )
        if is_side_effecting and not getattr(self, "retryable", False) and max_retries > 0:
            logger.info(
                "Node %s is side-effecting — auto-retry disabled (a partial "
                "write must not be re-run); make the node idempotent and set "
                "retryable=True to opt back in.",
                ctx.node_id,
            )
            max_retries = 0

        last_error: Optional[str] = None
        for attempt in range(1 + max_retries):
            try:
                output = await self.execute(ctx)
                elapsed = int((time.time() - t0) * 1000)
                return NodeExecutionResult(
                    node_id=ctx.node_id,
                    status=NodeExecutionStatus.COMPLETED,
                    output_data=output,
                    retry_count=attempt,
                    retry_errors=retry_errors,
                    started_at=started,
                    completed_at=datetime.utcnow(),
                    duration_ms=elapsed,
                )
            except Exception as exc:
                last_error = str(exc)
                retry_errors.append(last_error)

                # Deterministic failures (e.g. a blocked write attempt) must
                # not be auto-retried — retrying only re-runs the work and
                # re-fails identically. The exception opts out via a
                # ``non_retryable = True`` attribute.
                if getattr(exc, "non_retryable", False):
                    logger.error(
                        "Node %s failed with a non-retryable error: %s",
                        ctx.node_id, last_error,
                    )
                    break

                if attempt < max_retries:
                    delay = _compute_backoff_delay(attempt, retry_delay, backoff)
                    logger.warning(
                        "Node %s attempt %d/%d failed: %s — retrying in %.1fs",
                        ctx.node_id, attempt + 1, 1 + max_retries, last_error, delay,
                    )
                    await asyncio.sleep(delay)
                else:
                    logger.error("Node %s failed after %d attempt(s): %s", ctx.node_id, attempt + 1, last_error)

        elapsed = int((time.time() - t0) * 1000)
        return NodeExecutionResult(
            node_id=ctx.node_id,
            status=NodeExecutionStatus.FAILED,
            error=last_error,
            retry_count=max_retries,
            retry_errors=retry_errors,
            started_at=started,
            completed_at=datetime.utcnow(),
            duration_ms=elapsed,
        )


# ============================================================================
# Node Registry
# ============================================================================

_NODE_REGISTRY: Dict[NodeType, Type[BaseNode]] = {}


def register_node(cls: Type[BaseNode]) -> Type[BaseNode]:
    """Decorator to register a node class."""
    _NODE_REGISTRY[cls.node_type] = cls
    return cls


def get_node(node_type: NodeType) -> BaseNode:
    """Instantiate a node by type."""
    cls = _NODE_REGISTRY.get(node_type)
    if cls is None:
        raise ValueError(f"Unknown node type: {node_type}")
    return cls()


def get_all_schemas() -> List[NodeSchema]:
    """Return schemas for all registered node types (for UI palette)."""
    # Nothing is hidden any more. The two nodes that used to be
    # (STRUCTURED_SCHEMA_SINK, STAGING_WRITER) were Citra-platform internals and
    # are deleted outright — a node hidden from the palette but still registered
    # is a node users can reach by editing JSON, which is not the same as gone.
    return [cls.get_schema() for cls in _NODE_REGISTRY.values()]


# Auto-import node modules so @register_node decorators fire
from . import triggers, sources, processors, outputs, logic, agents  # noqa: F401, E402
from . import state  # noqa: F401, E402  — cross-run watermark
from . import multimodal  # noqa: F401, E402  — OCR / audio-transcribe / vision-LLM
from . import notifications  # noqa: F401, E402  — Slack / Teams / webhook notify
from . import vector, mcp  # noqa: F401, E402  — any vector DB / any MCP server
from . import retrieval  # noqa: F401, E402  — embed + rerank


def get_registry() -> Dict[NodeType, Type[BaseNode]]:
    return dict(_NODE_REGISTRY)
