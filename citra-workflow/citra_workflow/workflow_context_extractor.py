# Copyright (c) 2026 Trustedwear Tech Private Limited (https://citra-ai.com)
# Author: Rohit Kumar Chandan
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may not
# use this file except in compliance with the License. You may obtain a copy of
# the License at http://www.apache.org/licenses/LICENSE-2.0

"""Extract schema context from the workflow definition and upstream node outputs.

When an AI Agent node is about to execute, this module walks all its ancestor
nodes (via the ``incoming`` edge map) and extracts table names, column headers,
collection names, SQL queries, etc. from their **configs** and **outputs**.

This "Tier 1" context is free (already in memory) and always fresh, so it is
checked *before* the Redis cache or remote introspection.
"""

from __future__ import annotations
import logging
import re
from collections import deque
from typing import Any, Dict, List, Optional, Set

logger = logging.getLogger(__name__)

# Regex patterns to extract table names from SQL queries
_SQL_TABLE_PATTERNS = [
    re.compile(r'\bFROM\s+[`"\[]?(\w+)[`"\]]?', re.IGNORECASE),
    re.compile(r'\bJOIN\s+[`"\[]?(\w+)[`"\]]?', re.IGNORECASE),
    re.compile(r'\bINTO\s+[`"\[]?(\w+)[`"\]]?', re.IGNORECASE),
    re.compile(r'\bUPDATE\s+[`"\[]?(\w+)[`"\]]?', re.IGNORECASE),
]

# Source node types and the connection type they map to
_SOURCE_TYPE_MAP = {
    "sql_source": "sql",
    "mongo_source": "nosql",
    "api_source": "api",
    "s3_source": "s3",
    "sftp_source": "sftp",
}

# AI Agent nodes use different config keys for connections
_AGENT_CONN_KEYS = {
    "sql": "sql_connection_id",
    "nosql": "nosql_connection_id",
    "bucket": "bucket_connection_id",
    "sftp": "sftp_connection_id",
}


def _parse_sql_tables(query: str) -> List[str]:
    """Extract table names from a SQL query string using regex."""
    tables: list[str] = []
    seen: set[str] = set()
    for pattern in _SQL_TABLE_PATTERNS:
        for match in pattern.finditer(query):
            name = match.group(1)
            # Skip SQL keywords that might be mismatched
            if name.upper() in (
                "SELECT", "WHERE", "SET", "VALUES", "NULL",
                "TRUE", "FALSE", "AS", "ON", "AND", "OR",
            ):
                continue
            if name not in seen:
                tables.append(name)
                seen.add(name)
    return tables


def _extract_columns_from_output(output_data: Any) -> List[str]:
    """Extract column/field names from a node's output_data.

    Output format is ``{"items": [{"col1": ..., "col2": ...}, ...], "meta": {...}}``.
    We take the keys from the first item.
    """
    if not isinstance(output_data, dict):
        return []
    items = output_data.get("items")
    if not isinstance(items, list) or not items:
        return []
    first = items[0]
    if isinstance(first, dict):
        return list(first.keys())
    return []


def _get_all_ancestors(
    node_id: str,
    incoming: Dict[str, List[str]],
) -> List[str]:
    """BFS walk to find all ancestor node IDs (ordered from closest to farthest)."""
    visited: Set[str] = set()
    result: List[str] = []
    queue: deque[str] = deque()

    for parent in incoming.get(node_id, []):
        if parent not in visited:
            queue.append(parent)
            visited.add(parent)

    while queue:
        current = queue.popleft()
        result.append(current)
        for grandparent in incoming.get(current, []):
            if grandparent not in visited:
                queue.append(grandparent)
                visited.add(grandparent)

    return result


def extract_workflow_context(
    node_id: str,
    node_map: Dict[str, Any],
    node_outputs: Dict[str, Any],
    incoming: Dict[str, List[str]],
) -> Dict[str, Dict[str, Any]]:
    """Extract schema context from upstream nodes for an AI Agent.

    Walks all ancestors of *node_id* and collects table/collection names,
    column headers, SQL queries, etc. grouped by connection_id.

    Returns a dict keyed by ``"{conn_type}:{connection_id}"``::

        {
            "sql:conn-123": {
                "type": "sql",
                "connection_id": "conn-123",
                "tables_mentioned": ["applications", "users"],
                "columns_seen": ["id", "name", "email"],
                "queries": ["SELECT * FROM applications WHERE ..."]
            },
            "nosql:conn-456": {
                "type": "nosql",
                "connection_id": "conn-456",
                "database": "mydb",
                "collections_mentioned": ["applicants"],
                "fields_seen": ["_id", "name", "status"]
            }
        }

    Also includes an ``"_upstream_columns"`` key with column names from the
    immediate upstream output (regardless of source type), useful for context
    even when there is no matching connection_id.
    """
    result: Dict[str, Dict[str, Any]] = {}
    ancestors = _get_all_ancestors(node_id, incoming)

    # Immediate upstream column context (always included)
    direct_parents = incoming.get(node_id, [])
    all_upstream_columns: List[str] = []
    for pid in direct_parents:
        output = node_outputs.get(pid)
        if output:
            cols = _extract_columns_from_output(output)
            all_upstream_columns.extend(c for c in cols if c not in all_upstream_columns)
    if all_upstream_columns:
        result["_upstream_columns"] = {
            "columns": all_upstream_columns,
        }

    # Walk all ancestors and extract source-specific context
    for ancestor_id in ancestors:
        node_def = node_map.get(ancestor_id)
        if node_def is None:
            continue

        # node_def can be a NodeDefinition model or a dict — handle both
        node_type = getattr(node_def, "type", None) or (node_def.get("type") if isinstance(node_def, dict) else None)
        config = getattr(node_def, "config", None) or (node_def.get("config", {}) if isinstance(node_def, dict) else {})
        if not node_type or not config:
            continue

        # Normalize: NodeType enum → string value
        node_type_str = node_type.value if hasattr(node_type, "value") else str(node_type)
        conn_type = _SOURCE_TYPE_MAP.get(node_type_str)

        # Handle upstream AI Agent nodes — extract their tool connections
        if node_type_str == "ai_agent":
            ancestor_output = node_outputs.get(ancestor_id)
            ancestor_cols = _extract_columns_from_output(ancestor_output) if ancestor_output else []
            for agent_conn_type, config_key in _AGENT_CONN_KEYS.items():
                agent_conn_id = config.get(config_key, "")
                if not agent_conn_id:
                    continue
                key = f"{agent_conn_type}:{agent_conn_id}"
                if key not in result:
                    result[key] = {
                        "type": agent_conn_type,
                        "connection_id": agent_conn_id,
                    }
                # Propagate output columns from the agent to all its connection types
                ctx_entry = result[key]
                if agent_conn_type == "sql":
                    ctx_entry.setdefault("tables_mentioned", [])
                    ctx_entry.setdefault("columns_seen", [])
                    ctx_entry.setdefault("queries", [])
                    for c in ancestor_cols:
                        if c not in ctx_entry["columns_seen"]:
                            ctx_entry["columns_seen"].append(c)
                elif agent_conn_type == "nosql":
                    ctx_entry.setdefault("collections_mentioned", [])
                    ctx_entry.setdefault("fields_seen", [])
                    for c in ancestor_cols:
                        if c not in ctx_entry["fields_seen"]:
                            ctx_entry["fields_seen"].append(c)
            continue

        if not conn_type:
            continue

        connection_id = config.get("connection_id", "")
        if not connection_id:
            continue

        key = f"{conn_type}:{connection_id}"

        # Get or create context entry
        if key not in result:
            result[key] = {
                "type": conn_type,
                "connection_id": connection_id,
            }
        ctx_entry = result[key]

        # Extract source-specific info from config
        if conn_type == "sql":
            ctx_entry.setdefault("tables_mentioned", [])
            ctx_entry.setdefault("columns_seen", [])
            ctx_entry.setdefault("queries", [])

            query = config.get("query", "")
            if query:
                if query not in ctx_entry["queries"]:
                    ctx_entry["queries"].append(query)
                for tbl in _parse_sql_tables(query):
                    if tbl not in ctx_entry["tables_mentioned"]:
                        ctx_entry["tables_mentioned"].append(tbl)

        elif conn_type == "nosql":
            ctx_entry.setdefault("database", config.get("database", ""))
            ctx_entry.setdefault("collections_mentioned", [])
            ctx_entry.setdefault("fields_seen", [])

            coll = config.get("collection", "")
            if coll and coll not in ctx_entry["collections_mentioned"]:
                ctx_entry["collections_mentioned"].append(coll)

        elif conn_type == "api":
            ctx_entry.setdefault("urls", [])
            url = config.get("url", "")
            if url and url not in ctx_entry["urls"]:
                ctx_entry["urls"].append(url)
            ctx_entry["method"] = config.get("method", "GET")

        elif conn_type == "bucket":
            ctx_entry.setdefault("bucket", config.get("bucket", ""))
            ctx_entry.setdefault("prefixes", [])
            prefix = config.get("prefix", "")
            if prefix and prefix not in ctx_entry["prefixes"]:
                ctx_entry["prefixes"].append(prefix)

        elif conn_type == "sftp":
            ctx_entry.setdefault("paths", [])
            path = config.get("remote_path", "")
            if path and path not in ctx_entry["paths"]:
                ctx_entry["paths"].append(path)

        # Extract column/field names from output data (if this ancestor already ran)
        ancestor_output = node_outputs.get(ancestor_id)
        if ancestor_output:
            cols = _extract_columns_from_output(ancestor_output)
            if conn_type == "sql":
                for c in cols:
                    if c not in ctx_entry["columns_seen"]:
                        ctx_entry["columns_seen"].append(c)
            elif conn_type == "nosql":
                for c in cols:
                    if c not in ctx_entry["fields_seen"]:
                        ctx_entry["fields_seen"].append(c)

    return result
