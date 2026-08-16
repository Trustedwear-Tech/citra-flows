"""
MCP server node — call ANY standards-compliant Model Context Protocol server.

Replaces the removed `dept_mcp_source`, `dept_mcp_action` and
`dept_mcp_historical_pull` nodes. Those spoke a Citra dept-MCP's private
contract: they resolved sources through a Citra discovery service by
`dept_id` / `source_id`, and authenticated with a platform `SERVICE_API_KEY`.

This node speaks the public protocol instead. You give it a server URL, a
credential and a tool name; it lists or calls. It has no idea what a
"department" is, and nothing here assumes a Citra platform is running.

SAFETY NOTE
  The node it replaces enforced read-only usage by inspecting Citra discovery
  metadata for a "write verb". That metadata does not exist for a generic MCP
  server, so the check is re-expressed here in terms the protocol actually
  provides: an explicit, per-node `allow_writes` opt-in that defaults to OFF.
  A guard that silently stops guarding is worse than no guard, so this one is
  visible in the node config rather than hidden in a lookup.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List

from ..models import NodeType, NodeCategory
from . import BaseNode, NodeContext, NodeFieldSchema, register_node

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 60


@register_node
class McpServerNode(BaseNode):
    node_type = NodeType.MCP_SERVER
    category = NodeCategory.SOURCE
    label = "MCP Server"
    description = "Call a tool on any Model Context Protocol server"
    icon = "🔌"
    color = "#8b5cf6"

    @classmethod
    def get_fields(cls) -> List[NodeFieldSchema]:
        return [
            NodeFieldSchema(name="server_url", label="Server URL", type="text", required=True,
                            placeholder="https://mcp.internal/mcp",
                            help_text="Base URL of the MCP server."),
            NodeFieldSchema(name="auth_header", label="Auth Header", type="text",
                            default="Authorization",
                            help_text="Header carrying the credential. Blank if the server needs none."),
            NodeFieldSchema(name="auth_value", label="Auth Value", type="password",
                            placeholder="Bearer …",
                            help_text="Sent verbatim as the header value."),
            NodeFieldSchema(name="operation", label="Operation", type="select", default="call_tool",
                            options=[{"label": "Call a tool", "value": "call_tool"},
                                     {"label": "List available tools", "value": "list_tools"}]),
            NodeFieldSchema(name="tool_name", label="Tool Name", type="text",
                            placeholder="search_documents",
                            help_text="Required when calling a tool. Use 'List available tools' to discover names."),
            NodeFieldSchema(name="arguments", label="Arguments (JSON)", type="textarea",
                            placeholder='{"query": "{{ item.question }}"}',
                            help_text="Tool arguments. Supports {{ }} interpolation from the current item."),
            NodeFieldSchema(name="allow_writes", label="Allow write / mutating tools", type="boolean",
                            default=False,
                            help_text="OFF by default. When off, a tool whose name or declared "
                                      "annotations indicate it mutates data is refused. Turn this on "
                                      "only for a tool you have reviewed."),
            NodeFieldSchema(name="timeout_seconds", label="Timeout (s)", type="number",
                            default=DEFAULT_TIMEOUT_SECONDS),
            NodeFieldSchema(name="per_item", label="Call once per item", type="boolean", default=True,
                            help_text="On: one call per incoming item. Off: a single call for the run."),
        ]

    async def execute(self, ctx: NodeContext) -> Any:
        import httpx

        url = (ctx.config.get("server_url") or "").strip().rstrip("/")
        if not url:
            raise ValueError("MCP Server node needs a server URL.")
        operation = ctx.config.get("operation") or "call_tool"
        timeout = float(ctx.config.get("timeout_seconds") or DEFAULT_TIMEOUT_SECONDS)

        headers = {"Content-Type": "application/json"}
        h_name = (ctx.config.get("auth_header") or "").strip()
        h_val = ctx.config.get("auth_value") or ""
        if h_name and h_val:
            headers[h_name] = h_val

        async with httpx.AsyncClient(timeout=timeout) as client:
            if operation == "list_tools":
                tools = await _rpc(client, url, headers, "tools/list", {})
                items = (tools or {}).get("tools", [])
                logger.info("McpServer: %d tool(s) on %s", len(items), url)
                return self._make_output(items=items, tool_count=len(items))

            tool = (ctx.config.get("tool_name") or "").strip()
            if not tool:
                raise ValueError("MCP Server node needs a tool name when the operation is 'Call a tool'.")

            allow_writes = bool(ctx.config.get("allow_writes"))
            if not allow_writes:
                _refuse_if_mutating(tool, await _tool_annotations(client, url, headers, tool))

            raw_args = ctx.config.get("arguments") or "{}"
            sources = ctx.items if (ctx.config.get("per_item", True) and ctx.items) else [{}]

            out: List[Dict[str, Any]] = []
            for item in sources:
                args = _render_args(raw_args, item, getattr(ctx, "variables", {}) or {})
                result = await _rpc(client, url, headers, "tools/call",
                                    {"name": tool, "arguments": args})
                out.extend(_flatten_result(result))

        logger.info("McpServer: %s -> %d result item(s)", tool, len(out))
        return self._make_output(items=out, tool=tool, returned=len(out))


# ---------------------------------------------------------------------------
# Protocol helpers
# ---------------------------------------------------------------------------
async def _rpc(client, url: str, headers: Dict[str, str], method: str, params: Dict[str, Any]) -> Any:
    """One JSON-RPC 2.0 call. Raises loudly on a protocol-level error."""
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    resp = await client.post(url, headers=headers, json=payload)
    resp.raise_for_status()
    body = resp.json()
    if isinstance(body, dict) and body.get("error"):
        err = body["error"]
        raise ValueError(f"MCP server returned an error for {method}: "
                         f"{err.get('message', err)} (code {err.get('code')})")
    return (body or {}).get("result")


async def _tool_annotations(client, url: str, headers: Dict[str, str], tool: str) -> Dict[str, Any]:
    """Fetch a tool's declared annotations, best effort.

    A server that does not implement tools/list still gets guarded by the
    name-based check in _refuse_if_mutating -- absence of metadata must not be
    read as permission.
    """
    try:
        listing = await _rpc(client, url, headers, "tools/list", {})
    except Exception as exc:  # noqa: BLE001
        logger.warning("MCP tools/list unavailable (%s); falling back to name-based write check", exc)
        return {}
    for t in (listing or {}).get("tools", []):
        if t.get("name") == tool:
            return t.get("annotations") or {}
    return {}


# Verbs that PROVE a tool only reads. Everything else is treated as potentially
# mutating -- see _refuse_if_mutating for why this is an allowlist rather than a
# blocklist of write verbs.
_READ_VERBS = {"search", "get", "list", "find", "read", "query", "fetch", "lookup",
               "describe", "show", "view", "count", "check", "inspect", "browse"}


def _refuse_if_mutating(tool: str, annotations: Dict[str, Any]) -> None:
    """Block anything not PROVEN read-only, unless the node opted in.

    DEFAULT DENY, and the first version of this got it wrong. It blocked names
    matching a list of write verbs and allowed everything else -- so a tool
    called `archive_record`, published with no annotations at all, sailed
    straight through. A live test caught it. You cannot enumerate every verb
    that mutates something, so the blocklist was structurally unable to work,
    and it contradicted this module's own stated rule that absent metadata is
    never permission.

    Now a tool must PROVE it is safe, by one of two signals:
      1. `readOnlyHint: true` in the MCP annotations, or
      2. a name that leads with a read verb (search_/get_/list_/...).

    Anything else -- including a tool the server describes with no annotations
    at all -- is refused until the operator sets allow_writes for that node.
    Noisier, and correct: a false refusal costs one checkbox, a false allow
    costs data.
    """
    if annotations.get("destructiveHint") is True:
        raise ValueError(
            f"Tool '{tool}' declares destructiveHint=true. Enable "
            "'Allow write / mutating tools' on this node if that is intended."
        )
    if annotations.get("readOnlyHint") is True:
        return                                   # server states it is read-only

    parts = [p for p in tool.lower().replace("-", "_").split("_") if p]
    if parts and parts[0] in _READ_VERBS:
        return                                   # name proves it reads

    raise ValueError(
        f"Tool '{tool}' is not proven read-only: the server declares no "
        f"readOnlyHint for it and the name does not start with a read verb "
        f"({', '.join(sorted(_READ_VERBS))}). Enable 'Allow write / mutating "
        f"tools' on this node if calling it is intended."
    )


def _render_args(template: str, item: Any, variables: Dict[str, Any]) -> Dict[str, Any]:
    """Interpolate {{ item.x }} / {{ vars.y }} then parse as JSON."""
    import re

    def _resolve(expr: str) -> str:
        root, _, path = expr.strip().partition(".")
        # `vars.x` / `variables.x` address the run variables; `item.x` the current
        # item; a bare name falls back to a variable. The first version treated
        # `vars` as a variable NAME, so the documented {{ vars.question }} syntax
        # resolved to empty and the node failed on its own help text.
        if root in ("item", "items"):
            cur: Any = item
        elif root in ("var", "vars", "variables"):
            cur = variables
        else:
            cur = variables.get(root)
            path = path or ""
        for part in filter(None, path.split(".")):
            cur = cur.get(part) if isinstance(cur, dict) else getattr(cur, part, None)
            if cur is None:
                break
        return "" if cur is None else str(cur)

    rendered = re.sub(r"\{\{\s*([^}]+?)\s*\}\}", lambda m: _resolve(m.group(1)), template)
    try:
        args = json.loads(rendered or "{}")
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"Arguments did not parse as JSON after interpolation: {exc}. Rendered: {rendered[:200]}"
        ) from exc
    if not isinstance(args, dict):
        raise ValueError("Arguments must be a JSON object.")
    return args


def _flatten_result(result: Any) -> List[Dict[str, Any]]:
    """Turn an MCP tool result into workflow items.

    The protocol returns a `content` list of typed blocks. Structured JSON is
    passed through as items; text blocks are emitted as {"text": ...} so a
    downstream node always receives dicts.
    """
    if result is None:
        return []
    content = result.get("content") if isinstance(result, dict) else None
    if content is None:
        return [result] if isinstance(result, dict) else [{"value": result}]

    items: List[Dict[str, Any]] = []
    for block in content:
        if not isinstance(block, dict):
            items.append({"value": block})
            continue
        if block.get("type") == "text":
            text = block.get("text", "")
            try:
                parsed = json.loads(text)
            except (json.JSONDecodeError, TypeError):
                items.append({"text": text})
                continue
            items.extend(parsed if isinstance(parsed, list) else [parsed])
        else:
            items.append(block)
    return items
