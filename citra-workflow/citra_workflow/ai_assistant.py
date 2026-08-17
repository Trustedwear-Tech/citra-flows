# Copyright (c) 2026 Trustedwear Tech Private Limited (https://citra-ai.com)
# Author: Rohit Kumar Chandan
# SPDX-License-Identifier: BUSL-1.1
#
# Licensed under the Business Source License 1.1. Non-production use is granted;
# production use requires a commercial licence until the Change Date, after
# which this file converts to Apache-2.0. See LICENSE at the repository root.

"""
ai_assistant.py — Agentic, tool-using workflow copilot.

Unlike the single-shot generate / refine / edit-node endpoints (which force the
model to emit a full workflow JSON every turn), this runs a multi-round
tool-calling loop so the assistant ACTS on what the user actually asked:
answer a question, explain the flow, hand back a node's code, fix one node,
edit the workflow, or build a new one.

Edits are PROPOSED as operations (the UI shows an "Apply" button) — never
auto-applied — matching the recommend-then-act governance posture.

The loop is exposed as an async generator of events so the HTTP layer can
stream them (SSE). Streaming + a heartbeat is what keeps the connection warm
during the (possibly multi-minute) reasoning/tool rounds — the prod
"Failed to fetch" came from the old path sending no bytes for ~3 minutes,
after which the gateway cut the connection.

Event shape (dicts): {"type": "status"|"operation"|"validation"|"done"|"error", ...}
"""

import asyncio
import copy
import json
import logging
import os
from typing import Any, AsyncGenerator, Dict, List, Optional

from citra_llm.client import get_llm_client, get_default_model, get_llm_extra_body

from .ai_context import render_ai_context_sections, render_node_palette_section
from .workflow_reference_validator import (
    validate_workflow_references,
    detect_setup_gaps,
)

logger = logging.getLogger(__name__)

# Agentic budget. The model may read/inspect/validate across several rounds
# before answering or proposing; bounded so a confused model can't loop.
MAX_ROUNDS = int(os.getenv("WF_ASSISTANT_MAX_ROUNDS", "8"))
# Output budget per LLM call (the large-tier cap in citra_llm.oss is already
# 50k; we request the same here directly). Input is bounded by the model
# context window + the env input cap on the large tier.
MAX_OUTPUT_TOKENS = int(os.getenv("WF_ASSISTANT_MAX_OUTPUT_TOKENS", "50000"))
# Coarse guard so a giant pasted workflow can't blow the context window.
_MAX_WORKFLOW_CHARS = int(os.getenv("WF_ASSISTANT_MAX_WORKFLOW_CHARS", "300000"))

_TIER = "large"


# ── Tool schemas (OpenAI function-calling format) ───────────────────────
# Mostly READ tools so the model fetches only what it needs (keeps input
# lean). Two PROPOSE tools record a staged operation — they never mutate the
# canvas; the UI applies them on the user's click.
TOOLS: List[Dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "inspect_node",
            "description": "Return the full definition (type, label, config) of one node in the "
                           "current workflow. Use to read a node's real configuration before "
                           "answering about it, returning its code, or editing it.",
            "parameters": {
                "type": "object",
                "properties": {"node_id": {"type": "string", "description": "The node's id."}},
                "required": ["node_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_node_types",
            "description": "List every node type the builder supports (the palette), with what each "
                           "does and its config fields. Use before adding or changing node types.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_connections",
            "description": "List the caller's saved connections (id, type, name). Use only these "
                           "ids for any node's connection_id — never invent one.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "validate_workflow",
            "description": "Validate the CURRENT working copy (the canvas + the edits you've made "
                           "this turn) — references (connections, datasets, actions) and runnability. "
                           "Returns the issues to fix. Call this after editing and before finishing.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    # ── Edit tools — emit DELTAS, never the whole workflow. The server keeps a
    # working copy, applies each delta, and materialises ONE diff the user
    # Applies. Your OUTPUT must be only what changes. ──
    {
        "type": "function",
        "function": {
            "name": "add_node",
            "description": "Add ONE new node. Provide the full node (it's new). Use add_edge to wire it in.",
            "parameters": {
                "type": "object",
                "properties": {"node": {"type": "object", "description": "{id, type, label, position:{x,y}, config}"}},
                "required": ["node"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_node",
            "description": "Change fields of an EXISTING node — emit ONLY what changes, never the whole "
                           "node. Top-level keys in `changes` (label/type/position) overwrite; "
                           "`changes.config` is DEEP-MERGED into the node's config (set one config field "
                           "without resending the rest); `remove_config_keys` deletes config keys.",
            "parameters": {
                "type": "object",
                "properties": {
                    "node_id": {"type": "string"},
                    "changes": {"type": "object", "description": "Partial node, e.g. {\"label\":\"X\"} or {\"config\":{\"connection_id\":\"c1\"}}"},
                    "remove_config_keys": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["node_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "remove_node",
            "description": "Delete a node by id (and any edges touching it).",
            "parameters": {"type": "object", "properties": {"node_id": {"type": "string"}}, "required": ["node_id"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_edge",
            "description": "Connect two nodes. `source_handle` selects a branch on condition/switch nodes "
                           "(e.g. 'true', 'false', 'out-0').",
            "parameters": {
                "type": "object",
                "properties": {
                    "source": {"type": "string"}, "target": {"type": "string"},
                    "source_handle": {"type": "string"}, "label": {"type": "string"},
                },
                "required": ["source", "target"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "remove_edge",
            "description": "Remove an edge by `edge_id`, or by `source`+`target` (optionally `source_handle`).",
            "parameters": {
                "type": "object",
                "properties": {
                    "edge_id": {"type": "string"}, "source": {"type": "string"},
                    "target": {"type": "string"}, "source_handle": {"type": "string"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_workflow_meta",
            "description": "Change workflow-level fields — emit only what changes. `set_variables` merges; "
                           "`remove_variables` deletes.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"}, "description": {"type": "string"}, "icon": {"type": "string"},
                    "set_variables": {"type": "object"}, "remove_variables": {"type": "array", "items": {"type": "string"}},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_workflow",
            "description": "Replace the ENTIRE workflow with a new one. Use ONLY when building from scratch "
                           "(empty canvas) or a deliberate full rebuild — NEVER for edits (use "
                           "add_node/update_node/remove_node/add_edge/... so your output is just the delta). "
                           "You MUST put the complete workflow — a non-empty `nodes` array and its `edges` — "
                           "in the `workflow` ARGUMENT of this call. Do NOT describe the workflow only in your "
                           "message; if `workflow` is empty the call is rejected.",
            "parameters": {
                "type": "object",
                "properties": {"workflow": {"type": "object", "description": "{name, description, icon, tags, nodes, edges, variables}"}},
                "required": ["workflow"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_workflow_test",
            "description": "Execute a workflow in the TEST environment and return per-node results "
                           "(status, error, output preview) so you can reproduce a bug or VERIFY a "
                           "candidate fix before proposing it. Defaults to the current canvas; pass "
                           "`workflow` to run a corrected candidate instead. WARNING: this is a REAL "
                           "run against test connections (real writes to test systems) — tell the "
                           "user you're running a test execution before you call it.",
            "parameters": {
                "type": "object",
                "properties": {
                    "workflow": {"type": "object", "description": "Workflow to run; omit to run the current canvas."},
                    "trigger_data": {"type": "object", "description": "Optional trigger/input data for the run."},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_recent_runs",
            "description": "List this workflow's recent executions (execution_id, status, when, error). "
                           "Use to find the run the user means by 'my last run'.",
            "parameters": {
                "type": "object",
                "properties": {"limit": {"type": "integer", "description": "How many (default 5, max 20)."}},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_execution_results",
            "description": "Read the full per-node results/errors of a PAST execution by id, to "
                           "diagnose a failure that already happened without re-running.",
            "parameters": {
                "type": "object",
                "properties": {"execution_id": {"type": "string"}},
                "required": ["execution_id"],
            },
        },
    },
]


_SYSTEM_FRAMING = """\
You are an agentic copilot inside Citra AI's visual workflow builder. You help the \
user understand, fix, edit, and build workflows by REASONING and using TOOLS across \
multiple rounds, then either ANSWERING in plain language or PROPOSING a change.

Decide what to do from the user's message — do NOT assume every request is a build \
request:
- A QUESTION ("what does this do?", "why is node X failing?", "explain the flow") → \
investigate with tools if needed, then ANSWER in concise markdown. Do NOT propose or \
regenerate the workflow.
- A CODE request ("give me the Python for this node", "show the SQL") → inspect_node \
to read the real config, then return the ACTUAL code in a fenced ```python (or ```sql) \
block. Do NOT propose a change unless asked.
- A FIX/EDIT of ONE node ("fix this node", "make the email node send HTML") → \
inspect_node, then update_node with ONLY the changed fields.
- An EDIT of the WORKFLOW ("add an email step after the report", "remove the delay") → \
apply granular edits: add_node / update_node / remove_node / add_edge / remove_edge / \
set_workflow_meta.
- CREATE a workflow ("build a flow that …") → create_workflow with a new complete \
workflow (only when starting from an empty canvas / full rebuild).
- A TEST / DEBUG request ("test this", "find bugs", "why did my last run fail?") → \
  • To reproduce NOW: call run_workflow_test (runs the current working copy). \
  • For a failure that ALREADY happened: get_recent_runs, then get_execution_results on \
the run the user means, and read the failing node's error. \
  Then inspect_node the failing node, explain the ROOT CAUSE in plain language, apply \
the fix with update_node/add_node/…, then RE-RUN with run_workflow_test to confirm it \
now passes, and present the change for the user to Apply.

HOW TO EDIT — this is critical:
- You edit by emitting DELTAS, never by re-stating the whole workflow. To rename a node, \
call update_node(node_id, {"label": "New"}) — do NOT resend its config or the other \
nodes. To set one config field, update_node(node_id, {"config": {"connection_id": "c1"}}) \
— config is deep-merged, so you only send that one key. The server keeps the working \
copy and computes the final diff. Re-emitting the entire workflow for a small change is \
WRONG. create_workflow is ONLY for from-scratch builds.
- After your edits, the working copy reflects them — run_workflow_test and \
validate_workflow operate on that working copy, so you can edit then verify before the \
user Applies.

CONFIGURE EVERY NODE FULLY — this is what the user is paying for:
- The user expects a workflow they can RUN immediately without opening a single node to \
fill in settings. So configure EVERY node completely.
- Use the EXACT config field names listed under each node in the palette. Do NOT invent \
field names — the runtime ignores unknown keys (an invented `folder_path`/`file_extension`/\
`body_html`/`table_name` silently does nothing) and a REQUIRED field left unset fails the \
run. When unsure of a node's fields, call list_node_types and copy the names verbatim.
- CONFIG VALUE TYPES — match each field's declared type. A field whose type is `json` \
MUST be authored as a REAL JSON object or array, NEVER as a quoted/stringified JSON. \
Correct: "params": {"column": "score", "ascending": false}. WRONG: \
"params": "{\\"column\\": \\"score\\", \\"ascending\\": false}" (a string) — that fails \
at runtime with "string indices must be integers". This applies to Data Transform \
`params`, Validator `rules`, and every other `json`-type field. A field whose type is \
`text`/`textarea` is a plain string; a comma-separated list field (e.g. `fields_to_extract`) \
is a single comma-separated string, not an array.
- DATA TRANSFORM `rename`: `params` is a DIRECT map of {"<existing_column>": "<new_column>"} \
— the KEY is the column that exists now, the VALUE is the new name. Example: to rename \
current_role→role and _classification_label→classification, use \
{"current_role": "role", "_classification_label": "classification"}. Do NOT emit \
{"old_name": "current_role", "new_name": "role"} — that renames nothing (a silent no-op).
- NEVER leave a REQUIRED field empty and NEVER use a placeholder value. Banned values \
include `ftp.example.com`, `sftp.example.com`, `my-bucket`, `user@example.com`, \
`/data/candidates`, `results`, `your_table`, `TODO`, empty strings for required fields, \
and "No saved connection" when a fitting saved connection exists. Use real saved-connection \
ids and concrete, sensible values derived from the user's request.
- CONNECTIONS: for any `connection_id`, pick a saved connection from the AVAILABLE \
CONNECTIONS list whose type fits the node, matching by NAME when several share a type. \
Note FTP and SFTP connections often share the same `sftp` type — disambiguate by the \
connection NAME ("FTP" vs "SFTP"). The folder-reader nodes take a saved connection plus a \
directory: FTP/SFTP use `remote_dir`, S3 uses `prefix` (both REQUIRED). Leave the file \
`extensions` field BLANK unless the user named specific file types — blank reads ALL files, \
and the parser auto-OCRs images and auto-parses JSON/PDF/CSV, so a narrow extension filter \
would wrongly skip the candidate files.
- ENRICHMENT / SCORING / RANKING: when an LLM step should evaluate, score, classify, or \
add fields to EACH record (one candidate, one row, one document at a time), set \
`processing_mode: "each"` so the model runs once per record — supported by the LLM \
Processor, Classifier, and Field Extractor. On the LLM Processor ALSO set \
`merge_with_input: true` (this field exists ONLY on the LLM Processor) so every original \
field (name, email, skills…) is KEPT and the new fields are added on each record, and the \
rows flow intact to writers, exports, and email. Have the prompt return ONE JSON OBJECT \
per record (e.g. "Return a JSON object with: score, decision, reason"). Do NOT use \
`processing_mode: "all"` to make the model return a JSON ARRAY of all records — that array \
does NOT split back into per-record rows, so the database writers and exports silently \
receive ZERO rows. Prefer the built-in `data_transform` node (operation "sort") for \
ranking rather than hand-written Python, so the records keep their shape end-to-end.
- AVOID Python Code (code_block) nodes for routine data plumbing — sorting, ranking, \
counting, summarising, or building an email/HTML table. Hand-written Python frequently \
mis-shapes the data (it wraps the whole list inside a single record, so writers/exports \
then see ONE nested row instead of N candidate rows). Instead: rank with `data_transform` \
(operation "sort"), and let the Send Email node build the table itself via `{{items_table}}`. \
A code_block must output a flat LIST of record dicts assigned to `result` (one dict per \
candidate), never a single dict that contains the list. Use code_block ONLY when no \
built-in node can do the job.
- EMAIL: the Send Email node has no connection field — it sends via the platform mail \
service. When the user says "email me" / "send me", set `to` to the requesting user's \
email (given in the context below). Put the HTML report in `body_template`; include \
`{{items_table}}` to render the ranked candidate table automatically. Use exactly ONE \
email node per run. To insert a single field from the incoming record into the subject or \
body, use the BARE field name in double braces — `{{datetime}}`, `{{name}}` — NOT \
`{{item.datetime}}` or `{{data.x}}` (dotted paths are NOT interpolated and render \
literally). Supported email placeholders: `{{items_table}}`, `{{data}}`, `{{field}}` \
(first record's field), and `{{items[N].field}}`.

Running workflows:
- run_workflow_test executes a REAL run in the TEST environment (test connections, real \
writes to test systems). It is ALWAYS test — you cannot and must not run in production. \
Say "I'll run a test execution" (one line) BEFORE calling it.

Rules:
- Use read tools (inspect_node, list_node_types, list_connections) to \
ground yourself before editing — never invent node types, connection ids, or dataset \
triples; use ONLY what the tools and the context below return.
- CONNECT EVERYTHING. Every workflow needs exactly one trigger as the entry point, and \
every node must be wired into the graph with add_edge. An edge's `source` and `target` \
MUST be the EXACT id of a node that exists — copy ids verbatim from the nodes you added \
(do not guess or abbreviate). A node left unconnected will silently fail to run.
- After building or editing, ALWAYS call validate_workflow (it checks the working copy) \
and FIX every reported issue — especially connection/graph errors — before you finish. \
Do not present a workflow with unconnected nodes or dangling edges.
- Keep edits MINIMAL: change only what the request needs; preserve the user's existing \
node ids and config.
- You may call several tools across rounds. When you have nothing left to do, STOP \
calling tools and write your final answer.

Answer style (your final chat message):
- Be CONCISE. A short paragraph or a few bullets — not a report. \
Do NOT use markdown tables; they render badly in the narrow chat panel — use short \
bullet points instead. \
For a created/edited workflow: 1–2 sentences on what it does, then at most a few bullets \
of what the user should adjust (connection, table/columns, model). The workflow preview \
card already lists the nodes, so don't re-list them. End by noting they can Apply it.
"""


def build_system_prompt(context: Dict[str, Any]) -> str:
    """Agentic framing + the live node-palette and connections context."""
    prompt = _SYSTEM_FRAMING + "\n\n" + render_ai_context_sections(context)
    user_email = (context or {}).get("user_email")
    if user_email:
        prompt += (
            f"\n## Requesting user\n\nThe user making this request is `{user_email}`. "
            "When they ask to 'email me' / 'send me a summary', use this address for the "
            "email node's `to` field.\n"
        )
    return prompt


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + "\n…[truncated]"


def _node_index(workflow: Dict[str, Any]) -> str:
    """One line per node listing its id/type/label and its config FIELD NAMES —
    so even an oversized workflow exposes every node (nothing dropped). Exact
    config values are one inspect_node call away."""
    lines = []
    for n in workflow.get("nodes") or []:
        cfg = n.get("config")
        keys = list(cfg.keys()) if isinstance(cfg, dict) else []
        lines.append(
            f"- id={n.get('id')!r} type={n.get('type')!r} "
            f"label={n.get('label')!r} config_keys={keys}"
        )
    return "\n".join(lines)


def build_opening_message(
    *, prompt: str, workflow: Dict[str, Any], conversation_block: str,
    focused_node_id: Optional[str],
) -> str:
    """The first user turn: the COMPLETE current canvas + history + the request.

    The model must see every detail the user has — every node, its FULL config
    (including the user's manual edits), edges with handles, variables and meta.
    We inline the entire workflow JSON. Only if it exceeds the context budget do
    we fall back to a per-node index (every node + its config field names) plus a
    pointer to inspect_node for exact values — we never silently cut a node's
    config out of a mid-JSON string."""
    nodes = (workflow or {}).get("nodes") or []
    if not nodes:
        wf_block = "Current workflow on the canvas: EMPTY — there are no nodes yet."
    else:
        full = json.dumps(workflow, default=str)
        if len(full) <= _MAX_WORKFLOW_CHARS:
            wf_block = (
                "Current workflow on the canvas — COMPLETE JSON (every node with its "
                "full config exactly as the user has it now, all edges, variables and "
                f"meta):\n{full}"
            )
        else:
            edges_json = _truncate(json.dumps(workflow.get("edges") or [], default=str), 60000)
            vars_json = _truncate(json.dumps(workflow.get("variables") or {}, default=str), 8000)
            wf_block = (
                "Current workflow is too large to inline fully. Below is EVERY node "
                "with its config field names; call inspect_node(node_id) for any "
                "node's exact config values (nothing has been dropped — only the "
                "inline values are deferred).\n"
                f"Workflow meta: name={workflow.get('name')!r} "
                f"description={str(workflow.get('description') or '')[:500]!r}\n"
                f"Nodes:\n{_node_index(workflow)}\n"
                f"Edges:\n{edges_json}\n"
                f"Variables:\n{vars_json}"
            )

    parts = [wf_block]
    if focused_node_id:
        parts.append(f"The user has focused node `{focused_node_id}` — edits should target it.")
    if conversation_block.strip():
        parts.append(f"Conversation so far:\n{conversation_block}")
    parts.append(f"User: {prompt}")
    return "\n\n".join(parts)


# ── Tool execution ──────────────────────────────────────────────────────

def _validate_candidate(
    workflow: Dict[str, Any], context: Dict[str, Any], *, is_fresh: bool,
) -> Dict[str, Any]:
    """Reference validation + setup-gap detection + STRUCTURAL graph checks.

    The graph checks (restored from the legacy path) are what catch the
    "disconnected nodes" failure: an edge whose source/target isn't a real
    node id, a cycle, or a node wired to nothing. Without these the canvas
    silently drops the bad edges and the workflow renders unconnected."""
    errors = validate_workflow_references(
        workflow=workflow,
        connections=context.get("connections", []),
        is_fresh=is_fresh,
    )
    gaps = detect_setup_gaps(workflow, context.get("connections", []))

    nodes = workflow.get("nodes") or []
    edges = workflow.get("edges") or []
    # Edge→node references + cycle detection (legacy `_validate_dag`).
    try:
        from .router import _validate_dag
        for msg in _validate_dag(nodes, edges):
            errors.append({"code": "E_GRAPH", "message": msg})
    except Exception as exc:  # noqa: BLE001
        logger.warning("DAG validation unavailable: %s", exc)
    # Orphan detection — a node touched by no edge can't run (the executor
    # skips it). With >1 node, every node must appear on at least one edge.
    if len(nodes) > 1:
        touched = set()
        for e in edges:
            if e.get("source"):
                touched.add(e.get("source"))
            if e.get("target"):
                touched.add(e.get("target"))
        for n in nodes:
            nid = n.get("id")
            if nid and nid not in touched:
                errors.append({
                    "code": "E_ORPHAN", "node_id": nid,
                    "message": f"Node '{nid}' is not connected to any other node — add an edge.",
                })

    # Per-node config-SCHEMA checks — the highest-value gate against the
    # "nodes present but mis-configured" failure mode. The reference validator
    # above only checks connection ids / graph; it does NOT know each node's
    # field contract, so a node configured with the wrong field NAMES
    # (folder_path instead of remote_dir) or missing a REQUIRED field passes
    # reference validation but fails at runtime. We compare each node's config
    # keys against its full get_schema() field set (includes shared retry fields).
    errors.extend(_validate_node_config_schemas(nodes))

    return {"errors": errors, "setup_gaps": gaps, "is_clean": len(errors) == 0}


# Executor-GLOBAL config keys the runtime honors on ANY node regardless of the
# node's own schema — never flag these as "unknown". `continue_on_error` is read
# by the executor for every node (executor.py); the retry trio is part of every
# node's get_schema() already (BaseNode._retry_fields) so it is covered there and
# need not be listed here. `_comment`/`notes` are author annotations.
_UNIVERSAL_CONFIG_KEYS = {
    "continue_on_error", "_comment", "notes",
}


def _validate_node_config_schemas(nodes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Check each node's config against its node-type field schema.

    Emits E_CONFIG_REQUIRED for a missing REQUIRED field and E_CONFIG_UNKNOWN
    for a config key the node type does not define (which the runtime ignores —
    the classic silent mis-config). Only applies to node types that declare a
    non-empty field schema, so schemaless control nodes (triggers, merge,
    parallel_split) are never flagged."""
    out: List[Dict[str, Any]] = []
    try:
        from .nodes import get_registry
        from .models import NodeType
    except Exception:  # noqa: BLE001
        return out
    registry = get_registry()
    for n in nodes:
        nt_raw = n.get("type")
        if not nt_raw:
            continue
        try:
            cls = registry.get(NodeType(nt_raw))
        except Exception:  # noqa: BLE001 — unknown type is caught elsewhere
            cls = None
        if cls is None:
            continue
        # Skip decision uses the node's OWN declared fields: a node that declares
        # none (parallel_split / merge_wait) is a schemaless control node — accept
        # any config. But the allowed/required SETS come from get_schema(), which
        # appends the shared executor fields (max_retries / retry_delay_seconds /
        # retry_backoff) every node honors — otherwise those valid fields would be
        # wrongly reported as E_CONFIG_UNKNOWN.
        try:
            declared = cls.get_fields() or []
        except Exception:  # noqa: BLE001
            continue
        if not declared:
            continue  # schemaless control node — anything goes
        try:
            schema_fields = cls.get_schema().fields or declared
        except Exception:  # noqa: BLE001
            schema_fields = declared
        allowed = {f.name for f in schema_fields}
        required = {f.name for f in schema_fields if getattr(f, "required", False)}
        json_fields = {f.name for f in schema_fields if getattr(f, "type", None) == "json"}
        nid = n.get("id")
        cfg = n.get("config")
        if cfg is None:
            cfg = {}
        if not isinstance(cfg, dict):
            # The working copy holds the model's raw output (not yet a validated
            # NodeDefinition), so a list/string config can reach here. Reject it
            # clearly rather than skip — a non-object config fails at runtime
            # (ctx.config.get(...) on a list raises AttributeError).
            out.append({
                "code": "E_CONFIG_TYPE", "node_id": nid,
                "message": (f"Node '{nid}' ({nt_raw}) config must be a JSON object "
                            f"of field→value, but is a {type(cfg).__name__}. "
                            f"Set config to an object using these fields: {sorted(allowed)}."),
            })
            continue
        # A saved connection (connection_id) substitutes for inline connection
        # fields like host/url/remote_dir's credentials — don't demand those.
        has_conn = bool(cfg.get("connection_id"))
        for fname in sorted(required):
            present = cfg.get(fname) not in (None, "", [], {})
            if present:
                continue
            # host/url/etc. are satisfied by a saved connection.
            if has_conn and fname in {"host", "url", "bucket_name"}:
                continue
            out.append({
                "code": "E_CONFIG_REQUIRED", "node_id": nid, "field": fname,
                "message": (f"Node '{nid}' ({nt_raw}) is missing REQUIRED config "
                            f"field '{fname}'. Set it to a concrete value."),
            })
        # A `json`-type field must be a real object/array, not a stringified JSON.
        # The builder LLM sometimes emits e.g. params="{\"column\": \"score\"}" —
        # a string that parses as JSON. The runtime then crashes with "string
        # indices must be integers". Flag it here so it's fixed BEFORE Apply.
        for fname in sorted(json_fields):
            val = cfg.get(fname)
            if not isinstance(val, str):
                continue
            text = val.strip()
            if not (text.startswith("{") or text.startswith("[")):
                continue
            try:
                json.loads(text)
            except (ValueError, TypeError):
                continue
            out.append({
                "code": "E_CONFIG_TYPE", "node_id": nid, "field": fname,
                "message": (f"Node '{nid}' ({nt_raw}) config field '{fname}' is a "
                            f"`json` field but was authored as a STRINGIFIED JSON "
                            f"(a quoted string). Emit it as a REAL JSON object/array "
                            f"— e.g. {{\"column\": \"score\"}}, not "
                            f"\"{{\\\"column\\\": \\\"score\\\"}}\" — or it fails at "
                            f"runtime with 'string indices must be integers'."),
            })
        for key in cfg.keys():
            if key in allowed or key in _UNIVERSAL_CONFIG_KEYS:
                continue
            out.append({
                "code": "E_CONFIG_UNKNOWN", "node_id": nid, "field": key,
                "message": (f"Node '{nid}' ({nt_raw}) has config key '{key}' which is "
                            f"not a real field for this node type (it will be IGNORED "
                            f"at runtime). Valid fields: {sorted(allowed)}."),
            })
    return out


def _summarize_execution_doc(doc: Dict[str, Any]) -> Dict[str, Any]:
    """Compact per-node summary of an execution (object dumped to dict or a
    Mongo doc) for the model + the UI ``run_result`` event."""
    node_results = []
    for nid, r in (doc.get("node_results") or {}).items():
        rd = r if isinstance(r, dict) else (r.model_dump() if hasattr(r, "model_dump") else {})
        status = rd.get("status")
        status = status.value if hasattr(status, "value") else status
        out = rd.get("output_data")
        preview = _truncate(json.dumps(out, default=str), 500) if out is not None else None
        node_results.append({
            "node_id": nid, "status": status,
            "error": rd.get("error"), "output_preview": preview,
        })
    status = doc.get("status")
    status = status.value if hasattr(status, "value") else status
    return {
        "execution_id": doc.get("execution_id"),
        "status": status,
        "error": doc.get("error"),
        "environment": doc.get("environment"),
        "node_results": node_results,
    }


# How long run_workflow_test waits for the worker before handing the model a
# "still running" answer. The run itself is NOT cancelled on timeout — it
# finishes on the worker and lands in WorkflowExecutions like any other run.
_TEST_RUN_TIMEOUT_SECONDS = int(os.getenv("WF_AI_TEST_RUN_TIMEOUT_SECONDS", "240"))
_TEST_RUN_POLL_SECONDS = 1.0


async def _run_test_on_worker(wf, trigger_data: Dict[str, Any], jwt_token) -> Any:
    """Execute an assistant test run on Citra-Worker and await the outcome.

    The run MUST happen on the worker — the same substrate as manual, cron and
    webhook runs. Executing inline here (the old behavior) ran in the API
    container, which has no Docker socket, so any code_block node failed with
    "Code sandbox unavailable" even when the workflow was correct. The unsaved
    working copy travels inside the job payload; the worker only accepts an
    inline definition for environment='test'.

    Returns the execution-summary dict, or an error string for the model.
    """
    import uuid as _uuid
    from datetime import datetime as _dt
    from citra_mongo import get_async_mongo_client, MONGODB_DATABASE

    execution_id = str(_uuid.uuid4())
    db = get_async_mongo_client()[MONGODB_DATABASE]
    # Pre-create the execution doc (status=queued), mirroring the manual
    # /execute path, so the run is visible in run history immediately.
    await db["WorkflowExecutions"].insert_one({
        "execution_id": execution_id,
        "workflow_id": wf.workflow_id,
        "user_id": wf.user_id,
        "status": "queued",
        "trigger_data": trigger_data,
        "trigger_type": "assistant_test",
        "environment": "test",
        "started_at": _dt.utcnow(),
        "node_results": {},
        "current_node": None,
        "error": None,
    })
    try:
        from citra_queue import enqueue as _wq_enqueue, get_status as _wq_status
        job_id = _wq_enqueue("workflow.run", {
            "workflow_id": wf.workflow_id,
            "user_id": wf.user_id,
            "trigger_data": trigger_data,
            "execution_id": execution_id,
            "environment": "test",
            "jwt_token": jwt_token,
            # Unsaved working copy — the worker builds the definition from
            # this instead of loading the Workflows collection.
            "workflow_definition": wf.model_dump(mode="json"),
        }, tenant_id=wf.user_id, request_id=execution_id)
    except Exception as exc:  # noqa: BLE001
        logger.error("assistant test-run enqueue failed: %s", exc, exc_info=True)
        # Don't leave the pre-created doc stuck on "queued" — mirror the
        # manual /execute path and mark it failed.
        await db["WorkflowExecutions"].update_one(
            {"execution_id": execution_id},
            {"$set": {
                "status": "failed",
                "error": f"failed to enqueue job: {exc}",
                "completed_at": _dt.utcnow(),
            }},
        )
        return f"Test run could not start (worker queue unavailable): {exc}"

    loop = asyncio.get_event_loop()
    deadline = loop.time() + _TEST_RUN_TIMEOUT_SECONDS
    job: Optional[Dict[str, Any]] = None
    misses = 0
    while True:
        await asyncio.sleep(_TEST_RUN_POLL_SECONDS)
        job = await asyncio.to_thread(_wq_status, job_id)
        if job is None:
            # Unknown to Redis — expired or Redis restarted. A few misses in a
            # row means we can no longer track it; say so rather than spin.
            misses += 1
            if misses >= 5:
                return (
                    f"Test run status lost (job {job_id} unknown to the queue). "
                    f"Execution {execution_id} may still be running on the "
                    "worker — check the run history."
                )
        else:
            misses = 0
            if job.get("status") in ("done", "failed"):
                break
        if loop.time() > deadline:
            return (
                f"Test run still executing after {_TEST_RUN_TIMEOUT_SECONDS}s. "
                f"It continues on the worker (execution_id {execution_id}); "
                "check the run history for the final result."
            )

    doc = await db["WorkflowExecutions"].find_one({"execution_id": execution_id})
    if doc:
        doc.pop("_id", None)
    ex_status = str((doc or {}).get("status") or "").lower()
    # Statuses the executor writes once a run settles. "queued"/"running"/
    # missing here means our pre-created doc was never updated — the run's
    # real outcome is NOT in this doc, so don't summarize it as if it were.
    settled = ("completed", "failed", "cancelled", "timed_out",
               "waiting_approval", "paused")
    if ex_status not in settled:
        if job.get("status") == "failed":
            # The handler died before the executor recorded an outcome (e.g.
            # bad payload, worker crash-loop) — surface the queue error, not
            # a stale "queued" doc that would read as a hung run.
            err = job.get("last_error") or "unknown worker error"
            return f"Test run failed on the worker before completing: {err}"
        # Job "done" but the executor's Mongo save never landed
        # (_save_execution logs-and-swallows write errors). The job hash
        # still carries the handler's true result — report that status
        # instead of the stale doc.
        result = job.get("result") if isinstance(job.get("result"), dict) else {}
        detail = f"; error: {result['error']}" if result.get("error") else ""
        return (
            f"Test run finished with status "
            f"'{result.get('status') or 'unknown'}' but its execution record "
            f"was not persisted (execution_id {execution_id}{detail}) — "
            "node-level results are unavailable; check the worker logs."
        )
    return _summarize_execution_doc(doc)


# ── Working-copy mutators — apply one DELTA to the working copy in place. ──
# The model emits deltas (not the whole workflow); the server applies them
# here and computes the diff at the end. Each returns a short ack/error string.

def _wc_find(wc: Dict[str, Any], node_id: str) -> Optional[Dict[str, Any]]:
    for n in wc.get("nodes") or []:
        if n.get("id") == node_id:
            return n
    return None


def _wc_add_node(wc: Dict[str, Any], node: Dict[str, Any]) -> str:
    nid = (node or {}).get("id")
    if not nid:
        return "Error: the node needs an 'id'."
    if _wc_find(wc, nid):
        return f"Error: node '{nid}' already exists — use update_node."
    wc.setdefault("nodes", []).append(node)
    return f"Added node '{nid}'."


def _wc_update_node(wc, node_id, changes, remove_config_keys) -> str:
    n = _wc_find(wc, node_id)
    if not n:
        return f"Error: no node '{node_id}'. Use add_node to create it first, or copy an existing id."
    # The model sometimes passes `changes` (or its `config`) as a JSON STRING
    # instead of an object. Tolerate that rather than crashing the whole turn —
    # parse it, and if it still isn't a dict, tell the model how to re-issue.
    if isinstance(changes, str):
        try:
            changes = json.loads(changes)
        except (ValueError, TypeError):
            return ("Error: `changes` must be a JSON object like "
                    "{\"config\": {\"table\": \"x\"}}, not a string. Re-issue update_node.")
    if changes is not None and not isinstance(changes, dict):
        return "Error: `changes` must be a JSON object. Re-issue update_node with an object."
    for k, v in (changes or {}).items():
        if k == "config":
            if isinstance(v, str):
                try:
                    v = json.loads(v)
                except (ValueError, TypeError):
                    return ("Error: `config` must be a JSON object, not a string. "
                            "Re-issue update_node with config as an object.")
            if isinstance(v, dict):
                cfg = dict(n.get("config") or {})
                cfg.update(v)  # deep-merge one level — set a field without resending the rest
                n["config"] = cfg
            else:
                n["config"] = v
        else:
            n[k] = v
    if remove_config_keys and isinstance(n.get("config"), dict):
        for key in remove_config_keys:
            n["config"].pop(key, None)
    return f"Updated node '{node_id}'."


def _wc_remove_node(wc, node_id) -> str:
    nodes = wc.get("nodes") or []
    if not any(x.get("id") == node_id for x in nodes):
        return f"Error: no node '{node_id}'."
    wc["nodes"] = [x for x in nodes if x.get("id") != node_id]
    wc["edges"] = [e for e in (wc.get("edges") or [])
                   if e.get("source") != node_id and e.get("target") != node_id]
    return f"Removed node '{node_id}' and its edges."


def _wc_add_edge(wc, source, target, source_handle, label) -> str:
    if not _wc_find(wc, source) or not _wc_find(wc, target):
        return f"Error: both '{source}' and '{target}' must exist before connecting them."
    edge = {
        "id": f"{source}->{target}" + (f":{source_handle}" if source_handle else ""),
        "source": source, "target": target,
    }
    if source_handle:
        edge["source_handle"] = source_handle
    if label:
        edge["label"] = label
    wc.setdefault("edges", []).append(edge)
    return f"Connected '{source}' → '{target}'."


def _wc_remove_edge(wc, edge_id, source, target, source_handle) -> str:
    edges = wc.get("edges") or []

    def _match(e):
        if edge_id:
            return e.get("id") == edge_id
        if source and target:
            return (e.get("source") == source and e.get("target") == target
                    and (not source_handle or e.get("source_handle") == source_handle))
        return False

    kept = [e for e in edges if not _match(e)]
    removed = len(edges) - len(kept)
    wc["edges"] = kept
    return f"Removed {removed} edge(s)." if removed else "No matching edge to remove."


def _wc_set_meta(wc, name, description, icon, set_variables, remove_variables) -> str:
    if name is not None:
        wc["name"] = name
    if description is not None:
        wc["description"] = description
    if icon is not None:
        wc["icon"] = icon
    if set_variables:
        v = dict(wc.get("variables") or {})
        v.update(set_variables)
        wc["variables"] = v
    if remove_variables and isinstance(wc.get("variables"), dict):
        for k in remove_variables:
            wc["variables"].pop(k, None)
    return "Updated workflow settings."


def _build_edit_operation(
    current_workflow: Dict[str, Any], wc: Dict[str, Any], context: Dict[str, Any],
) -> Dict[str, Any]:
    """Materialise the accumulated deltas into ONE Apply-able operation:
    the resulting workflow snapshot + the server-computed diff + validation.
    The model never emitted this whole thing — it emitted deltas; we built it."""
    has_existing = bool(current_workflow.get("nodes"))
    validation = _validate_candidate(wc, context, is_fresh=not has_existing)
    diff = None
    if has_existing:
        from .router import _diff_workflows  # lazy — avoids load-time cycle
        diff = _diff_workflows(current_workflow, wc)
    # Diagnostic: log the proposed node ids, the actual edge pairs, and any
    # graph issues so disconnected-workflow reports can be traced from the log
    # (did the model emit edges whose endpoints aren't real node ids?).
    _node_ids = [n.get("id") for n in (wc.get("nodes") or [])]
    _edges = [f"{e.get('source')}->{e.get('target')}"
              + (f"[{e.get('source_handle')}]" if e.get("source_handle") else "")
              for e in (wc.get("edges") or [])]
    _issues = [e.get("message") or e.get("code") for e in validation["errors"]]
    logger.info(
        "WF assistant proposal: %d node(s) %s | %d edge(s) %s | clean=%s issues=%s",
        len(_node_ids), _node_ids, len(_edges), _edges, validation["is_clean"], _issues,
    )
    return {
        "type": "workflow",
        "workflow": {k: wc.get(k) for k in
                     ("name", "description", "icon", "tags", "nodes", "edges", "variables")},
        "diff": diff,
        "validation": {"errors": validation["errors"], "is_clean": validation["is_clean"]},
        "setup_gaps": validation["setup_gaps"],
    }


async def _execute_tool(
    name: str,
    args: Dict[str, Any],
    *,
    current_workflow: Dict[str, Any],
    context: Dict[str, Any],
    edit_state: Dict[str, Any],
    run_results: List[Dict[str, Any]],
    identity: Dict[str, Any],
) -> str:
    """Run one tool call. Read tools return data; edit tools mutate the working
    copy in ``edit_state['wc']`` (delta-based — the model emits only what
    changes); run_workflow_test appends to ``run_results``. Returns the tool
    result content string."""
    if name == "inspect_node":
        # Read the working copy so the model sees its own in-progress edits.
        nid = args.get("node_id")
        for n in (edit_state["wc"].get("nodes") or []):
            if n.get("id") == nid:
                return json.dumps(n, default=str)
        return f"No node with id '{nid}' in the current workflow."

    if name == "list_node_types":
        return render_node_palette_section()

    if name == "list_connections":
        conns = context.get("connections", []) or []
        slim = [{"id": c.get("id"), "type": c.get("type"), "name": c.get("name")} for c in conns]
        return json.dumps(slim, default=str) if slim else "No saved connections."

    if name == "validate_workflow":
        # Validate the working copy (canvas + this turn's edits).
        result = _validate_candidate(
            edit_state["wc"], context, is_fresh=not bool(current_workflow.get("nodes")),
        )
        return json.dumps(result, default=str)

    if name in ("add_node", "update_node", "remove_node", "add_edge",
                "remove_edge", "set_workflow_meta", "create_workflow"):
        wc = edit_state["wc"]
        if name == "add_node":
            msg = _wc_add_node(wc, args.get("node") or {})
        elif name == "update_node":
            msg = _wc_update_node(
                wc, args.get("node_id"), args.get("changes"), args.get("remove_config_keys"),
            )
        elif name == "remove_node":
            msg = _wc_remove_node(wc, args.get("node_id"))
        elif name == "add_edge":
            msg = _wc_add_edge(
                wc, args.get("source"), args.get("target"),
                args.get("source_handle"), args.get("label"),
            )
        elif name == "remove_edge":
            msg = _wc_remove_edge(
                wc, args.get("edge_id"), args.get("source"),
                args.get("target"), args.get("source_handle"),
            )
        elif name == "set_workflow_meta":
            msg = _wc_set_meta(
                wc, args.get("name"), args.get("description"), args.get("icon"),
                args.get("set_variables"), args.get("remove_variables"),
            )
        else:  # create_workflow — full replace (from-scratch builds only)
            new = args.get("workflow") or {}
            if not (new.get("nodes") or []):
                # The model "described" a workflow but didn't put it in the tool
                # args (empty/truncated). Fail loud so it re-calls with the real
                # payload IN THE SAME TURN — otherwise we'd emit a 0-node
                # proposal with no Apply button and a prose answer that lies.
                msg = ("Error: create_workflow requires the COMPLETE workflow in the "
                       "'workflow' argument — a non-empty 'nodes' array (and 'edges'). "
                       "You passed none. Re-call create_workflow now with the full nodes "
                       "and edges in the tool ARGUMENTS, not described in your message.")
            else:
                edit_state["wc"] = {
                    "name": new.get("name", wc.get("name", "AI Workflow")),
                    "description": new.get("description", ""),
                    "icon": new.get("icon", "🤖"),
                    "tags": new.get("tags", []),
                    "nodes": new.get("nodes", []),
                    "edges": new.get("edges", []),
                    "variables": new.get("variables", {}),
                }
                msg = f"Created workflow with {len(new.get('nodes'))} node(s)."
        if not msg.startswith("Error"):
            edit_state["edited"] = True
        return msg

    if name == "run_workflow_test":
        from .models import WorkflowDefinition
        # Default to the working copy so the model can edit-then-verify.
        wf_dict = dict(args.get("workflow") or edit_state["wc"])
        wf_dict.setdefault("name", wf_dict.get("name") or "AI Test Run")
        wf_dict.setdefault("workflow_id", identity.get("workflow_id") or "ai-test")
        wf_dict.setdefault("user_id", identity.get("user_id") or "ai-assistant")
        if identity.get("org_id"):
            wf_dict.setdefault("org_id", identity["org_id"])
        if identity.get("dept_ids"):
            wf_dict.setdefault("dept_ids", identity["dept_ids"])
        try:
            wf = WorkflowDefinition(**wf_dict)
        except Exception as exc:  # noqa: BLE001
            return f"Could not build a runnable workflow: {exc}"
        # Mint the workflow's org execution identity (same as manual/cron runs).
        token = None
        try:
            from citra_auth import mint_workflow_org_token
            token = mint_workflow_org_token(
                workflow_id=wf.workflow_id, org_id=getattr(wf, "org_id", None),
                dept_ids=getattr(wf, "dept_ids", None),
                author_email=identity.get("author_email") or identity.get("user_id"),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("test-run token mint failed: %s", exc)
        summary = await _run_test_on_worker(
            wf, args.get("trigger_data") or {}, token,
        )
        if isinstance(summary, str):
            return summary  # error/still-running message for the model
        run_results.append(summary)
        return json.dumps(summary, default=str)

    if name == "get_recent_runs":
        wfid = identity.get("workflow_id")
        if not wfid or wfid == "ai-test":
            return "This workflow isn't saved yet, so it has no run history. Use run_workflow_test to run it."
        limit = max(1, min(int(args.get("limit") or 5), 20))
        from citra_mongo import get_async_mongo_client, MONGODB_DATABASE
        db = get_async_mongo_client()[MONGODB_DATABASE]
        cursor = db["WorkflowExecutions"].find(
            {"workflow_id": wfid},
            {"execution_id": 1, "status": 1, "started_at": 1, "error": 1, "environment": 1},
        ).sort("started_at", -1).limit(limit)
        runs = [{
            "execution_id": d.get("execution_id"), "status": d.get("status"),
            "started_at": str(d.get("started_at")), "environment": d.get("environment"),
            "error": d.get("error"),
        } async for d in cursor]
        return json.dumps(runs, default=str) if runs else "No runs recorded for this workflow yet."

    if name == "get_execution_results":
        exec_id = args.get("execution_id")
        from citra_mongo import get_async_mongo_client, MONGODB_DATABASE
        db = get_async_mongo_client()[MONGODB_DATABASE]
        doc = await db["WorkflowExecutions"].find_one({"execution_id": exec_id})
        if not doc:
            return f"No execution found with id '{exec_id}'."
        # Org-scope: only read runs of the workflow the user is working on.
        wfid = identity.get("workflow_id")
        if wfid and wfid != "ai-test" and doc.get("workflow_id") != wfid:
            return "That execution belongs to a different workflow."
        doc.pop("_id", None)
        return json.dumps(_summarize_execution_doc(doc), default=str)

    return f"Unknown tool: {name}"


# ── Agent loop ──────────────────────────────────────────────────────────

def _chat_once(client, model: str, messages: List[Dict[str, Any]], extra_body: dict):
    """One tool-enabled chat completion (sync; called via asyncio.to_thread)."""
    kwargs: Dict[str, Any] = {
        "model": model,
        "messages": messages,
        "max_tokens": MAX_OUTPUT_TOKENS,
        "temperature": 0.3,
        "tools": TOOLS,
        "tool_choice": "auto",
    }
    if extra_body:
        kwargs["extra_body"] = extra_body
    return client.chat.completions.create(**kwargs)


def _status_for_tool(name: str, args: Dict[str, Any]) -> str:
    return {
        "inspect_node": f"Inspecting node `{args.get('node_id', '')}`…",
        "list_node_types": "Reviewing available node types…",
        "list_connections": "Checking your connections…",
        "validate_workflow": "Validating the workflow…",
        "add_node": f"Adding node `{(args.get('node') or {}).get('id', '')}`…",
        "update_node": f"Editing node `{args.get('node_id', '')}`…",
        "remove_node": f"Removing node `{args.get('node_id', '')}`…",
        "add_edge": f"Connecting `{args.get('source', '')}` → `{args.get('target', '')}`…",
        "remove_edge": "Removing a connection…",
        "set_workflow_meta": "Updating workflow settings…",
        "create_workflow": "Building the workflow…",
        "run_workflow_test": "Running a test execution…",
        "get_recent_runs": "Looking up recent runs…",
        "get_execution_results": "Reading the run results…",
    }.get(name, f"Running {name}…")


async def run_workflow_assistant(
    *,
    prompt: str,
    workflow: Dict[str, Any],
    conversation_block: str,
    focused_node_id: Optional[str],
    context: Dict[str, Any],
    user_id: str,
    user_email: str = "",
    workflow_id: Optional[str] = None,
    org_id: Optional[str] = None,
    dept_ids: Optional[List[str]] = None,
    author_email: Optional[str] = None,
) -> AsyncGenerator[Dict[str, Any], None]:
    """Agentic tool-calling loop. Yields event dicts the HTTP layer streams."""
    model = get_default_model(_TIER)
    extra_body = get_llm_extra_body(model, tier=_TIER)
    client = get_llm_client(async_=False, tier=_TIER)

    messages: List[Dict[str, Any]] = [
        {"role": "system", "content": build_system_prompt(context)},
        {"role": "user", "content": build_opening_message(
            prompt=prompt, workflow=workflow,
            conversation_block=conversation_block, focused_node_id=focused_node_id,
        )},
    ]
    current_workflow = workflow or {"nodes": [], "edges": [], "variables": {}}
    # Working copy the delta edit tools mutate; the net diff is computed at the
    # end. ``edited`` flips True once any edit tool ran.
    edit_state: Dict[str, Any] = {"wc": copy.deepcopy(current_workflow), "edited": False}
    run_results: List[Dict[str, Any]] = []
    # Identity used by run_workflow_test to mint the workflow's org execution
    # token and by the past-run tools to scope reads.
    identity = {
        "workflow_id": workflow_id,
        "org_id": org_id,
        "dept_ids": dept_ids or [],
        "author_email": author_email or user_email or user_id,
        "user_id": user_id,
    }

    in_tok = out_tok = cached_tok = 0
    repaired = False  # one forced graph-repair round max

    logger.info(
        "WF assistant turn: user=%s wf=%s focus=%s | canvas %d node(s)/%d edge(s) | prompt=%r",
        user_id, workflow_id, focused_node_id,
        len(current_workflow.get("nodes") or []), len(current_workflow.get("edges") or []),
        (prompt or "")[:300],
    )

    final_message = None
    try:
        for round_no in range(1, MAX_ROUNDS + 1):
            yield {"type": "status", "text": "Thinking…" if round_no == 1 else "Working…"}
            # The completion can occasionally come back empty (no `choices`) on a
            # provider hiccup or a truncated upstream response. Without a guard
            # `resp.choices[0]` raises a cryptic "'NoneType' object is not
            # subscriptable" and the whole turn dies. Retry a couple of times
            # before giving up so a transient blip doesn't lose the build.
            resp = None
            for attempt in range(3):
                resp = await asyncio.to_thread(_chat_once, client, model, messages, extra_body)
                if resp is not None and getattr(resp, "choices", None):
                    break
                logger.warning("WF assistant round %d: empty LLM response (attempt %d/3)",
                               round_no, attempt + 1)
                await asyncio.sleep(1.5 * (attempt + 1))
            if resp is None or not getattr(resp, "choices", None):
                raise RuntimeError(
                    "The model returned an empty response three times in a row. "
                    "This is usually a transient provider issue — please try again."
                )

            usage = getattr(resp, "usage", None)
            if usage:
                in_tok += usage.prompt_tokens or 0
                out_tok += usage.completion_tokens or 0
                details = getattr(usage, "prompt_tokens_details", None)
                if details:
                    cached_tok += getattr(details, "cached_tokens", 0) or 0

            msg = resp.choices[0].message
            tool_calls = getattr(msg, "tool_calls", None)

            if not tool_calls:
                # Before accepting the final answer: if edits were made and the
                # graph is broken (dangling edge / orphan node / cycle), feed the
                # issues back ONCE so the model fixes connections rather than
                # leaving a workflow whose edges the canvas will silently drop.
                if edit_state["edited"] and not repaired:
                    v = _validate_candidate(
                        edit_state["wc"], context,
                        is_fresh=not bool(current_workflow.get("nodes")),
                    )
                    fixable = [e for e in v["errors"]
                               if e.get("code") in ("E_GRAPH", "E_ORPHAN",
                                                    "E_CONFIG_REQUIRED", "E_CONFIG_UNKNOWN",
                                                    "E_CONFIG_TYPE")]
                    if fixable:
                        repaired = True
                        messages.append({"role": "assistant", "content": msg.content or ""})
                        messages.append({"role": "user", "content": (
                            "Before finishing, FIX these problems. For connection/graph issues, "
                            "every node must be connected and every edge must reference an "
                            "existing node id. For config issues, use the EXACT field names from "
                            "the node palette — rename wrong keys and fill missing REQUIRED "
                            "fields with concrete values (no placeholders):\n"
                            + "\n".join("- " + gi["message"] for gi in fixable)
                            + "\nUse update_node / add_edge / remove_node, then finish."
                        )})
                        yield {"type": "status", "text": "Fixing node configuration…"}
                        continue
                # Final answer — the model stopped calling tools.
                final_message = (msg.content or "").strip() or "Done."
                break

            # Append the assistant turn (with its tool calls) then each result.
            messages.append(msg)
            for tc in tool_calls:
                fname = tc.function.name
                raw_args = tc.function.arguments or "{}"
                args_ok = True
                try:
                    fargs = json.loads(raw_args)
                except json.JSONDecodeError:
                    fargs, args_ok = {}, False
                yield {"type": "status", "text": _status_for_tool(fname, fargs)}
                if not args_ok:
                    # Truncated/invalid tool arguments — tell the model to re-issue
                    # the call with complete JSON rather than running with empty args
                    # (this is how a big create_workflow payload silently became 0 nodes).
                    logger.warning("WF assistant round %d: tool=%s INVALID args (len=%d)",
                                   round_no, fname, len(raw_args))
                    messages.append({"role": "tool", "tool_call_id": tc.id, "content": (
                        f"Error: the arguments for {fname} were not valid JSON (likely "
                        "truncated). Re-issue the call with complete, valid JSON arguments."
                    )})
                    continue
                logger.info("WF assistant round %d: tool=%s args=%s",
                            round_no, fname, _truncate(json.dumps(fargs, default=str), 400))
                result = await _execute_tool(
                    fname, fargs,
                    current_workflow=current_workflow, context=context,
                    edit_state=edit_state, run_results=run_results, identity=identity,
                )
                logger.info("WF assistant round %d: tool=%s -> %s",
                            round_no, fname, _truncate(str(result), 400))
                # Surface a test-run result (per-node pass/fail) immediately.
                if fname == "run_workflow_test" and run_results:
                    yield {"type": "run_result", "run": run_results[-1]}
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": result,
                })
        else:
            final_message = ("I ran out of steps before finishing. Here's what I prepared "
                             "so far — try narrowing the request.")

        # Materialise the accumulated deltas into ONE Apply-able operation. The
        # model emitted only deltas; the diff/snapshot is built here.
        operations: List[Dict[str, Any]] = []
        if edit_state["edited"]:
            op = _build_edit_operation(current_workflow, edit_state["wc"], context)
            operations = [op]
            yield {"type": "operation", "operation": op}
            if not op["validation"]["is_clean"]:
                yield {"type": "validation", "validation": op["validation"]}
        logger.info(
            "WF assistant done: edited=%s repaired=%s operations=%d answer_chars=%d",
            edit_state["edited"], repaired, len(operations), len(final_message or ""),
        )
        yield {"type": "done", "message": final_message or "Done.", "operations": operations}
    except Exception as exc:  # noqa: BLE001 — fail loud to the client
        logger.error("Workflow assistant failed for user %s: %s", user_id, exc, exc_info=True)
        yield {"type": "error", "message": f"Assistant error: {exc}"}
        return
    finally:
        # Track usage once at the end (best-effort billing).
        try:
            from citra_llm.oss import _track_usage
            _track_usage(
                user_id=user_id, user_email=user_email, model=model,
                input_tokens=in_tok, output_tokens=out_tok,
                cached_tokens=cached_tok, caller="WF_ASSISTANT",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Workflow assistant usage tracking failed: %s", exc)
