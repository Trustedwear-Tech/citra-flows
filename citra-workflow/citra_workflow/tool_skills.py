"""Tool skill prompt templates for AI Agent nodes.

Each tool gets a "skill" block that is injected into the system prompt to tell
the LLM:
  - What kind of system it is connected to (nature)
  - That it is READ-ONLY (enterprise safety)
  - Best practices for using the tool efficiently
  - The discovered schema / structure (filled at runtime)
  - Known context from the workflow itself (upstream queries, columns)
"""

from __future__ import annotations
from typing import Any, Dict, Optional

from .schema_cache import MAX_SCHEMA_PROMPT_CHARS


# ---------------------------------------------------------------------------
# Workflow context formatters  (Tier 1 — "Known from this workflow")
# ---------------------------------------------------------------------------

def _fmt_workflow_sql(wf: Dict[str, Any]) -> str:
    """Format SQL workflow context into a text block."""
    lines = ["**Known from this workflow:**"]
    tables = wf.get("tables_mentioned", [])
    columns = wf.get("columns_seen", [])
    queries = wf.get("queries", [])
    if tables:
        lines.append(f"  Tables used upstream: {', '.join(tables)}")
    if columns:
        lines.append(f"  Columns seen in data: {', '.join(columns)}")
    if queries:
        for q in queries[:3]:
            # Truncate long queries
            display = q[:200] + "..." if len(q) > 200 else q
            lines.append(f"  Upstream query: `{display}`")
    return "\n".join(lines)


def _fmt_workflow_nosql(wf: Dict[str, Any]) -> str:
    lines = ["**Known from this workflow:**"]
    db = wf.get("database", "")
    colls = wf.get("collections_mentioned", [])
    fields = wf.get("fields_seen", [])
    if db:
        lines.append(f"  Database: {db}")
    if colls:
        lines.append(f"  Collections used upstream: {', '.join(colls)}")
    if fields:
        lines.append(f"  Fields seen in data: {', '.join(fields)}")
    return "\n".join(lines)


def _fmt_workflow_bucket(wf: Dict[str, Any]) -> str:
    lines = ["**Known from this workflow:**"]
    bucket = wf.get("bucket", "")
    prefixes = wf.get("top_level_prefixes", []) or wf.get("prefixes", [])
    if bucket:
        lines.append(f"  Bucket: {bucket}")
    if prefixes:
        lines.append(f"  Prefixes used: {', '.join(prefixes)}")
    return "\n".join(lines)


def _fmt_workflow_sftp(wf: Dict[str, Any]) -> str:
    lines = ["**Known from this workflow:**"]
    paths = wf.get("paths_used", []) or wf.get("paths", [])
    if paths:
        lines.append(f"  Paths used upstream: {', '.join(paths)}")
    return "\n".join(lines)


def _fmt_workflow_api(wf: Dict[str, Any]) -> str:
    lines = ["**Known from this workflow:**"]
    url = wf.get("base_url", "")
    method = wf.get("method", "")
    if url:
        lines.append(f"  URL: {url}")
    if method:
        lines.append(f"  Method: {method}")
    return "\n".join(lines)


_WORKFLOW_FORMATTERS = {
    "sql_query": _fmt_workflow_sql,
    "nosql_query": _fmt_workflow_nosql,
    "bucket_read": _fmt_workflow_bucket,
    "sftp_read": _fmt_workflow_sftp,
    "http_request": _fmt_workflow_api,
}


# ---------------------------------------------------------------------------
# Formatters: turn a schema dict into a concise text block
# ---------------------------------------------------------------------------

def _fmt_sql_schema(schema: Dict[str, Any]) -> str:
    """Format SQL schema dict into a compact text listing."""
    if "error" in schema:
        return f"(Schema unavailable: {schema['error']})"

    dialect = schema.get("dialect", "SQL")
    tables = schema.get("tables", [])
    lines = [f"Database dialect: {dialect}", f"Tables ({len(tables)}):"]
    char_count = sum(len(l) for l in lines)
    shown = 0

    for tbl in tables:
        cols = tbl.get("columns", [])
        col_parts = []
        for c in cols:
            nullable = "" if c.get("nullable", True) else " NOT NULL"
            col_parts.append(f"{c['name']} ({c['type']}{nullable})")
        tbl_line = f"  - {tbl['name']}: {', '.join(col_parts)}"
        if char_count + len(tbl_line) > MAX_SCHEMA_PROMPT_CHARS:
            remaining = len(tables) - shown
            lines.append(f"  ... and {remaining} more tables (use sql_query to explore)")
            break
        lines.append(tbl_line)
        char_count += len(tbl_line)
        shown += 1

    return "\n".join(lines)


def _fmt_nosql_schema(schema: Dict[str, Any]) -> str:
    if "error" in schema:
        return f"(Schema unavailable: {schema['error']})"

    db_name = schema.get("database", "")
    colls = schema.get("collections", [])
    lines = [f"Database: {db_name}", f"Collections ({len(colls)}):"]
    char_count = sum(len(l) for l in lines)
    shown = 0

    for coll in colls:
        fields = ", ".join(coll.get("sample_fields", []))
        est = coll.get("estimated_count", "?")
        coll_line = f"  - {coll['name']} (~{est} docs): [{fields}]"
        if char_count + len(coll_line) > MAX_SCHEMA_PROMPT_CHARS:
            remaining = len(colls) - shown
            lines.append(f"  ... and {remaining} more collections")
            break
        lines.append(coll_line)
        char_count += len(coll_line)
        shown += 1

    return "\n".join(lines)


def _fmt_bucket_structure(schema: Dict[str, Any]) -> str:
    if "error" in schema:
        return f"(Structure unavailable: {schema['error']})"

    bucket = schema.get("bucket", "")
    prefixes = schema.get("top_level_prefixes", [])
    samples = schema.get("sample_objects", [])
    lines = [f"Bucket: {bucket}"]
    if prefixes:
        lines.append(f"Top-level folders: {', '.join(prefixes)}")
    if samples:
        sample_str = ", ".join(f"{o['key']} ({o['size']}B)" for o in samples[:10])
        lines.append(f"Sample objects: {sample_str}")
    return "\n".join(lines)


def _fmt_sftp_structure(schema: Dict[str, Any]) -> str:
    if "error" in schema:
        return f"(Structure unavailable: {schema['error']})"

    host = schema.get("host", "")
    protocol = schema.get("protocol", "sftp").upper()
    entries = schema.get("root_entries", [])
    lines = [f"Server: {host} ({protocol})"]
    if entries:
        items = []
        for e in entries[:20]:
            suffix = "/" if e.get("is_dir") else ""
            items.append(f"{e['name']}{suffix}")
        lines.append(f"Root directory: {', '.join(items)}")
        if len(entries) > 20:
            lines.append(f"  ... and {len(entries) - 20} more entries")
    return "\n".join(lines)


def _fmt_api_info(schema: Dict[str, Any]) -> str:
    if "error" in schema:
        return f"(Info unavailable: {schema['error']})"
    url = schema.get("base_url", "")
    auth = schema.get("auth_type", "none")
    return f"Base URL: {url}\nAuthentication: {auth}"


_SCHEMA_FORMATTERS = {
    "sql_query": _fmt_sql_schema,
    "nosql_query": _fmt_nosql_schema,
    "bucket_read": _fmt_bucket_structure,
    "sftp_read": _fmt_sftp_structure,
    "http_request": _fmt_api_info,
}


# ---------------------------------------------------------------------------
# Skill templates
# ---------------------------------------------------------------------------

TOOL_SKILLS = {
    "sql_query": """\
### SQL Database (read-only)
You are connected to an enterprise SQL database. **Only SELECT and WITH (CTE) queries are allowed.**
Any INSERT, UPDATE, DELETE, DROP, ALTER, or other write operations will be blocked.

{schema_text}

Best practices:
- Always add a LIMIT clause to avoid large result sets (default limit is 50 rows).
- Use exact table and column names from the schema above.
- Start with exploratory queries (SELECT * FROM table LIMIT 5) before writing complex joins.
- Use COUNT(*) to understand data volume before fetching rows.
- If the schema shows many tables, focus on the ones relevant to the user's question.
""",

    "nosql_query": """\
### MongoDB / DocumentDB (read-only)
You are connected to a MongoDB-compatible database. Only read operations (find) are allowed.

{schema_text}

Best practices:
- Use the exact collection names listed above.
- Start with a small limit (5-10 docs) to understand document structure before querying broadly.
- Use filter documents to narrow results: {{"field": "value"}}.
- The sample_fields shown above are inferred from a few documents — the actual schema may vary per document.
""",

    "bucket_read": """\
### Bucket Storage (read-only)
You have read-only access to an S3-compatible / MinIO object store. You can list objects and download files.
No uploads or deletions are permitted.

{schema_text}

Best practices:
- Start by listing with a prefix to narrow down the bucket contents.
- Use the 'list' action before 'download' to find the correct object key.
- Downloaded text files are returned as-is; binary files are base64-encoded.
- Large files (>5 MB) cannot be downloaded.
""",

    "sftp_read": """\
### SFTP/FTP Server (read-only)
You have read-only access to a file server. You can list directories and download files.
No uploads or deletions are permitted.

{schema_text}

Best practices:
- Start by listing directories to navigate the file system structure.
- Use the 'list' action to explore before downloading files.
- Downloaded text files are returned as-is; binary files are base64-encoded.
- Large files (>5 MB) cannot be downloaded.
""",

    "http_request": """\
### HTTP / REST API
You can make HTTP requests to an external API endpoint. Authentication is pre-configured.

{schema_text}

Best practices:
- Use GET requests for reading data; avoid POST/PUT/DELETE unless explicitly instructed.
- This is an enterprise system — prefer read-only operations.
- Start with known endpoints (e.g., the base URL, health checks) before exploring.
- Parse JSON responses to extract relevant data.
- Handle pagination if the API returns paginated results.
""",

    "web_search": """\
### Web Search
You can search the internet for real-time information using DuckDuckGo.

Best practices:
- Use specific, focused search queries for better results.
- Combine multiple search results to form a comprehensive answer.
- Verify information across multiple results when possible.
""",

    "code_execute": """\
### Python Code Execution (sandboxed)
You can execute Python code snippets in a secure sandbox. No imports are allowed.
Available builtins: print, len, range, str, int, float, list, dict, tuple, set, bool,
min, max, sum, abs, round, sorted, enumerate, zip, map, filter, isinstance, json.

Best practices:
- Use for calculations, data processing, formatting, and analysis.
- All output must go through print() — the return value is captured from stdout.
- No file I/O, network access, or module imports are available.
""",
}


# ---------------------------------------------------------------------------
# Build the skill section for the system prompt
# ---------------------------------------------------------------------------

def format_tool_skill(
    tool_name: str,
    schema: Optional[Dict[str, Any]] = None,
) -> str:
    """Return the skill prompt block for a single tool.

    *schema* is the result of ``get_or_discover_schema()`` — may be ``None``
    for tools that have no discovery function (e.g. ``web_search``).

    When the schema contains a ``_workflow`` key (workflow context merged in)
    or ``_from_workflow`` (pure workflow context), the workflow-derived info
    is rendered **first** as "Known from this workflow", followed by the full
    discovered schema.
    """
    template = TOOL_SKILLS.get(tool_name)
    if template is None:
        return ""

    parts: list[str] = []

    if schema is not None:
        # Workflow context (rendered first — most relevant)
        wf_data = schema.get("_workflow") or (schema if schema.get("_from_workflow") else None)
        if wf_data:
            wf_formatter = _WORKFLOW_FORMATTERS.get(tool_name)
            if wf_formatter:
                parts.append(wf_formatter(wf_data))

        # Full discovered schema (rendered second — supplementary)
        if not schema.get("_from_workflow"):
            formatter = _SCHEMA_FORMATTERS.get(tool_name)
            if formatter:
                full_text = formatter(schema)
                if full_text:
                    if parts:
                        parts.append("\n**Full schema (from introspection):**")
                    parts.append(full_text)

    schema_text = "\n".join(parts)
    return template.format(schema_text=schema_text)


def build_tool_skills_section(
    tool_schemas: Dict[str, Optional[Dict[str, Any]]],
    workflow_context: Optional[Dict[str, Any]] = None,
) -> str:
    """Build the full ``## Connected Tools`` section for the system prompt.

    *tool_schemas* maps ``tool_name → schema_dict | None``.
    *workflow_context* is the raw output of ``extract_workflow_context()`` —
    used to render upstream column info.
    """
    blocks = []
    for tool_name, schema in tool_schemas.items():
        block = format_tool_skill(tool_name, schema)
        if block:
            blocks.append(block)

    if not blocks:
        return ""

    header = (
        "\n\n## Connected Tools — READ-ONLY\n"
        "Below is a description of each tool you can use, including connection details "
        "and schema information discovered from the target systems. "
        "**All connections are to enterprise systems — you MUST only perform read operations.**\n\n"
    )

    # Append upstream column context if available
    upstream_cols = (workflow_context or {}).get("_upstream_columns", {}).get("columns", [])
    footer = ""
    if upstream_cols:
        cols_str = ", ".join(upstream_cols[:50])
        footer = (
            f"\n\n### Upstream Data Context\n"
            f"Columns available from the previous workflow step: {cols_str}\n"
            f"The input data passed to you (via {{{{data}}}}) contains these fields.\n"
        )

    return header + "\n".join(blocks) + footer
