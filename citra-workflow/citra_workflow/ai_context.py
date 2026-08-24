# Copyright (c) 2026 Trustedwear Tech Private Limited (https://citra-ai.com)
# Author: Rohit Kumar Chandan
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may not
# use this file except in compliance with the License. You may obtain a copy of
# the License at http://www.apache.org/licenses/LICENSE-2.0

"""AI authoring context — what the workflow-generation LLM sees about
this specific tenant before it composes a workflow.

There are three context layers, gathered fresh on every call:

1. **Node palette** — registry-driven enumeration of every node the AI is
   allowed to author. Nodes with ``ai_visible = False`` are excluded.
   Optional ``ai_authoring_hint`` is appended per node.

2. **Available connections** — the caller's saved WorkflowConnections
   (sql, mongo, api, bucket, sftp, smtp …). Without this, the AI emits
   placeholder ``connection_id: "sql1"`` strings that fail at first run.

3. (removed) the Citra dept-MCP catalogue — see PORTING.md. The nodes it
   fed (``dept_mcp_source`` / ``dept_mcp_action``) are gone; use the generic
   ``mcp_server`` and ``vector_search`` nodes instead.

The combined output is rendered as plain prose for inclusion in the
``_WORKFLOW_GEN_SYSTEM_PROMPT`` at request time. Generation is async-safe
and degrades to "no entries available" on backend outage so the AI still
produces something usable, just without the auto-wiring lift.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional, Tuple

import httpx

from .nodes import get_registry
from .models import NodeCategory

logger = logging.getLogger(__name__)


# ── 1. Node palette section ──────────────────────────────────────────


_CATEGORY_ORDER: List[Tuple[NodeCategory, str]] = [
    (NodeCategory.TRIGGER,   "Triggers (entry points — every workflow needs exactly ONE)"),
    (NodeCategory.SOURCE,    "Data Sources (read data into the pipeline)"),
    (NodeCategory.AGENT,     "AI / Agents"),
    (NodeCategory.PROCESSOR, "Processors (transform / analyze data)"),
    (NodeCategory.LOGIC,     "Logic / Flow Control"),
    (NodeCategory.OUTPUT,    "Outputs (write / send results)"),
]


def _render_node_config_fields(cls) -> List[str]:
    """Render a node's declared config fields (from ``get_fields()``) as compact
    one-liners the builder LLM can copy verbatim.

    This is what stops the model inventing field names: without the real schema
    it guesses (``folder_path``/``file_extension``) instead of the actual keys
    (``remote_dir``/``extensions``), the runtime silently ignores the unknown
    keys, and the REQUIRED field left unset fails the run. We surface the EXACT
    name, type, required flag, allowed select values, default, and help text.
    """
    try:
        fields = cls.get_fields() or []
    except Exception:  # noqa: BLE001 — a node with a broken get_fields must not break the palette
        return []
    out: List[str] = []
    for f in fields:
        parts = [f"`{f.name}`", f"({f.type}{', REQUIRED' if f.required else ''})"]
        if getattr(f, "connection_type", None):
            parts.append(f"[saved-connection type: {f.connection_type}]")
        if f.options:
            vals = [str(o.get("value")) for o in f.options
                    if isinstance(o, dict) and o.get("value") is not None]
            if vals:
                parts.append("allowed values: " + " | ".join(vals))
        if f.default not in (None, "", [], {}):
            parts.append(f"default={f.default!r}")
        line = "      - " + " ".join(parts)
        if f.help_text:
            line += f" — {f.help_text}"
        out.append(line)
    return out


def render_node_palette_section() -> str:
    """Build the 'Available Node Types' section from the live registry.

    Honors ``ai_visible`` (excluded) and appends ``ai_authoring_hint``
    when set, PLUS each node's exact config field schema. New nodes
    registered via ``@register_node`` show up the moment they're imported
    — no prompt drift.
    """
    registry = get_registry()
    # Group ai-visible nodes by category
    grouped: Dict[NodeCategory, List[type]] = {cat: [] for cat, _ in _CATEGORY_ORDER}
    for cls in registry.values():
        # `ai_visible = False` is still honoured — it just has no users today.
        if not getattr(cls, "ai_visible", True):
            continue
        if cls.category in grouped:
            grouped[cls.category].append(cls)

    lines: List[str] = []
    lines.append("## Available Node Types (by category)")
    lines.append("")
    lines.append(
        "For EACH node below, the exact config field NAMES, their types, whether "
        "they are REQUIRED, the allowed values for select fields, and defaults are "
        "listed under it. When you author a node's `config`, you MUST use these "
        "EXACT field names. The runtime IGNORES any key that is not in this list "
        "(so an invented name like `folder_path` or `file_extension` silently does "
        "nothing), and a REQUIRED field left unset FAILS the run. Fill every "
        "REQUIRED field with a concrete value — never a placeholder."
    )
    lines.append("")
    for cat, heading in _CATEGORY_ORDER:
        nodes = grouped.get(cat) or []
        if not nodes:
            continue
        lines.append(f"### {heading}")
        for cls in sorted(nodes, key=lambda c: c.node_type.value):
            label = getattr(cls, "label", "") or cls.node_type.value
            description = cls.description or label
            base = f"- {cls.node_type.value} ({label}): {description}"
            hint = getattr(cls, "ai_authoring_hint", "") or ""
            if hint:
                base += f"\n    AI HINT: {hint}"
            field_lines = _render_node_config_fields(cls)
            if field_lines:
                base += "\n    config fields (use these EXACT names):\n" + "\n".join(field_lines)
            lines.append(base)
        lines.append("")

    return "\n".join(lines)


# ── 2. Saved connections section ────────────────────────────────────


def _mask_label(name: str) -> str:
    return (name or "").strip() or "(unnamed)"


async def fetch_connections_for_ai(org_id: str) -> List[Dict[str, Any]]:
    """Return the ORG's saved connections trimmed to {id, type, name}.

    IT workflow connections are org-owned and shared across the IT team, so the
    builder sees every connection in the org (matching the Connections screen
    and the runtime resolver's org_id scope) — not just the building user's.

    Secrets are never read or returned — the Mongo projection deliberately
    excludes the encrypted ``test``/``prod`` blobs, so only id/type/name/
    description (enough to wire ``connection_id``) ever reaches the LLM. Empty
    list on database miss / resolver-unavailable.
    """
    if not org_id:
        return []
    try:
        from citra_mongo import get_async_mongo_client, MONGODB_DATABASE
    except ImportError:
        try:
            from mongodb_manager import get_async_mongo_client, MONGODB_DATABASE  # type: ignore
        except ImportError:
            logger.warning("ai_context: no mongo client available")
            return []

    client = get_async_mongo_client()
    db = client[MONGODB_DATABASE]
    cursor = db["WorkflowConnections"].find(
        {"org_id": org_id},
        {"connection_id": 1, "name": 1, "type": 1, "description": 1, "_id": 0},
    ).sort("created_at", -1)
    out: List[Dict[str, Any]] = []
    async for doc in cursor:
        out.append({
            "id": doc.get("connection_id") or "",
            "type": doc.get("type") or "",
            "name": _mask_label(doc.get("name")),
            "description": (doc.get("description") or "")[:160],
        })
    return out


def render_connections_section(connections: List[Dict[str, Any]]) -> str:
    """Render the saved-connections list for the system prompt."""
    if not connections:
        return (
            "## Available Connections\n\n"
            "(none yet — when authoring SQL / Mongo / API / Bucket / SFTP / SMTP nodes, "
            "leave `connection_id` empty and add a 'suggestions' item asking the user to "
            "register the connection in the Connections screen.)\n"
        )
    lines: List[str] = [
        "## Available Connections",
        "",
        "Reference these by `id` — NEVER invent a connection_id. If none of the listed",
        "connections fits the user's intent, leave the id empty and add a 'suggestions'",
        "item asking them to register a new connection.",
        "",
    ]
    for c in connections:
        line = f"- id={c['id']!r}  type={c['type']!r}  name={c['name']!r}"
        if c.get("description"):
            line += f"  — {c['description']}"
        lines.append(line)
    lines.append("")
    return "\n".join(lines)


def _data_discovery_url() -> Optional[str]:
    url = (
        os.getenv("DATA_DISCOVERY_SERVICE_URL")
        or os.getenv("DATA_DISCOVERY_URL")
        or os.getenv("CATALOGUE_SERVICE_URL")
    )
    return (url or "").rstrip("/") or None


# ── 5. One-shot context bundle ──────────────────────────────────────


async def gather_ai_context(
    *,
    user_id: str,
    tenant_id: str,
    dept_ids: Optional[List[str]] = None,
    auth_header: Optional[str] = None,
    query: Optional[str] = None,
) -> Dict[str, Any]:
    """Bundle every piece of context the AI authoring endpoints need.

    The router calls this once per /generate-workflow or /refine request,
    then renders the sections into the system prompt and ships the raw
    structures into the reference validator. ``query`` (the user's workflow
    goal / edit request) semantic-ranks the MCP sources so only the most
    relevant ones are injected.
    """
    import asyncio
    connections = await fetch_connections_for_ai(tenant_id)
    return {
        "connections": connections,
    }


def render_ai_context_sections(context: Dict[str, Any]) -> str:
    """Render every gathered section back-to-back for prompt inclusion."""
    return "\n".join([
        render_node_palette_section(),
        render_connections_section(context.get("connections", [])),
    ])
