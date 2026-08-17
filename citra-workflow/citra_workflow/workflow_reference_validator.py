# Copyright (c) 2026 Trustedwear Tech Private Limited (https://citra-ai.com)
# Author: Rohit Kumar Chandan
# SPDX-License-Identifier: BUSL-1.1
#
# Licensed under the Business Source License 1.1. Non-production use is granted;
# production use requires a commercial licence until the Change Date, after
# which this file converts to Apache-2.0. See LICENSE at the repository root.

"""Reference validator for AI-generated workflows.

After the LLM emits a workflow, we check every id-bearing field on every
node against the live registries (saved connections,
node-type registry). If any reference is unresolved we surface a
structured error list so the UI can highlight the broken nodes and the
user can either pick a real value or ask the AI to retry.

The checks are intentionally *additive* — they run AFTER the existing
DAG-shape check (``_validate_dag`` in router.py). DAG failures are
fatal; reference failures are surfaced but the workflow is still
returned so the user can edit it. The router decides whether to gate
or warn based on a flag.

Error codes (stable, UI-rendered):
  E_UNKNOWN_NODE_TYPE        — node.type not in registry
  E_FORBIDDEN_NODE_TYPE      — node.type is ai_visible=False (created
                               in a fresh workflow, not allowed)
  E_UNKNOWN_CONNECTION       — connection_id field references a
                               connection that doesn't exist for this
                               user
  E_CONNECTION_TYPE_MISMATCH — referenced connection exists but its
                               ``type`` doesn't match the field's
                               required ``connection_type``
  E_UNKNOWN_DATASET          — dept_mcp_*.{dept_id, source_id,
                               dataset_id} triple not in catalogue
  E_UNKNOWN_ACTION           — dept_mcp_action.action_id not in the
                               dataset's write_actions[]
  E_UNKNOWN_MCP_SOURCE       — ai_agent.mcp_tools[] entry references a
                               source_id not in the registered MCP sources
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional


def _connection_index(connections: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    return {c.get("id"): c for c in connections if c.get("id")}


def _normalize_connection_type(value: Any) -> str:
    raw = str(value or "").strip().lower()
    # Historical schemas use both names for the same S3/object-storage
    # connection family. Saved connections are stored as "bucket"; several
    # older node schemas still request "s3".
    if raw == "s3":
        return "bucket"
    return raw


def _connection_type_matches(required: Any, actual: Any) -> bool:
    return _normalize_connection_type(required) == _normalize_connection_type(actual)


def _err(code: str, node_id: str, **fields: Any) -> Dict[str, Any]:
    return {"code": code, "node_id": node_id, **fields}


def _registry_field_schemas() -> Dict[str, List[Any]]:
    """Map node_type → list of NodeFieldSchema for every registered node."""
    from .nodes import get_registry
    out: Dict[str, List[Any]] = {}
    for node_type, cls in get_registry().items():
        # A node class whose get_fields() raises is a programming error in that
        # node definition. Returning [] would silently disable field-reference
        # validation for that node type (invalid refs slip through). Fail loud.
        try:
            out[node_type.value] = list(cls.get_fields())
        except Exception as exc:
            raise RuntimeError(
                f"Failed to load field schema for node type "
                f"{node_type.value!r} ({cls.__name__}); cannot build the "
                f"reference validator with an incomplete schema."
            ) from exc
    return out


def _ai_visible_node_types() -> Dict[str, bool]:
    from .nodes import get_registry
    return {nt.value: getattr(cls, "ai_visible", True) for nt, cls in get_registry().items()}


def validate_workflow_references(
    *,
    workflow: Dict[str, Any],
    connections: List[Dict[str, Any]],
    is_fresh: bool = True,
) -> List[Dict[str, Any]]:
    """Return a list of reference errors for the AI-generated workflow.

    Empty list = workflow is reference-clean and ready to apply.

    ``is_fresh=True`` rejects nodes flagged ``ai_visible=False`` because
    a freshly generated workflow must not contain dept-flow internals.
    During /refine pass ``is_fresh=False`` to allow them to be preserved.
    """
    errors: List[Dict[str, Any]] = []

    nodes = workflow.get("nodes") or []
    if not isinstance(nodes, list):
        return errors

    field_schemas = _registry_field_schemas()
    ai_visible = _ai_visible_node_types()

    conn_idx = _connection_index(connections)

    for node in nodes:
        if not isinstance(node, dict):
            continue
        node_id = str(node.get("id") or "")
        node_type = str(node.get("type") or "")
        config = node.get("config") or {}
        if not isinstance(config, dict):
            config = {}

        # ── node-type checks ───────────────────────────────────────
        if node_type not in field_schemas:
            errors.append(_err("E_UNKNOWN_NODE_TYPE", node_id, node_type=node_type))
            continue

        if is_fresh and not ai_visible.get(node_type, True):
            errors.append(_err("E_FORBIDDEN_NODE_TYPE", node_id, node_type=node_type))
            # Still continue — surface any other issues at once.

        # ── connection_picker fields ───────────────────────────────
        for field in field_schemas[node_type]:
            f_type = getattr(field, "type", "") or ""
            f_name = getattr(field, "name", "") or ""
            if f_type != "connection_picker":
                continue
            conn_id = config.get(f_name)
            if not conn_id:
                continue  # connection_id is optional in many nodes (inline allowed)
            entry = conn_idx.get(conn_id)
            if entry is None:
                errors.append(_err(
                    "E_UNKNOWN_CONNECTION", node_id,
                    field=f_name, value=conn_id,
                ))
                continue
            required_type = getattr(field, "connection_type", None)
            if required_type and not _connection_type_matches(required_type, entry.get("type")):
                errors.append(_err(
                    "E_CONNECTION_TYPE_MISMATCH", node_id,
                    field=f_name, value=conn_id,
                    expected_type=required_type, actual_type=entry.get("type"),
                ))

    return errors


# Config keys that, when populated, mean a node carries an INLINE connection
# and therefore doesn't strictly need a saved `connection_id`. Kept small and
# generic — the only purpose is to avoid false "needs a connection" notices for
# a node the user has deliberately wired with inline credentials by hand.
_INLINE_CONN_FIELDS = (
    "connection_string", "conn_string", "connection_uri", "uri", "url",
    "host", "hostname", "endpoint", "bucket", "smtp_host", "server",
)


def detect_setup_gaps(
    workflow: Dict[str, Any],
    connections: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """Deterministic runnability gaps for an AI-built workflow.

    Distinct from ``validate_workflow_references`` — that checks ids that ARE
    present. This catches the *silent* failure mode the reference validator
    intentionally skips: a node whose ``connection_picker`` field is EMPTY and
    which has no inline connection config either, so the graph looks
    reference-clean yet cannot run.

    These are NOT errors (nothing is mis-referenced) — they are setup steps the
    user must complete, returned so the caller can surface them as
    prerequisites rather than relying on the LLM to remember to mention them.

    At generation time the AI never fills inline credentials (it doesn't know
    the user's host/password), so an empty connection_picker is a near-certain
    signal the node is unrunnable until the user wires it.
    """
    gaps: List[Dict[str, Any]] = []
    nodes = workflow.get("nodes") or []
    if not isinstance(nodes, list):
        return gaps

    field_schemas = _registry_field_schemas()
    for node in nodes:
        if not isinstance(node, dict):
            continue
        node_id = str(node.get("id") or "")
        node_type = str(node.get("type") or "")
        config = node.get("config") or {}
        if not isinstance(config, dict):
            config = {}
        fields = field_schemas.get(node_type)
        if not fields:
            continue
        for field in fields:
            if (getattr(field, "type", "") or "") != "connection_picker":
                continue
            f_name = getattr(field, "name", "") or ""
            if config.get(f_name):
                continue  # a connection IS selected — fine
            # No connection picked. If the node carries inline connection
            # config, it can still run — don't flag it.
            has_inline = any(
                str(config.get(k) or "").strip() for k in _INLINE_CONN_FIELDS
            )
            if has_inline:
                continue
            conn_type = getattr(field, "connection_type", None) or "data"
            label = node.get("label") or node_type
            # If the org already has a connection of the right type, point the
            # user at picking one; otherwise tell them to register a new one.
            has_matching = any(
                _connection_type_matches(conn_type, (c or {}).get("type"))
                for c in (connections or [])
            )
            if has_matching:
                message = (
                    f"Node '{label}' has no {conn_type} connection selected — "
                    f"pick one of your existing {conn_type} connections (or "
                    f"register a new one in the Connections screen) and set it "
                    f"on the node (field '{f_name}'). The workflow can't run "
                    f"until then."
                )
            else:
                message = (
                    f"Node '{label}' has no {conn_type} connection selected — "
                    f"register one in the Connections screen, then set it on "
                    f"the node (field '{f_name}'). The workflow can't run until "
                    f"then."
                )
            gaps.append({
                "node_id": node_id,
                "field": f_name,
                "connection_type": conn_type,
                "message": message,
            })
    return gaps


def errors_to_suggestions(errors: List[Dict[str, Any]]) -> List[str]:
    """Convert structured errors to human-readable suggestion strings the
    AI chat panel can show. The UI may also render the structured list
    directly with per-node highlights."""
    out: List[str] = []
    for e in errors:
        code = e.get("code")
        nid = e.get("node_id") or "?"
        if code == "E_UNKNOWN_NODE_TYPE":
            out.append(f"Node '{nid}': unknown type '{e.get('node_type')}'.")
        elif code == "E_FORBIDDEN_NODE_TYPE":
            out.append(
                f"Node '{nid}': '{e.get('node_type')}' is a dept-flow internal node "
                "and cannot be added by AI. Register the source in Dept Sources instead."
            )
        elif code == "E_UNKNOWN_CONNECTION":
            out.append(
                f"Node '{nid}': field '{e.get('field')}' references "
                f"connection '{e.get('value')}' which does not exist. "
                "Pick a saved connection or create one in the Connections screen."
            )
        elif code == "E_CONNECTION_TYPE_MISMATCH":
            out.append(
                f"Node '{nid}': field '{e.get('field')}' expected a "
                f"'{e.get('expected_type')}' connection but '{e.get('value')}' "
                f"is type '{e.get('actual_type')}'."
            )

    return out
