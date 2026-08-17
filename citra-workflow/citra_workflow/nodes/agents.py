# Copyright (c) 2026 Trustedwear Tech Private Limited (https://citra-ai.com)
# Author: Rohit Kumar Chandan
# SPDX-License-Identifier: BUSL-1.1
#
# Licensed under the Business Source License 1.1. Non-production use is granted;
# production use requires a commercial licence until the Change Date, after
# which this file converts to Apache-2.0. See LICENSE at the repository root.

"""AI Agent nodes — autonomous agents with tool calling and structured output."""

from __future__ import annotations
import asyncio
import json
import logging
import os
import re
import threading
import uuid
from logging.handlers import RotatingFileHandler
from typing import Any, Dict, List, Optional

from ..models import NodeType, NodeCategory, NodeFieldSchema
from . import BaseNode, NodeContext, register_node, interpolate_variables
from .processors import _tier_field, _coerce_tier, _DEFAULT_WORKFLOW_TIER
from ..config import (
    MAX_AGENT_ITERATIONS, MAX_AGENT_INPUT_SIZE,
    MONGO_CONNECT_TIMEOUT_MS,
    HTTP_TIMEOUT_WEB_SEARCH, HTTP_TIMEOUT_TOOL, FTP_CONNECT_TIMEOUT,
    DEFAULT_AGENT_SQL_ROWS, DEFAULT_AGENT_NOSQL_LIMIT,
    MAX_AGENT_FILE_READ_SIZE, DEFAULT_AGENT_BUCKET_LIST_LIMIT,
    DEFAULT_AGENT_SFTP_LIST_LIMIT,
    AGENT_TOOL_CALL_TIMEOUT,
)

logger = logging.getLogger(__name__)


def _enforce_size_limit(value: str, limit: int, label: str) -> str:
    """Raise ValueError if value exceeds limit. Never truncate silently."""
    if len(value) > limit:
        raise ValueError(
            f"{label} ({len(value)} chars) exceeds limit ({limit}). "
            f"Reduce data size upstream or increase the corresponding WF_MAX_* env var."
        )
    return value

# ============================================================================
# Built-in Tools that agents can invoke
# ============================================================================

AVAILABLE_TOOLS = {
    "web_search": {
        "name": "web_search",
        "label": "Web Search",
        "description": "Search the web via DuckDuckGo for real-time information",
        "icon": "🔍",
    },
    "code_execute": {
        "name": "code_execute",
        "label": "Code Execution (Sandbox)",
        "description": (
            "Run Python in a Docker sandbox with pandas, openpyxl, pdfplumber, "
            "reportlab. Use for aggregations (FIFO P&L, top-N, group-by), file "
            "generation (Excel/CSV/PDF/DOCX), multi-row computation. Upstream "
            "node output is auto-mounted as /workspace/input/data.json."
        ),
        "icon": "💻",
        "default_enabled": True,
    },
    "sql_query": {
        "name": "sql_query",
        "label": "SQL Query",
        "description": "Run a read-only SQL query against the configured database connection",
        "icon": "🗄️",
    },
    "nosql_query": {
        "name": "nosql_query",
        "label": "NoSQL Query",
        "description": "Query a MongoDB/DocumentDB collection via the configured connection",
        "icon": "🗃️",
    },
    "http_request": {
        "name": "http_request",
        "label": "HTTP Request",
        "description": "Make HTTP requests to external APIs",
        "icon": "🌐",
    },
    "bucket_read": {
        "name": "bucket_read",
        "label": "Bucket Read",
        "description": "List or download files from an S3-compatible / MinIO bucket (read-only)",
        "icon": "☁️",
    },
    "sftp_read": {
        "name": "sftp_read",
        "label": "SFTP Read",
        "description": "List or download files from an SFTP/FTP server (read-only)",
        "icon": "📁",
    },
}


async def _tool_web_search(query: str, max_results: int = 5) -> str:
    """Search the web. Default provider is DuckDuckGo HTML scraping (no key
    needed). When ``WORKFLOW_AGENT_SEARCH_PROVIDER=chat`` is set, routes
    through chat's ``execute_internet_search`` instead — uses whichever
    provider chat is configured for (grok / openai / perplexity / google
    via SEARCH_PROVIDER env). Better quality, but requires a paid API key.
    """
    provider = (os.getenv("WORKFLOW_AGENT_SEARCH_PROVIDER") or "").lower().strip()
    if provider == "chat":
        try:
            from citra_internet_service import execute_internet_search
            # Run the sync chat function in a thread so we don't block.
            return await asyncio.to_thread(
                execute_internet_search,
                query,
                "",  # no extra context from the agent
            )
        except Exception as e:
            logger.warning(
                "Workflow agent web_search via chat provider failed (%s); "
                "falling back to DuckDuckGo.", e,
            )

    import httpx
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_WEB_SEARCH) as client:
            resp = await client.get(
                "https://html.duckduckgo.com/html/",
                params={"q": query},
                headers={"User-Agent": "CitraAI-Agent/1.0"},
            )
            resp.raise_for_status()
            # Extract result snippets from HTML
            text = resp.text
            results = []
            # Simple extraction of result snippets
            snippets = re.findall(r'class="result__snippet">(.*?)</a>', text, re.DOTALL)
            for snippet in snippets[:max_results]:
                clean = re.sub(r'<[^>]+>', '', snippet).strip()
                if clean:
                    results.append(clean)
            return json.dumps(results) if results else "No results found."
    except Exception as e:
        return f"Search error: {str(e)}"


async def _tool_code_execute(
    script: str,
    *,
    output_filename: str = "output.json",
    session_id: str = "",
    input_data: Any = None,
) -> str:
    """Execute Python in the chat-grade Docker sandbox.

    Routes through ``services.code_executor.execute_code`` — same warm
    container pool quick chat uses, with pandas, openpyxl, xlrd,
    python-docx, python-pptx, Pillow, xlsxwriter, reportlab, pdfplumber,
    jsonschema available.

    The upstream node's ``input_data`` is serialised to
    ``/workspace/input/data.json`` so scripts can do ``pd.read_json(
    '/workspace/input/data.json', orient='records')`` and immediately
    operate on it. Stdout becomes the tool result; any files written to
    ``/workspace/output/`` are returned as ``output_files`` (download URLs).

    Returns a JSON string with ``stdout``, ``stderr``, ``output_files``
    (list of {filename, download_url}), and ``success``.
    """
    # 1. Materialise input_data as a virtual file the sandbox can read.
    #    Falls back to an empty list if no upstream input.
    files: List[Dict[str, str]] = []
    data_payload: Any
    if input_data is None:
        data_payload = []
    elif isinstance(input_data, dict) and isinstance(input_data.get("items"), list):
        data_payload = input_data["items"]  # universal envelope unwrap
    else:
        data_payload = input_data

    session_id = session_id or f"wf_agent_{uuid.uuid4().hex[:8]}"

    # Spool input_data → JSON bytes → S3 (so the sandbox's existing
    # download path picks it up uniformly with chat-uploaded files).
    # The chat sandbox image always reads from /workspace/input/<filename>.
    if data_payload not in (None, [], {}):
        try:
            from bucket import upload_file as _bucket_upload
            payload_bytes = json.dumps(data_payload, default=str).encode("utf-8")
            s3_key = f"workflow_agent/{session_id}/data.json"
            _bucket_upload(payload_bytes, s3_key, content_type="application/json")
            files = [{"filename": "data.json", "s3_key": s3_key}]
        except Exception as _stage_err:
            logger.warning(
                "Workflow agent: failed to stage input_data to S3 (%s); "
                "the LLM script will see no /workspace/input/data.json.",
                _stage_err,
            )
            files = []

    # 2. Run the chat sandbox.
    try:
        from services.code_executor import execute_code as _chat_execute_code
    except Exception as imp_err:
        return json.dumps({
            "success": False,
            "stdout": "",
            "stderr": f"Sandbox unavailable: {imp_err}",
            "output_files": [],
        })

    try:
        result = await _chat_execute_code(
            script=script,
            session_id=session_id,
            files=files,
            output_filename=output_filename or "output.json",
        )
    except Exception as exec_err:
        return json.dumps({
            "success": False,
            "stdout": "",
            "stderr": f"Sandbox execution error: {exec_err}",
            "output_files": [],
        })

    # 3. Compact result for the LLM.
    return json.dumps({
        "success": bool(result.get("success")),
        "stdout": result.get("stdout", "")[:20_000],
        "stderr": result.get("stderr", "")[:4_000],
        "output_files": result.get("output_files", []),
    })


# ---------------------------------------------------------------------------
# SQL read-only guard (shared with sources.py)
# ---------------------------------------------------------------------------
DESTRUCTIVE_SQL_KW = frozenset({
    "DROP", "DELETE", "TRUNCATE", "ALTER", "UPDATE", "INSERT",
    "CREATE", "GRANT", "REVOKE", "EXEC", "EXECUTE", "MERGE",
})


def _validate_readonly_sql(sql: str) -> None:
    """Raise ValueError if *sql* is anything other than a SELECT / WITH query."""
    stripped = sql.strip()
    # Strip SQL comments that could mask destructive keywords
    no_comments = re.sub(r'/\*.*?\*/', ' ', stripped, flags=re.DOTALL)
    no_comments = re.sub(r'--[^\n]*', ' ', no_comments)
    upper = no_comments.strip().upper()

    first_word = upper.split()[0] if upper.split() else ""
    if first_word not in ("SELECT", "WITH"):
        raise ValueError("Only SELECT queries are allowed")

    # Block semicolons to prevent multi-statement attacks
    # (e.g. "SELECT 1; DROP TABLE x")
    if ";" in no_comments:
        raise ValueError("Semicolons are not allowed — only single SELECT statements")

    for kw in DESTRUCTIVE_SQL_KW:
        if re.search(rf'\b{kw}\b', upper):
            raise ValueError(f"Destructive SQL keyword '{kw}' is not allowed")


async def _tool_sql_query(
    sql: str,
    connection_id: str,
    user_id: str,
    environment: str,
    limit: int = DEFAULT_AGENT_SQL_ROWS,
    *,
    resolved_connection: Optional[Dict[str, Any]] = None,
) -> str:
    """Execute a read-only SQL query against the customer's own database."""
    _validate_readonly_sql(sql)
    try:
        if resolved_connection:
            resolved = resolved_connection
        else:
            # Connections are pre-resolved once by the agent runner (with
            # the workflow's org/SA scope) and passed in — a tool never
            # resolves them itself. See C4.
            raise ValueError(
                "internal error: agent tool connection was not pre-resolved"
            )
        conn_str = resolved.get("connection_string", "")
        if not conn_str:
            return "Error: SQL connection has no connection_string configured."

        import sqlalchemy
        from sqlalchemy import text

        def _run():
            engine = sqlalchemy.create_engine(conn_str, pool_pre_ping=True)
            with engine.connect() as conn:
                result = conn.execute(text(sql))
                cols = list(result.keys())
                rows = [dict(zip(cols, row)) for row in result.fetchmany(limit)]
            engine.dispose()
            return rows

        rows = await asyncio.get_event_loop().run_in_executor(None, _run)
        return json.dumps(rows, default=str)
    except ValueError:
        raise                       # re-raise validation errors
    except Exception as e:
        return f"SQL error: {str(e)}"


async def _tool_nosql_query(
    collection: str,
    filter_doc: dict,
    connection_id: str,
    user_id: str,
    environment: str,
    limit: int = DEFAULT_AGENT_NOSQL_LIMIT,
    *,
    resolved_connection: Optional[Dict[str, Any]] = None,
) -> str:
    """Run a read-only find() on the customer's MongoDB/DocumentDB collection."""
    try:
        from motor.motor_asyncio import AsyncIOMotorClient

        if resolved_connection:
            resolved = resolved_connection
        else:
            # Connections are pre-resolved once by the agent runner (with
            # the workflow's org/SA scope) and passed in — a tool never
            # resolves them itself. See C4.
            raise ValueError(
                "internal error: agent tool connection was not pre-resolved"
            )
        conn_str = resolved.get("connection_string", "")
        db_name = resolved.get("database", "")
        if not conn_str or not db_name:
            return "Error: NoSQL connection is missing connection_string or database."

        client = AsyncIOMotorClient(conn_str, serverSelectionTimeoutMS=MONGO_CONNECT_TIMEOUT_MS)
        try:
            db = client[db_name]
            cursor = db[collection].find(filter_doc).limit(limit)
            docs = []
            async for doc in cursor:
                doc["_id"] = str(doc["_id"])
                docs.append(doc)
        finally:
            client.close()
        return json.dumps(docs, default=str)
    except Exception as e:
        return f"NoSQL error: {str(e)}"


async def _tool_http_request(url: str, method: str = "GET",
                              headers: dict = None, body: str = None) -> str:
    """Make HTTP request to external API with SSRF protection."""
    import httpx
    from ..utils.ssrf import assert_url_is_public

    # SSRF guard — shared validator (resolves the host; blocks private /
    # loopback / link-local / metadata / reserved / multicast addresses).
    try:
        assert_url_is_public(url)
    except ValueError as ssrf_err:
        return f"Error: {ssrf_err}"

    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_TOOL) as client:
            resp = await client.request(
                method=method.upper(),
                url=url,
                headers=headers or {},
                content=body,
            )
            body_text = resp.text
            return json.dumps({
                "status": resp.status_code,
                "body": body_text,
            })
    except Exception as e:
        return f"HTTP error: {str(e)}"


# ---------------------------------------------------------------------------
# Read-only enforcement for agent LLM tool calls
# ---------------------------------------------------------------------------
# The AI Agent workflow node is contractually READ-ONLY: the user attaches
# tools so the LLM can *read* enterprise data, never mutate it. If the model
# attempts a write / mutating tool call we BLOCK it, audit the attempt to a
# dedicated log file, and raise WriteBlockedError. Per RULE #1 (fail loud) the
# exception propagates out to fail the node — it is NOT fed back to the LLM as
# a tool result and NOT silently swallowed.


class WriteBlockedError(Exception):
    """Raised when the agent LLM attempts a write / mutating tool call.

    Propagates out of the agent loop to fail the node. Never caught by the
    per-tool error handlers (which would otherwise convert it to a string and
    feed it back to the model).

    ``non_retryable`` tells the node ``run()`` wrapper not to auto-retry: a
    blocked write is deterministic, so retrying would only re-run the LLM loop,
    re-attempt the same write, and duplicate the audit record.
    """

    non_retryable = True

    def __init__(self, fn_name: str, reason: str, fn_args: Optional[Dict[str, Any]] = None):
        self.fn_name = fn_name
        self.reason = reason
        self.fn_args = fn_args or {}
        super().__init__(f"Write attempt blocked for tool '{fn_name}': {reason}")


# HTTP methods that cannot mutate the target system.
_HTTP_READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

# Built-in tools that enforce their own read-only guard internally
# (sql_query → SELECT-only, nosql_query → find()-only, bucket/sftp → list +
# download only). Always read-safe; no extra check needed here.
_READ_ONLY_BUILTIN_TOOLS = frozenset({
    "web_search", "sql_query", "nosql_query", "bucket_read", "sftp_read",
})

# Verbs that signal a mutating / write action.
_WRITE_VERBS = (
    "create|insert|update|upsert|delete|remove|drop|truncate|write|save|"
    "modify|put|post|patch|execute|exec|rpc|provision|deploy|revoke|grant|"
    "assign|approve|reject|cancel|submit|trigger|run_action|execute_action"
)

# Anywhere in the name (word-boundary anchored, so "customer_updates" does NOT
# match "update"). Used for the catch-all on fully-unknown tools.
_WRITE_VERB_RE = re.compile(
    rf"(?:^|[_\-\s])(?:{_WRITE_VERBS})(?:$|[_\-\s])", re.IGNORECASE
)

# At the START of the name only. Used for dept-MCP source names: a write
# *action* source is named after its verb (create_ticket, execute_action),
# whereas a read source named "asset_data" / "customer_updates" must not trip.
# (The MCP /query path is hard read-only by validation + GET-only + DB
# privilege, so this is forward-looking defense-in-depth, tuned to not
# false-positive on legitimate read sources.)
_WRITE_VERB_PREFIX_RE = re.compile(
    rf"^(?:{_WRITE_VERBS})(?:$|[_\-\s])", re.IGNORECASE
)

# dept-MCP verb metadata values that are read-safe (when the discovery service
# advertises a verb / action_type at all — today it does not).


def _assert_tool_is_read_only(
    fn_name: str,
    fn_args: Dict[str, Any],
    tool_def: Optional[Dict[str, Any]] = None,
) -> None:
    """Block any write / mutating agent tool call by raising WriteBlockedError.

    Detection by surface:
      * http_request  — only GET/HEAD/OPTIONS allowed (POST/PUT/PATCH/DELETE
                         can mutate the target → blocked).
      * dept_* tools  — blocked if the discovery metadata declares a write
                         endpoint / verb, or the tool name matches a write
                         verb. (Dispatch only ever hits the read query_endpoint
                         today, so a write-verb dept tool should never reach
                         here — if it does, fail loud.)
      * read-only built-ins and code_execute — pass through (the former guard
                         themselves; code_execute runs in a network-governed
                         sandbox and cannot be statically proven read-only).
      * any other / unknown tool — blocked if its name matches a write verb.
    """
    if fn_name == "http_request":
        method = str(fn_args.get("method", "GET")).strip().upper()
        if method not in _HTTP_READ_METHODS:
            raise WriteBlockedError(
                fn_name,
                f"HTTP method '{method}' can mutate the target system; only "
                f"{sorted(_HTTP_READ_METHODS)} are permitted on a read-only agent node.",
                fn_args,
            )
        return

    if fn_name in _READ_ONLY_BUILTIN_TOOLS or fn_name == "code_execute":
        return

    # The dept-MCP and IT-pinned `mcp_tools` branches were REMOVED 2026-08-08
    # (PORTING.md §6b). Both decided read-vs-write from Citra discovery-service
    # metadata (`execute_endpoint` / `verb` / `query_endpoint`) that does not
    # exist outside the Citra platform. Kept as-is they would have evaluated
    # against absent fields and passed everything -- a guard that still runs
    # and no longer guards.
    #
    # Anything that used to match those branches now falls through to the
    # name-based backstop below. That backstop is NOT strictly stronger: it
    # reads the tool NAME, so a write tool with an innocuous name would have
    # slipped past it. It is safe here only because `tool_def` is now always
    # None -- `mcp_name_map` is assigned {} and never written, so no caller can
    # supply the dept metadata those branches keyed on. If anything ever
    # repopulates `tool_def`, this guard must be revisited, not trusted.
    #
    # Generic MCP calls belong on the MCP Server node (nodes/mcp.py), which
    # enforces its own explicit `allow_writes` opt-in plus the protocol's
    # readOnlyHint / destructiveHint annotations.

    # Unknown tool name — be safe: if it looks like a write, block it.
    if _WRITE_VERB_RE.search(fn_name):
        raise WriteBlockedError(
            fn_name, "tool name matches a write-action pattern.", fn_args
        )


_write_block_logger: Optional[logging.Logger] = None
_write_block_logger_lock = threading.Lock()


def _get_write_block_logger() -> logging.Logger:
    """Lazily build the dedicated write-block audit logger (own file handler).

    Path: WF_WRITE_BLOCK_LOG_FILE, else <WF_LOG_DIR or ./logs>/agent_write_blocks.log.
    Failures to open the file propagate (RULE #1) — we never silently drop an
    audit record.
    """
    global _write_block_logger
    if _write_block_logger is not None:
        return _write_block_logger
    with _write_block_logger_lock:
        if _write_block_logger is not None:
            return _write_block_logger
        lg = logging.getLogger("citra_workflow.agent_write_block")
        lg.setLevel(logging.WARNING)
        lg.propagate = False
        log_path = (os.getenv("WF_WRITE_BLOCK_LOG_FILE", "").strip()
                    or os.path.join(
                        os.getenv("WF_LOG_DIR", "").strip() or os.path.join(os.getcwd(), "logs"),
                        "agent_write_blocks.log",
                    ))
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        handler = RotatingFileHandler(
            log_path, maxBytes=5_000_000, backupCount=3, encoding="utf-8"
        )
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        lg.addHandler(handler)
        _write_block_logger = lg
        return lg


def _audit_write_block(
    err: "WriteBlockedError",
    *,
    user_id: str,
    org_id: str,
    workflow_id: str,
    node_id: str,
    execution_id: str,
    iteration: int,
) -> None:
    """Write one structured audit record for a blocked write attempt, to both
    the dedicated write-block log file and the normal app log."""
    record = {
        "event": "agent_write_blocked",
        "tool": err.fn_name,
        "reason": err.reason,
        "args": err.fn_args,
        "user_id": user_id,
        "org_id": org_id,
        "workflow_id": workflow_id,
        "node_id": node_id,
        "execution_id": execution_id,
        "iteration": iteration,
    }
    payload = json.dumps(record, default=str)
    _get_write_block_logger().error(payload)
    logger.error("🚫 [AGENT_WRITE_BLOCKED] %s", payload)


# Map tool names to functions
TOOL_EXECUTORS = {
    "web_search": _tool_web_search,
    "code_execute": _tool_code_execute,
    "sql_query": _tool_sql_query,
    "nosql_query": _tool_nosql_query,
    "http_request": _tool_http_request,
    "bucket_read": None,      # dispatched inline (needs connection params)
    "sftp_read": None,    # dispatched inline (needs connection params)
}


# ---------------------------------------------------------------------------
# Bucket read-only guard
# ---------------------------------------------------------------------------
_BUCKET_ALLOWED_ACTIONS = frozenset({"list", "download"})


async def _tool_bucket_read(
    action: str,
    connection_id: str,
    user_id: str,
    environment: str,
    prefix: str = "",
    key: str = "",
    max_keys: int = DEFAULT_AGENT_BUCKET_LIST_LIMIT,
    *,
    resolved_connection: Optional[Dict[str, Any]] = None,
) -> str:
    """List or download objects from an S3-compatible / MinIO bucket (read-only)."""
    action = action.lower().strip()
    if action not in _BUCKET_ALLOWED_ACTIONS:
        return f"Error: Only 'list' and 'download' actions are allowed (got '{action}')."

    if resolved_connection:
        resolved = resolved_connection
    else:
        # Connections are pre-resolved once by the agent runner (with the
        # workflow's org/SA scope) and passed in — a tool never resolves
        # them itself. See C4.
        raise ValueError(
            "internal error: agent tool connection was not pre-resolved"
        )
    bucket = resolved.get("bucket", "")
    if not bucket:
        return "Error: Bucket connection has no bucket configured."

    import boto3

    boto_kwargs: dict = {
        "aws_access_key_id": resolved.get("access_key_id") or None,
        "aws_secret_access_key": resolved.get("secret_access_key") or None,
        "region_name": resolved.get("region") or None,
    }
    endpoint_url = resolved.get("endpoint_url")
    if endpoint_url:
        boto_kwargs["endpoint_url"] = endpoint_url

    try:
        s3 = await asyncio.get_event_loop().run_in_executor(
            None, lambda: boto3.client("s3", **boto_kwargs)
        )

        if action == "list":
            def _list():
                resp = s3.list_objects_v2(
                    Bucket=bucket, Prefix=prefix, MaxKeys=min(max_keys, DEFAULT_AGENT_BUCKET_LIST_LIMIT)
                )
                objects = []
                for obj in resp.get("Contents", []):
                    objects.append({
                        "key": obj["Key"],
                        "size": obj["Size"],
                        "last_modified": str(obj["LastModified"]),
                    })
                return objects
            items = await asyncio.get_event_loop().run_in_executor(None, _list)
            return json.dumps(items, default=str)

        else:  # download
            if not key:
                return "Error: 'key' is required for download action."
            from . import sanitize_remote_path
            key = sanitize_remote_path(key)

            def _download():
                resp = s3.head_object(Bucket=bucket, Key=key)
                size = resp["ContentLength"]
                if size > MAX_AGENT_FILE_READ_SIZE:
                    raise ValueError(
                        f"File size ({size} bytes) exceeds agent read limit "
                        f"({MAX_AGENT_FILE_READ_SIZE} bytes). "
                        f"Increase WF_MAX_AGENT_FILE_READ_SIZE to allow larger files."
                    )
                body = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
                return body

            raw = await asyncio.get_event_loop().run_in_executor(None, _download)
            # Try to decode as text; fall back to base64
            try:
                text = raw.decode("utf-8")
                return json.dumps({"key": key, "content": text, "encoding": "utf-8"})
            except UnicodeDecodeError:
                import base64
                return json.dumps({"key": key, "content": base64.b64encode(raw).decode(), "encoding": "base64"})

    except ValueError:
        raise
    except Exception as e:
        return f"Bucket error: {str(e)}"


# ---------------------------------------------------------------------------
# SFTP read-only guard
# ---------------------------------------------------------------------------
_SFTP_ALLOWED_ACTIONS = frozenset({"list", "download"})


async def _tool_sftp_read(
    action: str,
    connection_id: str,
    user_id: str,
    environment: str,
    remote_path: str = "/",
    max_entries: int = DEFAULT_AGENT_SFTP_LIST_LIMIT,
    *,
    resolved_connection: Optional[Dict[str, Any]] = None,
) -> str:
    """List directory or download a file from an SFTP/FTP server (read-only)."""
    action = action.lower().strip()
    if action not in _SFTP_ALLOWED_ACTIONS:
        return f"Error: Only 'list' and 'download' actions are allowed (got '{action}')."

    from . import sanitize_remote_path
    remote_path = sanitize_remote_path(remote_path)

    if resolved_connection:
        resolved = resolved_connection
    else:
        # Connections are pre-resolved once by the agent runner (with the
        # workflow's org/SA scope) and passed in — a tool never resolves
        # them itself. See C4.
        raise ValueError(
            "internal error: agent tool connection was not pre-resolved"
        )
    host = resolved.get("host", "")
    if not host:
        return "Error: SFTP connection has no host configured."

    port = int(resolved.get("port", 22))
    username = resolved.get("username", "")
    password = resolved.get("password", "")
    private_key = resolved.get("private_key", "")
    protocol = resolved.get("protocol", "sftp")

    try:
        if protocol == "sftp":
            return await _sftp_action(action, host, port, username, password, private_key, remote_path, max_entries)
        else:
            return await _ftp_action(action, host, port, username, password, remote_path, max_entries, use_tls=(protocol == "ftps"))
    except ValueError:
        raise
    except Exception as e:
        return f"SFTP error: {str(e)}"


async def _sftp_action(action, host, port, username, password, private_key, remote_path, max_entries):
    import io

    def _do():
        import paramiko
        transport = paramiko.Transport((host, port))
        try:
            if private_key:
                key_file = io.StringIO(private_key)
                pkey = paramiko.RSAKey.from_private_key(key_file)
                transport.connect(username=username, pkey=pkey)
            else:
                transport.connect(username=username, password=password)
            sftp = paramiko.SFTPClient.from_transport(transport)
            try:
                if action == "list":
                    entries = []
                    for attr in sftp.listdir_attr(remote_path)[:max_entries]:
                        entries.append({
                            "name": attr.filename,
                            "size": attr.st_size,
                            "is_dir": bool(attr.st_mode and (attr.st_mode & 0o40000)),
                        })
                    return json.dumps(entries, default=str)
                else:  # download
                    stat = sftp.stat(remote_path)
                    if stat.st_size and stat.st_size > MAX_AGENT_FILE_READ_SIZE:
                        raise ValueError(
                            f"File size ({stat.st_size} bytes) exceeds agent read limit "
                            f"({MAX_AGENT_FILE_READ_SIZE} bytes). "
                            f"Increase WF_MAX_AGENT_FILE_READ_SIZE to allow larger files."
                        )
                    buf = io.BytesIO()
                    sftp.getfo(remote_path, buf)
                    return buf.getvalue()
            finally:
                sftp.close()
        finally:
            transport.close()

    result = await asyncio.get_event_loop().run_in_executor(None, _do)
    if action == "list":
        return result  # already JSON string
    # download: try text, fallback to base64
    try:
        text = result.decode("utf-8")
        return json.dumps({"path": remote_path, "content": text, "encoding": "utf-8"})
    except UnicodeDecodeError:
        import base64
        return json.dumps({"path": remote_path, "content": base64.b64encode(result).decode(), "encoding": "base64"})


async def _ftp_action(action, host, port, username, password, remote_path, max_entries, use_tls=False):
    import io

    def _do():
        if use_tls:
            from ftplib import FTP_TLS
            ftp = FTP_TLS()
        else:
            from ftplib import FTP
            ftp = FTP()
        ftp.connect(host, port, timeout=FTP_CONNECT_TIMEOUT)
        ftp.login(username or "anonymous", password or "")
        if use_tls:
            ftp.prot_p()
        try:
            if action == "list":
                entries = []
                for name in ftp.nlst(remote_path)[:max_entries]:
                    entries.append({"name": name})
                return json.dumps(entries, default=str)
            else:  # download
                buf = io.BytesIO()
                ftp.retrbinary(f"RETR {remote_path}", buf.write)
                return buf.getvalue()
        finally:
            ftp.quit()

    result = await asyncio.get_event_loop().run_in_executor(None, _do)
    if action == "list":
        return result
    if len(result) > MAX_AGENT_FILE_READ_SIZE:
        raise ValueError(
            f"File size ({len(result)} bytes) exceeds agent read limit "
            f"({MAX_AGENT_FILE_READ_SIZE} bytes)."
        )
    try:
        text = result.decode("utf-8")
        return json.dumps({"path": remote_path, "content": text, "encoding": "utf-8"})
    except UnicodeDecodeError:
        import base64
        return json.dumps({"path": remote_path, "content": base64.b64encode(result).decode(), "encoding": "base64"})


def _build_tool_definitions(tool_names: List[str], tool_schemas: Optional[Dict[str, Any]] = None) -> List[Dict]:
    """Build OpenAI-compatible function definitions for selected tools.

    When *tool_schemas* is provided, tool descriptions are enriched with
    schema summaries (e.g. table names for SQL, collection names for NoSQL).
    """
    tool_defs = []
    _schemas = tool_schemas or {}

    def _enrich_desc(base: str, tool_name: str) -> str:
        """Append a short schema summary to the base description."""
        schema = _schemas.get(tool_name)
        if not schema or "error" in (schema or {}):
            return base
        if tool_name == "sql_query":
            tables = schema.get("tables", [])
            if tables:
                names = ", ".join(t["name"] for t in tables[:15])
                suffix = f" Available tables: {names}"
                if len(tables) > 15:
                    suffix += f" (and {len(tables)-15} more)"
                return base + suffix
        elif tool_name == "nosql_query":
            colls = schema.get("collections", [])
            if colls:
                names = ", ".join(c["name"] for c in colls[:15])
                return base + f" Available collections: {names}"
        elif tool_name == "bucket_read":
            bucket = schema.get("bucket", "")
            if bucket:
                return base + f" Bucket: {bucket}"
        elif tool_name == "sftp_read":
            host = schema.get("host", "")
            if host:
                return base + f" Server: {host}"
        return base

    if "web_search" in tool_names:
        tool_defs.append({
            "type": "function",
            "function": {
                "name": "web_search",
                "description": "Search the web for real-time information",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Search query"},
                        "max_results": {"type": "integer", "description": "Max results (default 5)", "default": 5},
                    },
                    "required": ["query"],
                },
            },
        })

    if "code_execute" in tool_names:
        tool_defs.append({
            "type": "function",
            "function": {
                "name": "code_execute",
                "description": (
                    "Execute a Python script in a Docker sandbox (same image quick chat uses). "
                    "Available libraries: pandas, openpyxl, xlrd, python-docx, python-pptx, "
                    "Pillow, xlsxwriter, reportlab, pdfplumber, jsonschema. The upstream "
                    "node's output is mounted as /workspace/input/data.json — read it with "
                    "`pd.read_json('/workspace/input/data.json', orient='records')`. Use this "
                    "tool for ANY multi-row aggregation (totals, top-N, group-by, FIFO/LIFO "
                    "lot matching, P&L, joins, time series), file generation (Excel/CSV/PDF/"
                    "DOCX/PPTX written to /workspace/output/), or computation that exceeds "
                    "what the LLM can do mentally. NEVER eyeball aggregates from sampled rows."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "script": {
                            "type": "string",
                            "description": (
                                "Python script. Read input with "
                                "`pd.read_json('/workspace/input/data.json', orient='records')`. "
                                "Print results to stdout (they become the tool result). Write "
                                "files to /workspace/output/<output_filename> if you want them "
                                "downloadable downstream."
                            ),
                        },
                        "output_filename": {
                            "type": "string",
                            "description": (
                                "Filename for any /workspace/output/ artifact (e.g. "
                                "'pnl.xlsx', 'report.pdf'). Pass 'output.json' if you "
                                "don't write a file (only print to stdout)."
                            ),
                            "default": "output.json",
                        },
                    },
                    "required": ["script"],
                },
            },
        })

    if "sql_query" in tool_names:
        tool_defs.append({
            "type": "function",
            "function": {
                "name": "sql_query",
                "description": _enrich_desc("Execute a read-only SQL SELECT query against the configured database.", "sql_query"),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "sql": {"type": "string", "description": "SQL SELECT query to execute"},
                        "limit": {"type": "integer", "description": "Max rows to return", "default": 50},
                    },
                    "required": ["sql"],
                },
            },
        })

    if "nosql_query" in tool_names:
        tool_defs.append({
            "type": "function",
            "function": {
                "name": "nosql_query",
                "description": _enrich_desc("Query a MongoDB/DocumentDB collection with a find filter.", "nosql_query"),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "collection": {"type": "string", "description": "Collection name"},
                        "filter": {"type": "object", "description": "MongoDB query filter", "default": {}},
                        "limit": {"type": "integer", "description": "Max documents to return", "default": 20},
                    },
                    "required": ["collection"],
                },
            },
        })

    if "http_request" in tool_names:
        tool_defs.append({
            "type": "function",
            "function": {
                "name": "http_request",
                "description": _enrich_desc(
                    "Make a read-only HTTP request to an external API. Only GET/HEAD/OPTIONS "
                    "are permitted — write methods (POST/PUT/PATCH/DELETE) are blocked.",
                    "http_request",
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "url": {"type": "string", "description": "The URL to request"},
                        "method": {"type": "string", "enum": ["GET", "HEAD", "OPTIONS"], "default": "GET"},
                        "headers": {"type": "object", "description": "Request headers"},
                    },
                    "required": ["url"],
                },
            },
        })

    if "bucket_read" in tool_names:
        tool_defs.append({
            "type": "function",
            "function": {
                "name": "bucket_read",
                "description": _enrich_desc("List objects or download a file from an S3-compatible / MinIO bucket. Read-only — no uploads or deletions.", "bucket_read"),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "enum": ["list", "download"], "description": "'list' to list objects, 'download' to fetch a file"},
                        "prefix": {"type": "string", "description": "Key prefix to filter when listing (optional)", "default": ""},
                        "key": {"type": "string", "description": "Exact object key to download (required for download)"},
                        "max_keys": {"type": "integer", "description": "Max objects to return when listing", "default": 100},
                    },
                    "required": ["action"],
                },
            },
        })

    if "sftp_read" in tool_names:
        tool_defs.append({
            "type": "function",
            "function": {
                "name": "sftp_read",
                "description": _enrich_desc("List a directory or download a file from an SFTP/FTP server. Read-only — no uploads or deletions.", "sftp_read"),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "enum": ["list", "download"], "description": "'list' to list directory, 'download' to fetch a file"},
                        "remote_path": {"type": "string", "description": "Directory path (for list) or file path (for download)"},
                        "max_entries": {"type": "integer", "description": "Max entries to return when listing", "default": 100},
                    },
                    "required": ["action", "remote_path"],
                },
            },
        })

    return tool_defs


async def _run_agent_with_tools(
    system_prompt: str,
    user_message: str,
    tier: str,
    tool_names: List[str],
    user_id: str,
    environment: str = "test",
    org_id: str = "",
    owner_id: str = "",
    owner_type: str = "",
    sql_connection_id: str = "",
    nosql_connection_id: str = "",
    bucket_connection_id: str = "",
    sftp_connection_id: str = "",
    output_schema: Optional[Dict] = None,
    max_iterations: int = 10,
    workflow_context: Optional[Dict] = None,
    input_data: Any = None,
    jwt_token: Optional[str] = None,
    workflow_id: str = "",
    node_id: str = "",
    execution_id: str = "",
) -> Dict[str, Any]:
    """Run an LLM agent loop with tool calling support.

    The ``tier`` argument selects which configured LLM tier (small / medium
    / large) the agent runs on. The actual model name, base URL, API key,
    and provider-specific ``extra_body`` are all resolved per tier from
    environment via llm_client.
    """
    from citra_llm import get_llm_client, get_llm_model, get_llm_extra_body

    # Local alias used by the code_execute dispatch to mount upstream node
    # output as /workspace/input/data.json in the Docker sandbox.
    _agent_input_data = input_data

    tier = _coerce_tier(tier)
    client = get_llm_client(async_=False, tier=tier)
    model = get_llm_model(tier=tier)
    tier_extra_body = get_llm_extra_body(model, tier=tier)

    # ------------------------------------------------------------------
    # Schema discovery + tool skill enrichment (once per execution)
    # ------------------------------------------------------------------
    tool_schemas: Dict[str, Any] = {}
    _tool_conn_map = {
        "sql_query": sql_connection_id,
        "nosql_query": nosql_connection_id,
        "bucket_read": bucket_connection_id,
        "sftp_read": sftp_connection_id,
        "http_request": "",  # API connections not passed yet; skip discovery
    }
    try:
        from ..schema_cache import get_or_discover_schema
        from ..tool_skills import build_tool_skills_section

        for tn in (tool_names or []):
            conn_id = _tool_conn_map.get(tn, "")
            if conn_id:
                schema = await get_or_discover_schema(
                    tn, conn_id, org_id, owner_id, environment,
                    owner_type=owner_type,
                    workflow_context=workflow_context,
                )
                if schema:
                    tool_schemas[tn] = schema
            else:
                # Tools without connections still get a skill block (no schema)
                tool_schemas[tn] = None

        skills_section = build_tool_skills_section(tool_schemas, workflow_context=workflow_context)
        if skills_section:
            system_prompt = (system_prompt or "") + skills_section
    except Exception as e:
        logger.warning("Schema discovery / skill enrichment failed (non-fatal): %s", e)

    # ------------------------------------------------------------------
    # Pre-resolve connections once (avoids repeated MongoDB + decryption
    # per tool call inside the LLM loop)
    # ------------------------------------------------------------------
    _resolved_connections: Dict[str, Dict[str, Any]] = {}
    _conn_id_map = {
        "sql_query": sql_connection_id,
        "nosql_query": nosql_connection_id,
        "bucket_read": bucket_connection_id,
        "sftp_read": sftp_connection_id,
    }
    try:
        from ..connection_resolver import resolve_connection

        for tool_name, conn_id in _conn_id_map.items():
            if conn_id and tool_name in (tool_names or []):
                resolved = await resolve_connection(
                    conn_id, org_id=org_id, owner_id=owner_id,
                    owner_type=owner_type, environment=environment,
                )
                _resolved_connections[tool_name] = resolved
    except Exception as e:
        # Connection resolution failed — raise immediately so the user
        # gets a clear error instead of burning LLM tokens on broken tools.
        raise ValueError(
            f"Failed to resolve connection before agent execution: {e}"
        ) from e

    # Validate that resolved connections have the required fields.
    _REQUIRED_FIELDS = {
        "sql_query": [("connection_string", "connection_string")],
        "nosql_query": [("connection_string", "connection_string"), ("database", "database")],
        "bucket_read": [("bucket", "bucket")],
        "sftp_read": [("host", "host")],
    }
    for tool_name, resolved in _resolved_connections.items():
        for field_key, field_label in _REQUIRED_FIELDS.get(tool_name, []):
            if not resolved.get(field_key):
                conn_id = _conn_id_map[tool_name]
                raise ValueError(
                    f"Connection '{conn_id}' is missing required field "
                    f"'{field_label}' for tool '{tool_name}'. "
                    f"Check the connection configuration in the {environment} environment."
                )

    # Build tool definitions (with optional schema summaries for descriptions)
    tool_defs = _build_tool_definitions(tool_names, tool_schemas) if tool_names else []

    # Agent MCP tools REMOVED 2026-08-08 (PORTING.md §6b). They spoke the
    # Citra dept-MCP /query contract. Use the MCP Server node (nodes/mcp.py)
    # for any standards-compliant server instead.
    mcp_name_map: Dict[str, Dict[str, Any]] = {}

    # Migration guard: a stale `dept_*` entry in the `tools` picker (from a
    # workflow saved against the old Citra discovery-driven agent) is now a
    # no-op — warn loud rather than silently dropping the data source.
    _stale_dept = [t for t in (tool_names or []) if str(t).startswith("dept_")]
    if _stale_dept:
        logger.warning(
            "Workflow agent: ignoring legacy discovery-driven tool(s) %s — the "
            "agent no longer resolves them. Put an mcp_server or vector_search "
            "node upstream of this agent instead.",
            _stale_dept,
        )

    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": user_message})

    tool_calls_log = []

    for iteration in range(max_iterations):
        kwargs = {
            "model": model,
            "messages": messages,
            "temperature": 0.3,
            # Explicit reasoning-safe cap. Without max_tokens the provider
            # defaults to a SMALL completion limit, and a reasoning model
            # (GLM/DeepSeek) spends hidden reasoning tokens against it and
            # truncates mid-think. Large is capped at 32K (NOT the 128K tier
            # ceiling): the agent loop's message history GROWS every iteration
            # and strict providers reject prompt_tokens + max_tokens > context
            # — 32K covers reasoning + a full tool-call turn while staying
            # inside the window even on long multi-tool runs.
            "max_tokens": {"small": 8000, "medium": 16000, "large": 32000}.get(tier, 16000),
        }
        if tier_extra_body:
            kwargs["extra_body"] = tier_extra_body
        if tool_defs:
            kwargs["tools"] = tool_defs

        # If output schema is specified and this is likely the final call
        if output_schema and (not tool_defs or iteration > 0):
            schema_instruction = (
                f"\n\nYou MUST respond with valid JSON matching this schema: "
                f"{json.dumps(output_schema)}"
            )
            # Append to last user or system message
            if messages[-1]["role"] in ("user", "system"):
                messages[-1]["content"] += schema_instruction

        # Charge this agent iteration's LLM call against the run's per-run
        # budget (raises RunLlmBudgetExceeded when the cap is hit) — in the async
        # context, before the thread offload. One charge per loop iteration.
        from ..llm_budget import charge_llm_call
        charge_llm_call()
        resp = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: client.chat.completions.create(**kwargs)
        )

        choice = resp.choices[0]
        message = choice.message

        # If no tool calls, we're done
        if not message.tool_calls:
            final_text = message.content or ""

            # Try to parse structured output
            if output_schema:
                try:
                    cleaned = final_text.strip()
                    if cleaned.startswith("```"):
                        # Tolerate a fence with no newline after it (```{...}```)
                        # — splitting on the first newline would otherwise raise
                        # IndexError on a 1-element list.
                        nl = cleaned.find("\n")
                        cleaned = cleaned[nl + 1:] if nl != -1 else cleaned[3:]
                        cleaned = cleaned.rsplit("```", 1)[0].strip()
                    parsed = json.loads(cleaned)
                    return {
                        "result": parsed,
                        "raw_response": final_text,
                        "tool_calls": tool_calls_log,
                        "structured": True,
                    }
                except (json.JSONDecodeError, IndexError):
                    pass

            return {
                "result": final_text,
                "tool_calls": tool_calls_log,
                "structured": False,
            }

        # Process tool calls
        messages.append(message.model_dump())

        for tool_call in message.tool_calls:
            fn_name = tool_call.function.name
            try:
                fn_args = json.loads(tool_call.function.arguments)
            except json.JSONDecodeError:
                fn_args = {}

            tool_calls_log.append({
                "tool": fn_name,
                "args": fn_args,
                "iteration": iteration,
            })

            # Read-only enforcement (RULE #1: fail loud). Blocks any write /
            # mutating tool call BEFORE it runs. Placed ahead of the per-tool
            # try/except blocks below so WriteBlockedError is never caught and
            # converted into a tool result fed back to the model — it audits to
            # the write-block log file and propagates out to fail the node.
            try:
                _assert_tool_is_read_only(
                    fn_name, fn_args, tool_def=mcp_name_map.get(fn_name),
                )
            except WriteBlockedError as _wb:
                _audit_write_block(
                    _wb,
                    user_id=user_id,
                    org_id=org_id,
                    workflow_id=workflow_id,
                    node_id=node_id,
                    execution_id=execution_id,
                    iteration=iteration,
                )
                raise

            # The dept-MCP dispatch branch was removed 2026-08-08 (PORTING.md
            # §6b) along with the mcp_tools config that populated it.

            if fn_name not in TOOL_EXECUTORS:
                tool_result = f"Unknown tool: {fn_name}"
            else:
                try:
                    if fn_name == "web_search":
                        _coro = _tool_web_search(
                            fn_args.get("query", ""),
                            fn_args.get("max_results", 5),
                        )
                    elif fn_name == "code_execute":
                        # Chat-grade Docker sandbox: script + output_filename.
                        # Pipe upstream node output as /workspace/input/data.json
                        # so scripts can `pd.read_json('/workspace/input/data.json')`.
                        _coro = _tool_code_execute(
                            script=fn_args.get("script") or fn_args.get("code", ""),
                            output_filename=fn_args.get("output_filename", "output.json"),
                            session_id=f"wf_{user_id or 'anon'}_{uuid.uuid4().hex[:6]}",
                            input_data=_agent_input_data,
                        )
                    elif fn_name == "sql_query":
                        if not sql_connection_id:
                            _coro = None
                            tool_result = "Error: No SQL connection configured on this agent node."
                        else:
                            _coro = _tool_sql_query(
                                fn_args.get("sql", ""),
                                sql_connection_id,
                                user_id,
                                environment,
                                fn_args.get("limit", DEFAULT_AGENT_SQL_ROWS),
                                resolved_connection=_resolved_connections.get("sql_query"),
                            )
                    elif fn_name == "nosql_query":
                        if not nosql_connection_id:
                            _coro = None
                            tool_result = "Error: No NoSQL connection configured on this agent node."
                        else:
                            _coro = _tool_nosql_query(
                                fn_args.get("collection", ""),
                                fn_args.get("filter", {}),
                                nosql_connection_id,
                                user_id,
                                environment,
                                fn_args.get("limit", DEFAULT_AGENT_NOSQL_LIMIT),
                                resolved_connection=_resolved_connections.get("nosql_query"),
                            )
                    elif fn_name == "http_request":
                        _coro = _tool_http_request(
                            fn_args.get("url", ""),
                            fn_args.get("method", "GET"),
                            fn_args.get("headers"),
                            fn_args.get("body"),
                        )
                    elif fn_name == "bucket_read":
                        if not bucket_connection_id:
                            _coro = None
                            tool_result = "Error: No bucket connection configured on this agent node."
                        else:
                            _coro = _tool_bucket_read(
                                fn_args.get("action", "list"),
                                bucket_connection_id,
                                user_id,
                                environment,
                                prefix=fn_args.get("prefix", ""),
                                key=fn_args.get("key", ""),
                                max_keys=fn_args.get("max_keys", DEFAULT_AGENT_BUCKET_LIST_LIMIT),
                                resolved_connection=_resolved_connections.get("bucket_read"),
                            )
                    elif fn_name == "sftp_read":
                        if not sftp_connection_id:
                            _coro = None
                            tool_result = "Error: No SFTP connection configured on this agent node."
                        else:
                            _coro = _tool_sftp_read(
                                fn_args.get("action", "list"),
                                sftp_connection_id,
                                user_id,
                                environment,
                                remote_path=fn_args.get("remote_path", "/"),
                                max_entries=fn_args.get("max_entries", DEFAULT_AGENT_SFTP_LIST_LIMIT),
                                resolved_connection=_resolved_connections.get("sftp_read"),
                            )
                    else:
                        _coro = None
                        tool_result = f"Tool {fn_name} not implemented"

                    # Await with per-tool timeout
                    if _coro is not None:
                        try:
                            tool_result = await asyncio.wait_for(
                                _coro, timeout=AGENT_TOOL_CALL_TIMEOUT
                            )
                        except asyncio.TimeoutError:
                            tool_result = (
                                f"Error: Tool '{fn_name}' timed out after "
                                f"{AGENT_TOOL_CALL_TIMEOUT}s. Try a simpler "
                                f"query or increase WF_AGENT_TOOL_CALL_TIMEOUT."
                            )
                except Exception as e:
                    tool_result = f"Tool error: {str(e)}"

            messages.append({
                "role": "tool",
                "tool_call_id": tool_call.id,
                "content": str(tool_result),
            })

    # Exceeded max iterations
    return {
        "result": "Agent reached maximum iteration limit.",
        "tool_calls": tool_calls_log,
        "structured": False,
        "warning": "max_iterations_reached",
    }


# ============================================================================
# AI Agent Node
# ============================================================================

@register_node
class AIAgentNode(BaseNode):
    node_type = NodeType.AI_AGENT
    category = NodeCategory.AGENT
    label = "AI Agent"
    description = "Autonomous AI agent with tool calling, structured output, and multi-step reasoning"
    icon = "🤖"
    color = "#7c3aed"
    ai_authoring_hint = (
        "This agent does NOT call MCP servers or vector stores itself. To give it "
        "external data, put a mcp_server, vector_search or reranker node UPSTREAM "
        "of it and let its output flow in — then write the agent's "
        "system_prompt/user_prompt to use that data. Only the built-in tools "
        "listed in 'tools' are callable from inside the agent loop."
    )

    @classmethod
    def get_fields(cls) -> List[NodeFieldSchema]:
        return [
            NodeFieldSchema(
                name="agent_name", label="Agent Name", type="text",
                placeholder="Research Agent",
                help_text="A descriptive name for this agent",
            ),
            NodeFieldSchema(
                name="system_prompt", label="System Instructions", type="textarea",
                required=True,
                placeholder="You are a research assistant. Analyze the provided data and produce a detailed report with findings, risks, and recommendations.",
                help_text="Define the agent's role, behavior, and goals",
            ),
            NodeFieldSchema(
                name="user_prompt", label="Task / Input Prompt", type="textarea",
                required=True,
                placeholder="Analyze this data: {{data}}\n\nProvide your analysis.",
                help_text="Use {{data}} for input data, {{variable_name}} for workflow variables",
            ),
            _tier_field(),
            NodeFieldSchema(
                name="tools", label="Agent Tools", type="tool_picker",
                default=["code_execute"],
                help_text=(
                    "Select tools this agent can use during execution. "
                    "Code Execution is enabled by default — it gives the agent a "
                    "Python sandbox (pandas, openpyxl, pdfplumber, etc.) for any "
                    "computation on upstream data. Web Search and dept-MCP tools "
                    "are opt-in."
                ),
            ),
            NodeFieldSchema(
                name="sql_connection_id", label="SQL Connection", type="connection_picker",
                connection_type="sql",
                help_text="Required when the sql_query tool is enabled",
            ),
            NodeFieldSchema(
                name="nosql_connection_id", label="NoSQL Connection", type="connection_picker",
                connection_type="mongo",
                help_text="Required when the nosql_query tool is enabled",
            ),
            NodeFieldSchema(
                name="bucket_connection_id", label="Bucket Connection", type="connection_picker",
                connection_type="bucket",
                help_text="Required when the bucket_read tool is enabled",
            ),
            NodeFieldSchema(
                name="sftp_connection_id", label="SFTP Connection", type="connection_picker",
                connection_type="sftp",
                help_text="Required when the sftp_read tool is enabled",
            ),
            NodeFieldSchema(
                name="output_schema", label="Output Schema (JSON)", type="json",
                placeholder='{"type": "object", "properties": {"summary": {"type": "string"}, "score": {"type": "number"}}}',
                help_text="Optional JSON Schema to enforce structured output format",
            ),
            NodeFieldSchema(
                name="max_iterations", label="Max Tool Iterations", type="number",
                default=10,
                help_text="Maximum number of tool-calling rounds (1-20)",
            ),
        ]

    async def execute(self, ctx: NodeContext) -> Any:
        system_prompt = ctx.config.get("system_prompt", "You are a helpful AI agent.")
        user_prompt_template = ctx.config.get("user_prompt", "{{data}}")
        tier = _coerce_tier(ctx.config.get("tier", _DEFAULT_WORKFLOW_TIER))
        tool_names = ctx.config.get("tools", [])
        output_schema = ctx.config.get("output_schema")
        max_iterations = int(ctx.config.get("max_iterations", 10))
        if max_iterations > MAX_AGENT_ITERATIONS:
            raise ValueError(
                f"max_iterations ({max_iterations}) exceeds limit ({MAX_AGENT_ITERATIONS}). "
                f"Set WF_MAX_AGENT_ITERATIONS to increase the limit."
            )

        # Build user message from template
        input_data = ctx.input_data
        data_str = json.dumps(input_data, default=str)
        if len(data_str) > MAX_AGENT_INPUT_SIZE:
            raise ValueError(
                f"Agent input data ({len(data_str)} chars) exceeds limit ({MAX_AGENT_INPUT_SIZE}). "
                f"Add a filter or transform node upstream to reduce data size, "
                f"or set WF_MAX_AGENT_INPUT_SIZE to increase the limit."
            )
        user_message = user_prompt_template.replace("{{data}}", data_str)

        # Replace variable placeholders
        user_message = interpolate_variables(user_message, ctx.variables)

        # Parse output schema if string
        if isinstance(output_schema, str):
            try:
                output_schema = json.loads(output_schema) if output_schema.strip() else None
            except json.JSONDecodeError:
                output_schema = None

        result = await _run_agent_with_tools(
            system_prompt=system_prompt,
            user_message=user_message,
            tier=tier,
            tool_names=tool_names if isinstance(tool_names, list) else [],
            user_id=ctx.user_id,
            org_id=ctx.org_id,
            owner_id=ctx.owner_id,
            owner_type=ctx.owner_type,
            environment=ctx.environment,
            sql_connection_id=ctx.config.get("sql_connection_id", ""),
            nosql_connection_id=ctx.config.get("nosql_connection_id", ""),
            bucket_connection_id=ctx.config.get("bucket_connection_id", ""),
            sftp_connection_id=ctx.config.get("sftp_connection_id", ""),
            output_schema=output_schema,
            max_iterations=max_iterations,
            workflow_context=ctx.workflow_context,
            input_data=input_data,
            jwt_token=getattr(ctx, "jwt_token", None),
            workflow_id=getattr(ctx, "workflow_id", "") or "",
            node_id=getattr(ctx, "node_id", "") or "",
            execution_id=getattr(ctx, "execution_id", "") or "",
        )

        # Wrap agent result in universal envelope
        agent_output = result.get("result", "")
        if isinstance(agent_output, dict):
            items = [agent_output]
        elif isinstance(agent_output, list):
            items = agent_output
        else:
            items = [{"result": agent_output}]

        return self._make_output(
            items=items,
            tool_calls=result.get("tool_calls", []),
            structured=result.get("structured", False),
            raw_response=result.get("raw_response"),
            warning=result.get("warning"),
        )
