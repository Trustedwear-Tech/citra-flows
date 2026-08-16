"""Output / sink nodes — write data, export files, send notifications."""

from __future__ import annotations
import base64
import json
import logging
import os
from typing import Any, Dict, List, Optional

from ..models import NodeType, NodeCategory, NodeFieldSchema
from ..utils.objectstore import require_bucket
from . import BaseNode, NodeContext, register_node, interpolate_variables, sanitize_remote_path
from ..config import (
    MONGO_CONNECT_TIMEOUT_MS, HTTP_TIMEOUT_PDF_RENDER,
    HTTP_TIMEOUT_WEBHOOK_OUTPUT, FTP_CONNECT_TIMEOUT,
    MAX_EMAIL_BODY_SIZE, MAX_TABLE_RECORDS,
)

logger = logging.getLogger(__name__)


@register_node
class SQLWriterNode(BaseNode):
    node_type = NodeType.SQL_WRITER
    category = NodeCategory.OUTPUT
    label = "SQL Writer"
    description = "Write records to a SQL table"
    icon = "💾"
    color = "#f59e0b"

    @classmethod
    def get_fields(cls) -> List[NodeFieldSchema]:
        return [
            NodeFieldSchema(name="connection_id", label="Connection", type="connection_picker",
                            connection_type="sql",
                            help_text="Select a saved SQL connection (overrides inline connection string)"),
            NodeFieldSchema(name="connection_string", label="Connection String", type="password",
                            help_text="Used if no saved connection selected"),
            NodeFieldSchema(name="table", label="Table Name", type="text", required=True, placeholder="results"),
            NodeFieldSchema(name="mode", label="Write Mode", type="select", default="append",
                            options=[{"label": "Append", "value": "append"},
                                     {"label": "Replace", "value": "replace"}]),
        ]

    async def execute(self, ctx: NodeContext) -> Any:
        import pandas as pd
        import sqlalchemy
        import asyncio

        # Resolve connection: saved connection_id takes priority over inline
        connection_id = ctx.config.get("connection_id")
        if connection_id:
            from ..connection_resolver import resolve_connection
            resolved = await resolve_connection(
                connection_id, org_id=ctx.org_id, owner_id=ctx.owner_id,
                owner_type=ctx.owner_type, environment=ctx.environment,
            )
            conn_str = resolved["connection_string"]
        else:
            conn_str = ctx.config.get("connection_string", "")
        if not conn_str:
            raise ValueError("A SQL connection is required — select a saved connection or provide an inline connection string")

        table = ctx.config["table"]
        mode = ctx.config.get("mode", "append")
        records = ctx.items

        if not records:
            return self._make_output(items=[], written=0, table=table)

        def _write():
            engine = sqlalchemy.create_engine(conn_str, pool_pre_ping=True)
            df = pd.DataFrame(records)
            # Serialize list/dict values to JSON strings for SQL compatibility
            for col in df.columns:
                df[col] = df[col].apply(lambda v: json.dumps(v, default=str) if isinstance(v, (list, dict)) else v)
            df.to_sql(table, engine, if_exists=mode, index=False)
            engine.dispose()
            return len(df)

        count = await asyncio.get_event_loop().run_in_executor(None, _write)
        logger.info(f"SQLWriter: wrote {count} records to {table}")
        return self._make_output(items=[], written=count, table=table)


@register_node
class PDFExportNode(BaseNode):
    node_type = NodeType.PDF_EXPORT
    category = NodeCategory.OUTPUT
    label = "Export PDF"
    description = "Generate a PDF report from data using Playwright render service"
    icon = "📑"
    color = "#f59e0b"

    @classmethod
    def get_fields(cls) -> List[NodeFieldSchema]:
        return [
            NodeFieldSchema(name="title", label="Report Title", type="text", required=True),
            NodeFieldSchema(name="template", label="HTML Template", type="textarea",
                            help_text="Use {{data}} for data, {{title}} for title. Leave blank for default table."),
            NodeFieldSchema(name="save_to_bucket", label="Save to Bucket", type="boolean", default=True),
            NodeFieldSchema(name="bucket_name", label="S3 Bucket", type="text",
                            help_text="Bucket to write to. Falls back to the BUCKET_NAME env var; there is no built-in default."),
        ]

    async def execute(self, ctx: NodeContext) -> Any:
        import httpx
        import os

        title = ctx.config.get("title", "Workflow Report")
        template = ctx.config.get("template", "")
        data = ctx.input_data

        # Prevent double extension: strip .pdf suffix before we append it later
        safe_title = title
        if safe_title.lower().endswith(".pdf"):
            safe_title = safe_title[:-4]

        # Build HTML
        if template:
            import html as _html
            html = template.replace("{{title}}", _html.escape(str(title))).replace("{{data}}", json.dumps(data, default=str))
        else:
            # Auto-generate table HTML from items
            records = ctx.items
            html = _build_table_html(title, records, data)

        # Call Playwright render service
        render_url = (os.getenv("PLAYWRIGHT_RENDER_URL")
                      or os.getenv("PLAYWRIGHT_SERVICE_URL")
                      or "http://playwright-render-service:3001")
        try:
            async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_PDF_RENDER) as client:
                resp = await client.post(f"{render_url}/render-html", json={
                    "html": html,
                    "output_format": "pdf",
                })
        except httpx.RequestError as exc:
            # Connection/timeout to the render service — name the URL so the
            # failure is actionable instead of the bare "All connection attempts
            # failed", and point at the dependency rather than the workflow.
            raise RuntimeError(
                f"Export PDF could not reach the Playwright render service at "
                f"{render_url} ({type(exc).__name__}). Ensure the render service "
                f"is running and PLAYWRIGHT_RENDER_URL points to it."
            ) from exc
        if resp.status_code != 200:
            raise RuntimeError(
                f"Playwright render service at {render_url} returned "
                f"{resp.status_code}: {resp.text[:200]}"
            )

        pdf_bytes = resp.content
        meta = {"size_bytes": len(pdf_bytes), "title": title,
                "download": True, "filename": f"{safe_title.replace(' ', '_')}.pdf",
                "content_b64": base64.b64encode(pdf_bytes).decode("ascii"),
                "content_type": "application/pdf"}

        if ctx.config.get("save_to_bucket", True):
            import asyncio
            from bucket import get_client

            bucket = require_bucket(ctx.config.get("bucket_name"), os.getenv("BUCKET_NAME"), node=self.__class__.__name__)
            env = os.getenv("ENVIRONMENT", "dev")
            object_key = f"{env}/workflows/{ctx.user_id}/{ctx.execution_id}/{safe_title.replace(' ', '_')}.pdf"

            def _upload():
                s3 = get_client()
                s3.put_object(Bucket=bucket, Key=object_key, Body=pdf_bytes, ContentType="application/pdf")
                return object_key

            key = await asyncio.get_event_loop().run_in_executor(None, _upload)
            meta["object_key"] = key

        return self._make_output(items=ctx.items, **meta)


@register_node
class ExcelExportNode(BaseNode):
    node_type = NodeType.EXCEL_EXPORT
    category = NodeCategory.OUTPUT
    label = "Export Excel"
    description = "Generate an Excel file and save to bucket"
    icon = "📊"
    color = "#f59e0b"

    @classmethod
    def get_fields(cls) -> List[NodeFieldSchema]:
        return [
            NodeFieldSchema(name="filename", label="File Name", type="text", default="export.xlsx"),
            NodeFieldSchema(name="sheet_name", label="Sheet Name", type="text", default="Data"),
            NodeFieldSchema(name="bucket_name", label="S3 Bucket", type="text",
                            help_text="Bucket to write to. Falls back to the BUCKET_NAME env var; there is no built-in default."),
        ]

    async def execute(self, ctx: NodeContext) -> Any:
        import pandas as pd
        import io
        import asyncio
        from bucket import get_client

        records = ctx.items
        filename = ctx.config.get("filename", "export.xlsx")
        sheet = ctx.config.get("sheet_name", "Data")

        # Flatten nested values and hide internal fields for clean Excel export
        clean_records = _flatten_records_for_export(records)

        buf = io.BytesIO()
        df = pd.DataFrame(clean_records) if clean_records else pd.DataFrame()
        df.to_excel(buf, sheet_name=sheet, index=False, engine="openpyxl")
        buf.seek(0)

        bucket = require_bucket(ctx.config.get("bucket_name"), os.getenv("BUCKET_NAME"), node=self.__class__.__name__)
        env = os.getenv("ENVIRONMENT", "dev")
        object_key = f"{env}/workflows/{ctx.user_id}/{ctx.execution_id}/{filename}"

        def _up():
            s3 = get_client()
            s3.put_object(Bucket=bucket, Key=object_key, Body=buf.getvalue(),
                          ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

        await asyncio.get_event_loop().run_in_executor(None, _up)
        excel_bytes = buf.getvalue()
        return self._make_output(items=ctx.items, object_key=object_key, rows=len(df), filename=filename,
                                 download=True, content_b64=base64.b64encode(excel_bytes).decode("ascii"),
                                 content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@register_node
class CSVExportNode(BaseNode):
    node_type = NodeType.CSV_EXPORT
    category = NodeCategory.OUTPUT
    label = "Export CSV"
    description = "Export data as CSV to bucket"
    icon = "📋"
    color = "#f59e0b"

    @classmethod
    def get_fields(cls) -> List[NodeFieldSchema]:
        return [
            NodeFieldSchema(name="filename", label="File Name", type="text", default="export.csv"),
            NodeFieldSchema(name="bucket_name", label="S3 Bucket", type="text",
                            help_text="Bucket to write to. Falls back to the BUCKET_NAME env var; there is no built-in default."),
        ]

    async def execute(self, ctx: NodeContext) -> Any:
        import pandas as pd
        import asyncio
        from bucket import get_client

        records = ctx.items
        filename = ctx.config.get("filename", "export.csv")

        # Flatten nested values and hide internal fields for clean CSV export
        clean_records = _flatten_records_for_export(records)

        df = pd.DataFrame(clean_records) if clean_records else pd.DataFrame()
        csv_bytes = df.to_csv(index=False).encode("utf-8")

        bucket = require_bucket(ctx.config.get("bucket_name"), os.getenv("BUCKET_NAME"), node=self.__class__.__name__)
        env = os.getenv("ENVIRONMENT", "dev")
        object_key = f"{env}/workflows/{ctx.user_id}/{ctx.execution_id}/{filename}"

        def _up():
            s3 = get_client()
            s3.put_object(Bucket=bucket, Key=object_key, Body=csv_bytes, ContentType="text/csv")

        await asyncio.get_event_loop().run_in_executor(None, _up)
        return self._make_output(items=ctx.items, object_key=object_key, rows=len(df), filename=filename,
                                 download=True, content_b64=base64.b64encode(csv_bytes).decode("ascii"),
                                 content_type="text/csv")


@register_node
class EmailSenderNode(BaseNode):
    node_type = NodeType.EMAIL_SENDER
    category = NodeCategory.OUTPUT
    label = "Send Email"
    description = "Send email with results"
    icon = "✉️"
    color = "#f59e0b"

    @classmethod
    def get_fields(cls) -> List[NodeFieldSchema]:
        return [
            NodeFieldSchema(name="to", label="To", type="text", required=True, placeholder="user@example.com"),
            NodeFieldSchema(name="subject", label="Subject", type="text", required=True),
            NodeFieldSchema(name="body_template", label="Body Template", type="textarea",
                            placeholder="Hello,\n\nWorkflow completed.\n\n{{data}}\n\nRegards"),
            NodeFieldSchema(name="include_data", label="Include Data in Body", type="boolean", default=True),
        ]

    async def execute(self, ctx: NodeContext) -> Any:
        import httpx

        to_addr = ctx.config["to"]
        subject = ctx.config["subject"]
        body_template = ctx.config.get("body_template", "Workflow results:\n\n{{data}}")

        items = ctx.items or []

        # Rank best-first for the shortlist table when the upstream evaluation
        # stamped a rank/score on each record (the email reads top-down, so a
        # ranked list must be sorted). rank ascending (1 = best) wins; else
        # score descending. Records without either keep their original order.
        def _rank_sort_key(rec):
            if isinstance(rec, dict):
                rv = rec.get("rank")
                if isinstance(rv, (int, float)) and not isinstance(rv, bool):
                    return (0, rv)
                for sk in ("score", "llm_score", "_score"):
                    sv = rec.get(sk)
                    if isinstance(sv, (int, float)) and not isinstance(sv, bool):
                        return (1, -sv)
            return (2, 0)

        if items and all(isinstance(r, dict) for r in items):
            items = sorted(items, key=_rank_sort_key)

        # Build HTML table string when items are present
        if items and isinstance(items[0], dict):
            import html as _html_mod

            # Detect whether the template already contains a <table> tag —
            # if so, {{data}} should emit only <tr> rows, not a nested table.
            template_has_table = "<table" in body_template.lower()

            # Recruiter-friendly column mapping: pick human-readable names
            # when the data contains standard candidate fields. Evaluation
            # fields (score/rank/decision/reason) produced by an upstream
            # LLM/ranking step are surfaced too, so the shortlist email shows
            # WHY each candidate ranked where they did — not just contact info.
            # First key matching a display name wins (so we don't duplicate a
            # column when, e.g., both `decision` and `recommendation` exist).
            _FRIENDLY_COLS = [
                ("rank", "Rank"),
                ("full name", "Candidate Name"),
                ("full_name", "Candidate Name"),
                ("name", "Candidate Name"),
                ("email", "Email"),
                ("phone", "Phone"),
                ("score", "Score"),
                ("llm_score", "Score"),
                ("_score", "Score"),
                ("evaluation info", "Score"),
                ("classification", "Classification"),
                ("decision", "Decision"),
                ("recommendation", "Decision"),
                ("recommended_next_step", "Next Step"),
                ("next_step", "Next Step"),
                ("reason", "Reason"),
                ("reasoning", "Reason"),
                ("_rule_reason", "Reason"),
                ("skills", "Skills"),
                ("_status", "Status"),
                ("_rule_passed", "Status"),
            ]

            def _pick_email_columns(record):
                """Return (display_name, key) pairs for the email table."""
                picked = []
                seen_display = set()
                for key, display in _FRIENDLY_COLS:
                    if key in record and display not in seen_display:
                        picked.append((display, key))
                        seen_display.add(display)
                if not picked:
                    # Fallback: use all keys
                    return [(str(k), k) for k in record.keys()]
                return picked

            email_cols = _pick_email_columns(items[0])

            def _fmt_cell(val):
                """Format a cell value for email display."""
                if isinstance(val, bool):
                    return "Selected" if val else "Rejected"
                if isinstance(val, list):
                    return ", ".join(str(v) for v in val)
                if isinstance(val, dict):
                    score = val.get("score")
                    if score is not None:
                        return str(score)
                    return ", ".join(f"{k}: {v}" for k, v in val.items())
                s = str(val)
                if s.lower() in ("true", "false"):
                    return "Selected" if s.lower() == "true" else "Rejected"
                return s

            if template_has_table:
                # Emit only <tr> rows — the template already provides <table> + <thead>
                rows_html = ""
                for i, rec in enumerate(items):
                    bg = "#ffffff" if i % 2 == 0 else "#f9f9f9"
                    cells = "".join(
                        f"<td style='padding:8px;border:1px solid #ccc;background:{bg}'>"
                        f"{_html_mod.escape(_fmt_cell(rec.get(k, '')))}</td>"
                        for _display, k in email_cols
                    )
                    rows_html += f"<tr>{cells}</tr>"
                items_table_html = rows_html
                items_rows_only = rows_html
            else:
                # Build a full standalone table
                header = "".join(
                    f"<th style='padding:8px;border:1px solid #ccc;background:#f0f4ff;font-weight:bold'>"
                    f"{_html_mod.escape(display)}</th>" for display, _k in email_cols
                )
                rows_html = ""
                for i, rec in enumerate(items):
                    bg = "#ffffff" if i % 2 == 0 else "#f9f9f9"
                    cells = "".join(
                        f"<td style='padding:8px;border:1px solid #ccc;background:{bg}'>"
                        f"{_html_mod.escape(_fmt_cell(rec.get(k, '')))}</td>"
                        for _display, k in email_cols
                    )
                    rows_html += f"<tr>{cells}</tr>"
                items_table_html = (
                    f"<table style='border-collapse:collapse;width:100%;font-family:Arial,sans-serif;font-size:13px'>"
                    f"<thead><tr>{header}</tr></thead><tbody>{rows_html}</tbody></table>"
                    f"<p style='color:#666;font-size:12px'>Total records: {len(items)}</p>"
                )
                items_rows_only = rows_html
        else:
            items_table_html = "<p><em>No candidate records found.</em></p>"
            items_rows_only = items_table_html

        # Auto-replace {{data}} with HTML table when items exist (backwards-compat),
        # also support explicit {{items_table}} placeholder.
        if "{{data}}" in body_template:
            if items and isinstance(items[0], dict):
                body = body_template.replace("{{data}}", items_table_html if not template_has_table else items_rows_only)
            else:
                # Only serialize/size-check the raw input when the template
                # actually asks for {{data}}.
                data_str = json.dumps(ctx.input_data, indent=2, default=str)
                if len(data_str) > MAX_EMAIL_BODY_SIZE:
                    raise ValueError(
                        f"Email body data ({len(data_str)} chars) exceeds limit ({MAX_EMAIL_BODY_SIZE}). "
                        f"Add a filter/transform node upstream to reduce data size, "
                        f"or set WF_MAX_EMAIL_BODY_SIZE to increase the limit."
                    )
                body = body_template.replace("{{data}}", data_str)
        else:
            # The template builds its own body (e.g. {{items_table}}, {{html_body}},
            # or {{field}} placeholders) and does NOT reference {{data}} — don't
            # serialize the raw input or enforce the {{data}} size limit, which
            # otherwise fails a perfectly valid custom HTML report template.
            body = body_template

        body = body.replace("{{items_table}}", items_table_html)

        # Expand {{items[N].field}} and {{field}} (shorthand for first item)
        # placeholders. Reuse the SAME best-first-sorted `items` used to build
        # {{items_table}} above, so subject/body field placeholders line up with
        # the top-ranked row shown in the table (rather than the original,
        # unsorted first record).
        import re as _re

        def _expand_item_path(m):
            idx_str, key = m.group(1), m.group(2)
            try:
                item = items[int(idx_str)]
                if isinstance(item, dict) and key in item:
                    return str(item[key])
            except (IndexError, TypeError, ValueError):
                pass
            return m.group(0)  # leave unchanged if not resolvable

        body = _re.sub(r'\{\{items\[(\d+)\]\.(\w+)\}\}', _expand_item_path, body)

        # Shorthand: {{field}} → first item's field value (only if not already a workflow variable)
        if items and isinstance(items[0], dict):
            first = items[0]
            for k, v in first.items():
                placeholder = "{{" + k + "}}"
                if placeholder in body:
                    body = body.replace(placeholder, str(v))

        body = interpolate_variables(body, ctx.variables)

        # Apply the same item-field and workflow-variable interpolation to
        # the subject so templates like "Report for {{name}}" get rendered.
        if items and isinstance(items[0], dict):
            first = items[0]
            for k, v in first.items():
                placeholder = "{{" + k + "}}"
                if placeholder in subject:
                    subject = subject.replace(placeholder, str(v))
        subject = interpolate_variables(subject, ctx.variables)

        # Send via User Service API (uses AWS SES)
        user_service_url = os.getenv("USER_SERVICE_URL", "http://localhost:7004")
        payload = {
            "to": to_addr,
            "subject": subject,
            "body": body,
        }

        # ── Idempotency: one Send Email node sends at most once per execution ──
        # The executor schedules each node once, but defense-in-depth here guards
        # against any duplicate dispatch (e.g. a job redelivered to a second
        # worker with the same execution_id, or a future fan-in regression). The
        # claim is keyed by execution_id + node_id + recipient + subject so two
        # genuinely different runs (distinct execution_id) still each send.
        import hashlib
        _item_count = len(items)
        _retry_count = int(ctx.config.get("max_retries") or 0)
        _digest = hashlib.sha1(f"{to_addr}|{subject}".encode("utf-8")).hexdigest()[:16]
        _idem_key = f"wf:email:sent:{ctx.execution_id}:{ctx.node_id}:{_digest}"

        # Audit log immediately before the actual send — lets us confirm exactly
        # how many real send calls happen per execution.
        logger.info(
            "EmailSender: dispatch execution_id=%s workflow_id=%s node_id=%s "
            "upstream=%s recipient=%s subject=%r items=%d retries=%d",
            ctx.execution_id, ctx.workflow_id, ctx.node_id,
            (ctx.input_data.get("meta", {}) or {}).get("node_id")
            if isinstance(ctx.input_data, dict) else "multi",
            to_addr, subject, _item_count, _retry_count,
        )

        _cache = None
        try:
            from citra_cache import get_cache_manager
            _cache = get_cache_manager()
            if _cache.exists(_idem_key):
                logger.warning(
                    "EmailSender: SKIP duplicate send — already sent for "
                    "execution_id=%s node_id=%s recipient=%s subject=%r",
                    ctx.execution_id, ctx.node_id, to_addr, subject,
                )
                return self._make_output(
                    items=[], sent=False, to=to_addr, subject=subject,
                    reason="duplicate suppressed (idempotency)",
                )
        except Exception as _idem_exc:  # noqa: BLE001 — never block a real send on cache errors
            logger.warning("EmailSender: idempotency pre-check unavailable (%s) — proceeding", _idem_exc)
            _cache = None

        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(
                    f"{user_service_url}/api/send-workflow-email",
                    json=payload,
                )
            if resp.status_code == 200:
                # Mark sent BEFORE returning so any concurrent/duplicate dispatch
                # observes the marker. TTL bounds the key's lifetime well past a
                # single execution. Best-effort: a cache failure must not fail the
                # run — the email already went out.
                if _cache is not None:
                    try:
                        _cache.set(_idem_key, "1", ex=86400)
                    except Exception as _mark_exc:  # noqa: BLE001
                        logger.warning("EmailSender: failed to record idempotency marker (%s)", _mark_exc)
                logger.info(f"EmailSender: Email sent to {to_addr} via User Service")
                return self._make_output(items=[], sent=True, to=to_addr, subject=subject)
            else:
                logger.warning(f"EmailSender: User Service returned {resp.status_code}: {resp.text[:200]}")
                return self._make_output(items=[], sent=False, reason=f"User Service error: {resp.status_code}")
        except Exception as e:
            logger.error(f"EmailSender: Failed to send via User Service: {e}")
            return self._make_output(items=[], sent=False, reason=f"Email send failed: {str(e)}")


@register_node
class FileDownloadNode(BaseNode):
    node_type = NodeType.FILE_DOWNLOAD
    category = NodeCategory.OUTPUT
    label = "Download"
    description = "Download output data as a file"
    icon = "⬇️"
    color = "#06b6d4"

    @classmethod
    def get_fields(cls) -> List[NodeFieldSchema]:
        return [
            NodeFieldSchema(name="filename", label="File Name", type="text", required=True,
                            placeholder="output.json",
                            help_text="Name of the file to download"),
            NodeFieldSchema(name="file_format", label="Format", type="select", default="json",
                            options=[
                                {"label": "JSON", "value": "json"},
                                {"label": "CSV", "value": "csv"},
                                {"label": "Plain Text", "value": "text"},
                            ]),
        ]

    async def execute(self, ctx: NodeContext) -> Any:
        import base64

        filename = ctx.config.get("filename", "output.json")
        file_format = ctx.config.get("file_format", "json")
        items = self._extract_items(ctx.input_data)

        # Filter out internal fields from all export formats
        def _clean_item(rec):
            if not isinstance(rec, dict):
                return rec
            return {k: v for k, v in rec.items() if k not in _INTERNAL_FIELDS}

        clean_items = [_clean_item(item) for item in items]

        if file_format == "csv" and clean_items:
            import csv
            import io
            buf = io.StringIO()
            if isinstance(clean_items[0], dict):
                # Flatten nested values for CSV readability
                flat_items = [{k: _flatten_export_value(v) for k, v in rec.items()} for rec in clean_items]
                writer = csv.DictWriter(buf, fieldnames=flat_items[0].keys())
                writer.writeheader()
                writer.writerows(flat_items)
            else:
                writer = csv.writer(buf)
                for row in clean_items:
                    writer.writerow([row] if not isinstance(row, (list, tuple)) else row)
            content = buf.getvalue()
            content_type = "text/csv"
        elif file_format == "text":
            # Format items as human-readable text — NOT a JSON dump
            lines = []
            for i, item in enumerate(clean_items):
                lines.append(f"--- Record {i + 1} ---")
                if isinstance(item, dict):
                    for k, v in item.items():
                        lines.append(f"  {k}: {_flatten_export_value(v)}")
                else:
                    lines.append(f"  {item}")
                lines.append("")
            content = "\n".join(lines) if lines else "No data"
            content_type = "text/plain"
        else:
            # JSON: export items array only (not the meta envelope)
            content = json.dumps(clean_items, indent=2, default=str)
            content_type = "application/json"

        # Store file content directly in output (base64 encoded) for download
        content_bytes = content.encode("utf-8")
        content_b64 = base64.b64encode(content_bytes).decode("ascii")

        return self._make_output(
            items=items,
            download=True,
            filename=filename,
            content_b64=content_b64,
            content_type=content_type,
            size_bytes=len(content_bytes),
        )


@register_node
class BucketWriterNode(BaseNode):
    node_type = NodeType.BUCKET_WRITER
    category = NodeCategory.OUTPUT
    label = "Save to Bucket"
    description = "Save data as JSON to object storage bucket"
    icon = "☁️"
    color = "#f59e0b"

    @classmethod
    def get_fields(cls) -> List[NodeFieldSchema]:
        return [
            NodeFieldSchema(name="connection_id", label="Connection", type="connection_picker",
                            connection_type="s3",
                            help_text="Select a saved S3 connection (overrides inline connection/bucket settings)"),
            NodeFieldSchema(name="filename", label="File Name", type="text", required=True,
                            placeholder="output.json"),
            NodeFieldSchema(name="bucket_name", label="Bucket Name (override)", type="text",
                            help_text="Leave blank to use the default BUCKET_NAME env var"),
            NodeFieldSchema(name="aws_access_key_id", label="AWS Access Key ID (override)", type="text",
                            help_text="Leave blank to use the default AWS_S3_ACCESS_KEY_ID env var"),
            NodeFieldSchema(name="aws_secret_access_key", label="AWS Secret Access Key (override)", type="password",
                            help_text="Leave blank to use the default AWS_S3_SECRET_ACCESS_KEY env var"),
            NodeFieldSchema(name="aws_region", label="AWS Region (override)", type="text",
                            help_text="Leave blank to use the default AWS_S3_REGION env var"),
            NodeFieldSchema(name="use_env_prefix", label="Prepend environment prefix", type="boolean", default=True,
                            help_text="When enabled, automatically prepends 'dev/', 'prod/' etc. to the key. Disable when writing to external buckets."),
        ]

    async def execute(self, ctx: NodeContext) -> Any:
        import asyncio
        import boto3
        from botocore.client import Config as BotoConfig

        filename = ctx.config.get("filename", "output.json")
        connection_id = ctx.config.get("connection_id")
        endpoint_url = None

        if connection_id:
            from ..connection_resolver import resolve_connection
            resolved = await resolve_connection(
                connection_id, org_id=ctx.org_id, owner_id=ctx.owner_id,
                owner_type=ctx.owner_type, environment=ctx.environment,
            )
            bucket = require_bucket(resolved.get("bucket"), resolved.get("bucket_name"), os.getenv("BUCKET_NAME"), node=self.__class__.__name__)
            access_key = resolved.get("access_key_id") or resolved.get("aws_access_key_id") or os.getenv("AWS_S3_ACCESS_KEY_ID")
            secret_key = resolved.get("secret_access_key") or resolved.get("aws_secret_access_key") or os.getenv("AWS_S3_SECRET_ACCESS_KEY")
            region = resolved.get("region") or resolved.get("aws_region") or os.getenv("AWS_S3_REGION")
            endpoint_url = resolved.get("endpoint_url")
        else:
            bucket = require_bucket(ctx.config.get("bucket_name"), os.getenv("BUCKET_NAME"), node=self.__class__.__name__)
            access_key = ctx.config.get("aws_access_key_id") or os.getenv("AWS_S3_ACCESS_KEY_ID")
            secret_key = ctx.config.get("aws_secret_access_key") or os.getenv("AWS_S3_SECRET_ACCESS_KEY")
            region = ctx.config.get("aws_region") or os.getenv("AWS_S3_REGION")

        use_env_prefix = ctx.config.get("use_env_prefix", True)
        env = os.getenv("ENVIRONMENT", "dev")
        if use_env_prefix:
            object_key = f"{env}/workflows/{ctx.user_id}/{ctx.execution_id}/{filename}"
        else:
            object_key = filename

        body = json.dumps(ctx.input_data, indent=2, default=str).encode("utf-8")

        def _up():
            if access_key and secret_key:
                kwargs = {}
                if endpoint_url:
                    kwargs["endpoint_url"] = endpoint_url
                s3 = boto3.client(
                    's3',
                    aws_access_key_id=access_key,
                    aws_secret_access_key=secret_key,
                    region_name=region,
                    config=BotoConfig(signature_version='s3v4'),
                    **kwargs
                )
            else:
                from bucket import get_client
                s3 = get_client()

            s3.put_object(Bucket=bucket, Key=object_key, Body=body, ContentType="application/json")

        await asyncio.get_event_loop().run_in_executor(None, _up)
        return self._make_output(items=[], object_key=object_key, size_bytes=len(body))


@register_node
class SFTPWriterNode(BaseNode):
    node_type = NodeType.SFTP_WRITER
    category = NodeCategory.OUTPUT
    label = "SFTP / FTP Writer"
    description = "Upload files to an SFTP or FTP server"
    icon = "📁"
    color = "#f59e0b"

    @classmethod
    def get_fields(cls) -> List[NodeFieldSchema]:
        return [
            NodeFieldSchema(name="connection_id", label="Connection", type="connection_picker",
                            connection_type="sftp",
                            help_text="Select a saved SFTP/FTP connection (overrides inline fields)"),
            NodeFieldSchema(name="host", label="Host", type="text",
                            placeholder="sftp.example.com",
                            help_text="Used if no saved connection selected"),
            NodeFieldSchema(name="port", label="Port", type="number", default=22,
                            help_text="22 for SFTP, 21 for FTP"),
            NodeFieldSchema(name="username", label="Username", type="text", placeholder="myuser"),
            NodeFieldSchema(name="password", label="Password", type="password",
                            placeholder="(leave blank for key-based auth)"),
            NodeFieldSchema(name="private_key", label="Private Key (PEM)", type="textarea",
                            help_text="Paste SSH private key for key-based authentication (SFTP only)"),
            NodeFieldSchema(name="protocol", label="Protocol", type="select", default="sftp",
                            options=[{"label": "SFTP (SSH)", "value": "sftp"},
                                     {"label": "FTP", "value": "ftp"},
                                     {"label": "FTPS (FTP over TLS)", "value": "ftps"}]),
            NodeFieldSchema(name="remote_path", label="Remote File Path", type="text", required=True,
                            placeholder="/uploads/{{applicant_id}}/result.csv",
                            help_text="Supports {{variable}} placeholders from trigger data. Full path including filename on the remote server"),
            NodeFieldSchema(name="file_format", label="File Format", type="select", default="json",
                            options=[
                                {"label": "JSON", "value": "json"},
                                {"label": "CSV", "value": "csv"},
                                {"label": "Excel (XLSX)", "value": "excel"},
                                {"label": "Plain Text", "value": "text"},
                            ],
                            help_text="How to serialize the input data before uploading"),
            NodeFieldSchema(name="create_dirs", label="Create Directories", type="boolean", default=True,
                            help_text="Automatically create parent directories on the server if they don't exist"),
        ]

    async def execute(self, ctx: NodeContext) -> Any:
        import asyncio

        remote_path = sanitize_remote_path(interpolate_variables(ctx.config.get("remote_path", ""), ctx.variables))
        if not remote_path:
            raise ValueError("Remote file path is required")

        # Resolve connection
        connection_id = ctx.config.get("connection_id")
        if connection_id:
            from ..connection_resolver import resolve_connection
            resolved = await resolve_connection(
                connection_id, org_id=ctx.org_id, owner_id=ctx.owner_id,
                owner_type=ctx.owner_type, environment=ctx.environment,
            )
            host = resolved.get("host", "")
            port = int(resolved.get("port", 22))
            username = resolved.get("username", "")
            password = resolved.get("password", "")
            private_key = resolved.get("private_key", "")
            protocol = resolved.get("protocol", "sftp")
        else:
            host = ctx.config.get("host", "")
            port = int(ctx.config.get("port", 22))
            username = ctx.config.get("username", "")
            password = ctx.config.get("password", "")
            private_key = ctx.config.get("private_key", "")
            protocol = ctx.config.get("protocol", "sftp")

        if not host:
            raise ValueError("An SFTP/FTP host is required — select a saved connection or provide host details")

        # Serialize data
        data_bytes = await self._serialize(ctx)
        create_dirs = ctx.config.get("create_dirs", True)

        if protocol == "sftp":
            await self._upload_sftp(host, port, username, password, private_key,
                                    remote_path, data_bytes, create_dirs)
        else:
            await self._upload_ftp(host, port, username, password, remote_path,
                                   data_bytes, create_dirs, use_tls=(protocol == "ftps"))

        logger.info(f"SFTPWriter: uploaded {len(data_bytes)} bytes to {protocol}://{host}{remote_path}")
        return self._make_output(items=[], remote_path=remote_path, size_bytes=len(data_bytes), protocol=protocol, host=host)

    async def _serialize(self, ctx: NodeContext) -> bytes:
        import asyncio
        file_format = ctx.config.get("file_format", "json")
        data = ctx.input_data
        items = ctx.items

        if file_format == "csv":
            def _to_csv():
                import pandas as pd
                import io
                records = items if items else ([data] if isinstance(data, dict) else data)
                if not isinstance(records, list):
                    records = [records]
                df = pd.DataFrame(records)
                buf = io.BytesIO()
                df.to_csv(buf, index=False)
                return buf.getvalue()
            return await asyncio.get_event_loop().run_in_executor(None, _to_csv)

        elif file_format == "excel":
            def _to_excel():
                import pandas as pd
                import io
                records = items if items else ([data] if isinstance(data, dict) else data)
                if not isinstance(records, list):
                    records = [records]
                df = pd.DataFrame(records)
                buf = io.BytesIO()
                df.to_excel(buf, index=False, engine="openpyxl")
                return buf.getvalue()
            return await asyncio.get_event_loop().run_in_executor(None, _to_excel)

        elif file_format == "text":
            if isinstance(data, str):
                return data.encode("utf-8")
            content = data.get("content", data.get("text", ""))
            if content:
                return str(content).encode("utf-8")
            return json.dumps(data, indent=2, default=str).encode("utf-8")

        else:  # json
            return json.dumps(data, indent=2, default=str).encode("utf-8")

    async def _upload_sftp(self, host, port, username, password, private_key,
                           remote_path, data: bytes, create_dirs: bool):
        import asyncio
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
                if create_dirs:
                    _sftp_makedirs(sftp, os.path.dirname(remote_path))
                sftp.putfo(io.BytesIO(data), remote_path)
                sftp.close()
            finally:
                transport.close()

        await asyncio.get_event_loop().run_in_executor(None, _do)

    async def _upload_ftp(self, host, port, username, password, remote_path,
                          data: bytes, create_dirs: bool, use_tls=False):
        import asyncio
        import io

        def _do():
            if use_tls:
                from ftplib import FTP_TLS
                ftp = FTP_TLS()
            else:
                from ftplib import FTP
                ftp = FTP()
            ftp.connect(host, port, timeout=FTP_CONNECT_TIMEOUT)
            ftp.login(username or 'anonymous', password or '')
            if use_tls:
                ftp.prot_p()
            if create_dirs:
                _ftp_makedirs(ftp, os.path.dirname(remote_path))
            ftp.storbinary(f'STOR {remote_path}', io.BytesIO(data))
            ftp.quit()

        await asyncio.get_event_loop().run_in_executor(None, _do)


def _sftp_makedirs(sftp, remote_dir):
    """Recursively create directories on SFTP server."""
    import stat
    if not remote_dir or remote_dir == '/':
        return
    try:
        sftp.stat(remote_dir)
    except FileNotFoundError:
        parent = os.path.dirname(remote_dir)
        _sftp_makedirs(sftp, parent)
        sftp.mkdir(remote_dir)


def _ftp_makedirs(ftp, remote_dir):
    """Recursively create directories on FTP server."""
    if not remote_dir or remote_dir == '/':
        return
    parts = remote_dir.strip('/').split('/')
    current = ''
    for part in parts:
        current += f'/{part}'
        try:
            ftp.cwd(current)
        except Exception:
            ftp.mkd(current)
    ftp.cwd('/')


@register_node
class WebhookOutputNode(BaseNode):
    """POST workflow output to an external URL.

    Accepts an arbitrary URL + headers, so whoever configures it needs to
    know the destination is trustworthy. For a pre-configured channel
    instead, use email_sender or notify.
    """

    node_type = NodeType.WEBHOOK_OUTPUT
    category = NodeCategory.OUTPUT
    label = "Webhook Output"
    description = (
        "POST results to an external webhook URL. Takes a raw URL and headers — "
        "for a pre-configured channel use email_sender or notify instead."
    )
    icon = "🔗"
    color = "#f59e0b"

    @classmethod
    def get_fields(cls) -> List[NodeFieldSchema]:
        return [
            NodeFieldSchema(name="url", label="Webhook URL", type="text", required=True,
                            placeholder="https://hooks.example.com/workflow-results"),
            NodeFieldSchema(name="headers", label="Headers (JSON)", type="json", default={}),
        ]

    async def execute(self, ctx: NodeContext) -> Any:
        import httpx
        from ..utils.ssrf import assert_url_is_public

        url = ctx.config["url"]
        # SSRF guard — resolves the host and blocks private / loopback /
        # link-local (incl. cloud metadata) / reserved addresses, in any
        # encoding. Replaces the old string-prefix blocklist.
        assert_url_is_public(url)

        headers = ctx.config.get("headers", {}) or {}
        # Serialize with default=str so datetime / Decimal / other non-JSON-native
        # values (common in SQL rows, e.g. created_at) don't blow up the POST with
        # "Object of type datetime is not JSON serializable". httpx's json= uses the
        # strict stdlib encoder; encode ourselves and send as content instead.
        body = json.dumps(ctx.input_data, default=str).encode("utf-8")
        send_headers = {"Content-Type": "application/json", **headers}
        async with httpx.AsyncClient(
            timeout=HTTP_TIMEOUT_WEBHOOK_OUTPUT, follow_redirects=False,
        ) as client:
            resp = await client.post(url, content=body, headers=send_headers)
        return self._make_output(items=[], status_code=resp.status_code, url=url)


# ============================================================================
# Helper
# ============================================================================

# Internal fields that should not appear in user-facing exports (CSV, Excel, PDF)
# _rank  → exported as "Rank" (user-visible)
# _status → exported as "Status" (user-visible)
# _rule_reason → exported as "Reason" (user-visible)
# Only truly debug-internal fields are hidden
_INTERNAL_FIELDS = {"_rule_passed", "_score"}

# Display-name mapping for export columns
_EXPORT_COLUMN_MAP = {
    "full name": "Candidate Name",
    "salary expectation": "Salary (LPA)",
    "evaluation info": "Score",
    "_status": "Status",
    "_rank": "Rank",
    "_rule_reason": "Reason",
}


def _flatten_export_value(val):
    """Flatten a nested value into a CSV/Excel-friendly string."""
    if val is None:
        return ""
    if isinstance(val, bool):
        return str(val)
    if isinstance(val, (int, float)):
        return val
    if isinstance(val, list):
        if not val:
            return ""
        if isinstance(val[0], dict):
            parts = []
            for d in val:
                if "title" in d and "company" in d:
                    parts.append(f"{d['title']} at {d['company']} ({d.get('duration', 'N/A')})")
                elif "degree" in d and "institution" in d:
                    parts.append(f"{d['degree']} — {d['institution']} ({d.get('year', 'N/A')})")
                else:
                    parts.append("; ".join(f"{k}: {v}" for k, v in d.items()))
            return " | ".join(parts)
        return ", ".join(str(v) for v in val)
    if isinstance(val, dict):
        score = val.get("score")
        if score is not None:
            return score
        return "; ".join(f"{k}: {v}" for k, v in val.items())
    return str(val)


def _flatten_records_for_export(records: list) -> list:
    """Prepare records for CSV/Excel export: flatten nested values, hide internal fields, rename columns."""
    if not records:
        return []
    clean = []
    for rec in records:
        row = {}
        for key, val in rec.items():
            if key in _INTERNAL_FIELDS:
                continue  # Hide internal fields
            display_key = _EXPORT_COLUMN_MAP.get(key, key)
            row[display_key] = _flatten_export_value(val)
        clean.append(row)
    return clean


def _build_table_html(title: str, records: list, extra_data: Any = None) -> str:
    """Build a recruiter-friendly HTML table for PDF rendering."""
    import html as _html

    safe_title = _html.escape(str(title))

    if not records:
        summary = _html.escape(json.dumps(extra_data, indent=2, default=str)) if extra_data else "No data"
        return f"<html><body><h1>{safe_title}</h1><pre>{summary}</pre></body></html>"

    # Internal fields that should not appear in exported PDFs
    # Only truly debug fields are hidden; _rank/_status/_rule_reason are user-visible
    _PDF_INTERNAL = {"_rule_passed", "_score"}

    # Recruiter-friendly column mapping — ordered for readability
    _PDF_FRIENDLY_COLS = [
        ("full name", "Candidate Name"),
        ("name", "Name"),
        ("email", "Email"),
        ("phone", "Phone"),
        ("location", "Location"),
        ("role", "Role"),
        ("salary expectation", "Salary (LPA)"),
        ("salary", "Salary (LPA)"),
        ("skills", "Skills"),
        ("experiences", "Experience"),
        ("educations", "Education"),
        ("evaluation info", "Score"),
        ("_score", "Score"),
        ("_status", "Status"),
        ("_rank", "Rank"),
        ("_rule_reason", "Reason"),
        ("summary", "Summary"),
    ]

    def _pick_pdf_columns(record):
        """Return (display_name, key) pairs for the PDF table."""
        picked = []
        seen_display = set()
        for key, display in _PDF_FRIENDLY_COLS:
            if key in record and display not in seen_display and key not in _PDF_INTERNAL:
                picked.append((display, key))
                seen_display.add(display)
        # Add any remaining non-internal fields not already picked
        for key in record:
            if key not in _PDF_INTERNAL and key not in {k for _, k in picked}:
                display = key.replace("_", " ").title()
                if display not in seen_display:
                    picked.append((display, key))
                    seen_display.add(display)
        return picked

    def _fmt_pdf_cell(val):
        """Format a cell value for PDF display — flatten nested structures."""
        if val is None:
            return ""
        if isinstance(val, bool):
            return "Yes" if val else "No"
        if isinstance(val, list):
            if not val:
                return ""
            if isinstance(val[0], dict):
                # List of dicts → compact multi-line
                parts = []
                for d in val:
                    # Pick the most relevant fields
                    if "title" in d and "company" in d:
                        parts.append(f"{d['title']} at {d['company']} ({d.get('duration', 'N/A')})")
                    elif "degree" in d and "institution" in d:
                        parts.append(f"{d['degree']} — {d['institution']} ({d.get('year', 'N/A')})")
                    else:
                        parts.append("; ".join(f"{k}: {v}" for k, v in d.items()))
                return " | ".join(parts)
            # List of scalars
            return ", ".join(str(v) for v in val)
        if isinstance(val, dict):
            score = val.get("score")
            if score is not None:
                return str(score)
            return "; ".join(f"{k}: {v}" for k, v in val.items())
        return str(val)

    pdf_cols = _pick_pdf_columns(records[0])

    header = "".join(
        f"<th style='padding:8px;border:1px solid #ddd;background:#f5f5f5;white-space:nowrap'>"
        f"{_html.escape(display)}</th>" for display, _k in pdf_cols
    )
    rows = ""
    if len(records) > MAX_TABLE_RECORDS:
        raise ValueError(
            f"Table records count ({len(records)}) exceeds limit ({MAX_TABLE_RECORDS}). "
            f"Add a filter node upstream to reduce records, "
            f"or set WF_MAX_TABLE_RECORDS to increase the limit."
        )
    for rec in records:
        cells = "".join(
            f"<td style='padding:8px;border:1px solid #ddd;max-width:250px;word-wrap:break-word'>"
            f"{_html.escape(_fmt_pdf_cell(rec.get(k, '')))}</td>" for _display, k in pdf_cols
        )
        rows += f"<tr>{cells}</tr>"

    return f"""<html><head><style>
        body {{ font-family: Arial, sans-serif; margin: 40px; }}
        table {{ border-collapse: collapse; width: 100%; font-size: 12px; }}
        th {{ text-align: left; }}
    </style></head><body>
    <h1>{safe_title}</h1>
    <p>Total records: {len(records)}</p>
    <table><thead><tr>{header}</tr></thead><tbody>{rows}</tbody></table>
    </body></html>"""


# ---------------------------------------------------------------------------
# REMOVED 2026-08-08 — Citra platform coupling (see PORTING.md §1, §2, §3).
#
# Everything from here to EOF was Citra-specific: the Milvus collection helpers
# (`mcp_<dept>_<source_id>` naming), VectorSinkNode, StructuredSchemaSinkNode,
# CatalogueSinkNode, the agent few-shot sample nodes, and DeptMcpActionNode.
#
# They wrote into a Citra Decision System's vector store and called a Citra
# dept-MCP's /execute_action. Neither exists here.
#
# Replacements live in nodes/vector.py and nodes/mcp.py: a vector store you
# point at any database, and an MCP client you point at any server.
# ---------------------------------------------------------------------------
