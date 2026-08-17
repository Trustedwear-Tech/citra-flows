# Copyright (c) 2026 Trustedwear Tech Private Limited (https://citra-ai.com)
# Author: Rohit Kumar Chandan
# SPDX-License-Identifier: BUSL-1.1
#
# Licensed under the Business Source License 1.1. Non-production use is granted;
# production use requires a commercial licence until the Change Date, after
# which this file converts to Apache-2.0. See LICENSE at the repository root.

"""Redis-backed schema cache for AI Agent tool connections.

Caches the result of schema/structure discovery so subsequent agent executions
re-use introspection data instead of hitting the remote system every time.

Cache key format: ``agent:schema:{connection_id}:{environment}``
Default TTL: 1 hour (configurable via ``WF_SCHEMA_CACHE_TTL``).
"""

from __future__ import annotations
import json
import logging
import os
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

SCHEMA_CACHE_TTL = int(os.environ.get("WF_SCHEMA_CACHE_TTL", "3600"))

# Max characters of schema text to inject into the LLM prompt.
MAX_SCHEMA_PROMPT_CHARS = int(os.environ.get("WF_MAX_SCHEMA_PROMPT_CHARS", "4000"))


def _cache_key(connection_id: str, environment: str) -> str:
    return f"agent:schema:{connection_id}:{environment}"


def _get_cache():
    """Return the global CacheManager singleton (lazy import to avoid circular deps)."""
    from citra_cache import cache
    return cache


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def get_cached_schema(
    connection_id: str,
    environment: str = "test",
) -> Optional[Dict[str, Any]]:
    """Return the cached schema dict, or ``None`` on miss / error.

    Note: uses synchronous Redis calls (sub-ms on localhost). Declared async
    to match the calling convention of the discovery pipeline.
    """
    try:
        raw = _get_cache().get(_cache_key(connection_id, environment))
        if raw:
            return json.loads(raw)
    except Exception as e:
        logger.debug("Schema cache GET failed for %s: %s", connection_id, e)
    return None


async def cache_schema(
    connection_id: str,
    environment: str,
    schema: Dict[str, Any],
    ttl: int | None = None,
) -> None:
    """Store a schema dict in the cache with TTL.

    Note: uses synchronous Redis calls. Declared async to match the calling
    convention of the discovery pipeline.
    """
    try:
        _get_cache().set(
            _cache_key(connection_id, environment),
            json.dumps(schema, default=str),
            ex=ttl or SCHEMA_CACHE_TTL,
        )
    except Exception as e:
        logger.debug("Schema cache SET failed for %s: %s", connection_id, e)


async def invalidate_schema(connection_id: str, environment: str | None = None) -> None:
    """Delete cached schema(s) for a connection.

    If *environment* is ``None`` both ``test`` and ``prod`` caches are cleared.
    """
    try:
        c = _get_cache()
        if environment:
            c.delete(_cache_key(connection_id, environment))
        else:
            c.delete(
                _cache_key(connection_id, "test"),
                _cache_key(connection_id, "prod"),
            )
    except Exception as e:
        logger.debug("Schema cache invalidation failed for %s: %s", connection_id, e)


# ---------------------------------------------------------------------------
# Orchestrator: cache-aware discovery
# ---------------------------------------------------------------------------

async def get_or_discover_schema(
    tool_name: str,
    connection_id: str,
    org_id: str,
    owner_id: str = "",
    environment: str = "test",
    owner_type: str = "",
    workflow_context: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """Three-tier schema resolution: workflow context → Redis cache → remote discovery.

    *workflow_context* is the output of ``extract_workflow_context()`` keyed by
    ``"{conn_type}:{connection_id}"``.  When it already contains tables/columns
    for this connection, remote introspection is skipped entirely.

    Returns ``None`` only when the tool has no discovery function.
    Discovery errors produce a dict with an ``"error"`` key (non-fatal).
    """
    from .schema_discovery import DISCOVERY_FUNCTIONS

    discover_fn = DISCOVERY_FUNCTIONS.get(tool_name)

    # ── Tier 1: Workflow context (free, instant, always fresh) ─────────
    wf_schema = _schema_from_workflow_context(tool_name, connection_id, workflow_context)
    if wf_schema is not None:
        # If workflow context gives us enough (tables + columns), return directly.
        # We still allow Tier 2/3 to *supplement* if the workflow context is thin.
        if _is_sufficient_context(wf_schema):
            return wf_schema

    # ── Tier 2: Redis cache (fast) ────────────────────────────────────
    if discover_fn is not None:
        cached = await get_cached_schema(connection_id, environment)
        if cached is not None:
            # Merge workflow context on top of cached schema (workflow wins)
            if wf_schema:
                cached = _merge_schemas(wf_schema, cached)
            return cached

    # ── Tier 3: Remote introspection (slow) ───────────────────────────
    if discover_fn is not None:
        schema = await discover_fn(connection_id, org_id, owner_id, environment, owner_type=owner_type)

        # Cache even error results briefly (60 s)
        if "error" in schema:
            await cache_schema(connection_id, environment, schema, ttl=60)
        else:
            await cache_schema(connection_id, environment, schema)

        # Merge workflow context on top
        if wf_schema:
            schema = _merge_schemas(wf_schema, schema)
        return schema

    # No discovery function — return workflow context if available, else None
    return wf_schema


# ---------------------------------------------------------------------------
# Helpers for three-tier resolution
# ---------------------------------------------------------------------------

_TOOL_CONN_TYPE = {
    "sql_query": "sql",
    "nosql_query": "nosql",
    "bucket_read": "bucket",
    "sftp_read": "sftp",
    "http_request": "api",
}


def _schema_from_workflow_context(
    tool_name: str,
    connection_id: str,
    workflow_context: Optional[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """Extract a schema dict from workflow_context for this tool+connection."""
    if not workflow_context or not connection_id:
        return None

    conn_type = _TOOL_CONN_TYPE.get(tool_name)
    if not conn_type:
        return None

    key = f"{conn_type}:{connection_id}"
    wf_entry = workflow_context.get(key)
    if not wf_entry:
        return None

    # Convert workflow context entry into the same schema format as discovery
    schema: Dict[str, Any] = {"_from_workflow": True}

    if conn_type == "sql":
        tables_mentioned = wf_entry.get("tables_mentioned", [])
        columns_seen = wf_entry.get("columns_seen", [])
        queries = wf_entry.get("queries", [])

        schema["type"] = "sql"
        schema["tables_mentioned"] = tables_mentioned
        schema["columns_seen"] = columns_seen
        schema["queries"] = queries

        # Build a mini tables list for the formatter
        if tables_mentioned and columns_seen:
            schema["tables"] = [
                {"name": tbl, "columns": [{"name": c, "type": "?", "nullable": True} for c in columns_seen]}
                for tbl in tables_mentioned
            ]

    elif conn_type == "nosql":
        schema["type"] = "nosql"
        schema["database"] = wf_entry.get("database", "")
        schema["collections_mentioned"] = wf_entry.get("collections_mentioned", [])
        schema["fields_seen"] = wf_entry.get("fields_seen", [])

        if schema["collections_mentioned"]:
            schema["collections"] = [
                {"name": c, "sample_fields": schema["fields_seen"], "estimated_count": "?"}
                for c in schema["collections_mentioned"]
            ]

    elif conn_type == "bucket":
        schema["type"] = "bucket"
        schema["bucket"] = wf_entry.get("bucket", "")
        schema["top_level_prefixes"] = wf_entry.get("prefixes", [])

    elif conn_type == "sftp":
        schema["type"] = "sftp"
        schema["host"] = ""
        schema["paths_used"] = wf_entry.get("paths", [])

    elif conn_type == "api":
        schema["type"] = "api"
        urls = wf_entry.get("urls", [])
        schema["base_url"] = urls[0] if urls else ""
        schema["method"] = wf_entry.get("method", "GET")

    return schema


def _is_sufficient_context(schema: Dict[str, Any]) -> bool:
    """Check if workflow-derived schema has enough info to skip remote discovery."""
    stype = schema.get("type", "")
    if stype == "sql":
        return bool(schema.get("tables_mentioned") and schema.get("columns_seen"))
    if stype == "nosql":
        return bool(schema.get("collections_mentioned") and schema.get("fields_seen"))
    if stype == "bucket":
        return bool(schema.get("bucket"))
    if stype == "sftp":
        return bool(schema.get("paths_used"))
    if stype == "api":
        return bool(schema.get("base_url"))
    return False


def _merge_schemas(
    workflow_schema: Dict[str, Any],
    discovered_schema: Dict[str, Any],
) -> Dict[str, Any]:
    """Merge workflow context onto a discovered/cached schema.

    Workflow context data is added under a ``_workflow`` key so the skill
    formatter can render it separately (as "Known from this workflow").
    """
    merged = dict(discovered_schema)
    merged["_workflow"] = workflow_schema
    return merged
