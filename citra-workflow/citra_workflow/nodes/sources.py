# Copyright (c) 2026 Trustedwear Tech Private Limited (https://citra-ai.com)
# Author: Rohit Kumar Chandan
# SPDX-License-Identifier: BUSL-1.1
#
# Licensed under the Business Source License 1.1. Non-production use is granted;
# production use requires a commercial licence until the Change Date, after
# which this file converts to Apache-2.0. See LICENSE at the repository root.

"""Data source nodes — read data from databases, files, and APIs."""

from __future__ import annotations
import logging
from typing import Any, Dict, List, Optional

from ..models import NodeType, NodeCategory, NodeFieldSchema
from ..utils.objectstore import require_bucket
from . import BaseNode, NodeContext, register_node, interpolate_variables, sql_parameterize, sanitize_remote_path
from ..config import (
    DEFAULT_SQL_MAX_ROWS, DEFAULT_NOSQL_LIMIT,
    MONGO_CONNECT_TIMEOUT_MS, HTTP_TIMEOUT_API_SOURCE, FTP_CONNECT_TIMEOUT,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Architecture: Centralized file parsing
# ---------------------------------------------------------------------------
# ALL file-reading source nodes (S3SourceNode, SFTPSourceNode, CSVSourceNode)
# must call ``_parse_file_by_type`` — it is the ONLY parsing entry point.
#
# FTP and FTPS single-file reads are handled by SFTPSourceNode via its
# ``protocol`` config field ("sftp" | "ftp" | "ftps").  No separate FTP
# source node exists or is needed.
#
# ``_EXT_TO_TYPE`` below is the ONLY extension → file-type mapping.
# Folder-listing nodes (FTPFolderSourceNode, SFTPFolderSourceNode,
# S3FolderSourceNode) list directories / prefixes and optionally download
# + parse each file via the centralized parser.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Extension → file-type mapping  (shared by all file-reading source nodes)
# ---------------------------------------------------------------------------

_EXT_TO_TYPE: Dict[str, str] = {
    # Plain text variants
    "txt": "text", "md": "text", "html": "text", "xml": "text",
    "log": "text", "rst": "text", "yaml": "text", "yml": "text",
    # Structured / tabular
    "csv": "csv", "tsv": "csv",
    "xlsx": "excel", "xls": "excel",
    "json": "json",
    # Documents
    "pdf": "pdf",
    "docx": "word", "doc": "word",
    # Images  (OCR via Qwen)
    "jpg": "image", "jpeg": "image", "png": "image", "webp": "image", "gif": "image",
    # Audio  (transcription via Whisper)
    "mp3": "audio", "wav": "audio", "m4a": "audio", "ogg": "audio",
    "flac": "audio", "webm": "audio",
}

_AUDIO_MIME: Dict[str, str] = {
    "mp3": "audio/mpeg", "wav": "audio/wav", "m4a": "audio/mp4",
    "ogg": "audio/ogg", "flac": "audio/flac", "webm": "audio/webm",
}

_IMAGE_MIME: Dict[str, str] = {
    "jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
    "webp": "image/webp", "gif": "image/gif",
}


async def _parse_file_by_type(
    raw: bytes,
    file_type: str,
    filename: str,
    ctx: NodeContext,
    source_tag: str,
) -> Dict[str, Any]:
    """Shared file parser used by all file-fetching source nodes.

    Dispatches on *file_type* and returns the universal output envelope
    produced by ``BaseNode._make_output``.  All CPU-bound work runs in a
    thread executor so the async event loop is never blocked.

    Supported types
    ---------------
    pdf   — PyMuPDF (fitz) → PyPDF2 → pdfplumber fallback chain
    image — Qwen OCR via qwen_ocr_proxy.extract_text_from_image
    audio — Whisper Turbo via whisper_proxy.transcribe_audio
    excel — pandas read_excel
    csv   — pandas read_csv  (handles TSV via sep=tab)
    word  — python-docx → docx2txt fallback
    json  — json.loads
    text  — UTF-8 decode
    binary / unknown — base64 fallback with a warning
    """
    import asyncio
    import base64
    import io
    import os as _os

    ext = _os.path.splitext(filename)[1].lstrip(".").lower() if filename else ""

    def _out(items, **meta):
        return BaseNode._make_output(items, **meta)

    # ── PDF ──────────────────────────────────────────────────────────────
    if file_type == "pdf":
        extract_mode = ctx.config.get("extract_mode", "full_text")

        def _parse_pdf():
            # Primary: PyMuPDF (fitz)
            try:
                import fitz  # PyMuPDF
                doc = fitz.open(stream=raw, filetype="pdf")
                pages = []
                for i, page in enumerate(doc):
                    text = page.get_text("text") or ""
                    pages.append({"page": i + 1, "text": text.strip()})
                doc.close()
                return pages
            except ImportError:
                pass
            # Fallback: PyPDF2
            try:
                import PyPDF2
                reader = PyPDF2.PdfReader(io.BytesIO(raw))
                pages = []
                for i, page in enumerate(reader.pages):
                    text = page.extract_text() or ""
                    pages.append({"page": i + 1, "text": text.strip()})
                return pages
            except ImportError:
                pass
            # Fallback: pdfplumber
            try:
                import pdfplumber
                with pdfplumber.open(io.BytesIO(raw)) as pdf:
                    pages = []
                    for i, page in enumerate(pdf.pages):
                        text = page.extract_text() or ""
                        pages.append({"page": i + 1, "text": text.strip()})
                    return pages
            except ImportError:
                raise ImportError(
                    "PDF extraction requires PyMuPDF, PyPDF2, or pdfplumber. "
                    "Install with: pip install pymupdf"
                )

        pages = await asyncio.get_running_loop().run_in_executor(None, _parse_pdf)
        if extract_mode == "per_page":
            return _out(pages, count=len(pages), source=source_tag, format="pdf_pages")
        full_text = "\n\n".join(p["text"] for p in pages if p["text"])
        return _out(
            [{"text": full_text, "page_count": len(pages)}],
            page_count=len(pages), source=source_tag, format="pdf",
        )

    # ── Image (Qwen OCR) ─────────────────────────────────────────────────
    if file_type == "image":
        mime_type = _IMAGE_MIME.get(ext, "image/png")

        def _ocr():
            from qwen_ocr_proxy import extract_text_from_image
            return extract_text_from_image(
                image_bytes=raw,
                filename=filename or f"image.{ext or 'png'}",
                mime_type=mime_type,
                user_id=ctx.user_id,
            )

        text = await asyncio.get_running_loop().run_in_executor(None, _ocr)
        logger.info("File parser: OCR extracted %d chars from %s", len(text or ""), filename)
        return _out(
            [{"text": text or "", "filename": filename or "", "format": "image"}],
            source=source_tag, format="image",
        )

    # ── Audio (Whisper) ──────────────────────────────────────────────────
    if file_type == "audio":
        mime_type = _AUDIO_MIME.get(ext, "audio/mpeg")

        def _transcribe():
            from whisper_proxy import transcribe_audio
            return transcribe_audio(
                audio_bytes=raw,
                mime_type=mime_type,
                filename=filename or f"audio.{ext or 'mp3'}",
                user_id=ctx.user_id,
            )

        text = await asyncio.get_running_loop().run_in_executor(None, _transcribe)
        logger.info("File parser: Whisper transcribed %d chars from %s", len(text or ""), filename)
        return _out(
            [{"text": text or "", "filename": filename or "", "format": "audio"}],
            source=source_tag, format="audio",
        )

    # ── Excel ────────────────────────────────────────────────────────────
    if file_type == "excel":
        sheet = ctx.config.get("sheet_name") or None

        def _parse_excel():
            import pandas as pd
            kwargs = {"sheet_name": sheet} if sheet else {}
            df = pd.read_excel(io.BytesIO(raw), **kwargs)
            return df.to_dict(orient="records")

        records = await asyncio.get_running_loop().run_in_executor(None, _parse_excel)
        return _out(records, count=len(records), source=source_tag, format="excel")

    # ── CSV / TSV ────────────────────────────────────────────────────────
    if file_type == "csv":
        def _parse_csv():
            import pandas as pd
            sep = "\t" if ext == "tsv" else ","
            df = pd.read_csv(io.BytesIO(raw), sep=sep)
            return df.to_dict(orient="records")

        records = await asyncio.get_running_loop().run_in_executor(None, _parse_csv)
        return _out(records, count=len(records), source=source_tag, format="csv")

    # ── Word (docx / doc) ────────────────────────────────────────────────
    if file_type == "word":
        def _parse_word():
            # Primary: python-docx (works for .docx)
            try:
                from docx import Document
                doc = Document(io.BytesIO(raw))
                paragraphs = [p.text for p in doc.paragraphs if p.text.strip()]
                return "\n\n".join(paragraphs), len(paragraphs)
            except Exception:
                pass
            # Fallback: docx2txt (alternative .docx extraction)
            try:
                import docx2txt
                import tempfile
                import os as _os2
                suffix = f".{ext}" if ext else ".docx"
                with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
                    tmp.write(raw)
                    tmp_path = tmp.name
                try:
                    extracted = docx2txt.process(tmp_path)
                finally:
                    _os2.unlink(tmp_path)
                paragraphs = [p for p in extracted.splitlines() if p.strip()]
                return extracted, len(paragraphs)
            except ImportError:
                raise ImportError(
                    "Word document extraction requires python-docx or docx2txt. "
                    "Install with: pip install python-docx"
                )

        word_text, para_count = await asyncio.get_running_loop().run_in_executor(None, _parse_word)
        return _out(
            [{"text": word_text, "paragraph_count": para_count, "filename": filename or ""}],
            source=source_tag, format="word",
        )

    # ── JSON ─────────────────────────────────────────────────────────────
    if file_type == "json":
        import json as _json
        data = _json.loads(raw.decode("utf-8"))
        items = data if isinstance(data, list) else [data]
        return _out(items, count=len(items), source=source_tag, format="json")

    # ── Plain Text ───────────────────────────────────────────────────────
    if file_type == "text":
        content = raw.decode("utf-8", errors="replace")
        lines = content.splitlines()
        return _out(
            [{"text": content, "line_count": len(lines)}],
            source=source_tag, format="text", line_count=len(lines),
        )

    # ── Fallback: unknown/binary type → store as a blob REFERENCE ─────────
    # Binary content must NOT travel inline. Base64 in the item bloats every
    # checkpoint and the execution document, and a file over Mongo's 16 MB
    # doc limit would fail the run outright. Spill the bytes to the blob store
    # (GridFS, chunked) and emit a compact {"_blob": {...}} reference — the same
    # shape the OCR / Transcribe / Vision nodes and the download endpoint
    # consume. Heavy media thus flows by reference, reliably.
    logger.info(
        "File parser: file_type=%r for %r — stored as a binary blob (by reference)",
        file_type, filename,
    )
    from ..blob_store import put_blob
    mime = _IMAGE_MIME.get(ext) or _AUDIO_MIME.get(ext) or "application/octet-stream"
    descriptor = await put_blob(
        raw, mime=mime, filename=filename or "file",
        execution_id=getattr(ctx, "execution_id", "") or "",
    )
    return _out([descriptor], source=source_tag, format="blob",
                size=len(raw), filename=filename or "", mime=mime)


@register_node
class SQLSourceNode(BaseNode):
    node_type = NodeType.SQL_SOURCE
    category = NodeCategory.SOURCE
    label = "SQL Database"
    description = "Query SQL Server, PostgreSQL, or MySQL"
    icon = "🗄️"
    color = "#3b82f6"

    @classmethod
    def get_fields(cls) -> List[NodeFieldSchema]:
        return [
            NodeFieldSchema(name="connection_id", label="Connection", type="connection_picker",
                            connection_type="sql",
                            help_text="Select a saved SQL connection (overrides inline connection string)"),
            NodeFieldSchema(name="connection_string", label="Connection String", type="password",
                            placeholder="mssql+pyodbc://user:pass@host/db?driver=ODBC+Driver+17+for+SQL+Server",
                            help_text="SQLAlchemy connection string (used if no saved connection selected)"),
            NodeFieldSchema(name="query", label="SQL Query", type="textarea",
                            placeholder="SELECT * FROM applications WHERE id = {{applicant_id}}",
                            help_text="SELECT query. Supports {{variable}} placeholders — "
                                      "do NOT quote them, parameterized quoting is automatic."),
            NodeFieldSchema(name="max_rows", label="Max Rows", type="number", default=DEFAULT_SQL_MAX_ROWS),
        ]

    async def execute(self, ctx: NodeContext) -> Any:
        import sqlalchemy
        from sqlalchemy import text
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

        query_template = ctx.config.get("query")
        if not query_template:
            raise ValueError("'SQL Query' is required")
        parameterized_query, bind_params = sql_parameterize(query_template, ctx.variables)
        max_rows = int(ctx.config.get("max_rows", DEFAULT_SQL_MAX_ROWS))

        # SQL safety guard: only allow SELECT / WITH ... SELECT
        _stripped = parameterized_query.strip()
        # Remove SQL comments that could hide destructive keywords
        import re
        _no_comments = re.sub(r'/\*.*?\*/', ' ', _stripped, flags=re.DOTALL)
        _no_comments = re.sub(r'--[^\n]*', ' ', _no_comments)
        _upper = _no_comments.strip().upper()

        DESTRUCTIVE_KW = {"DROP", "DELETE", "TRUNCATE", "ALTER", "UPDATE", "INSERT",
                          "CREATE", "GRANT", "REVOKE", "EXEC", "EXECUTE", "MERGE"}
        # First keyword must be SELECT or WITH (for CTEs)
        first_word = _upper.split()[0] if _upper.split() else ""
        if first_word not in ("SELECT", "WITH"):
            raise ValueError("Only SELECT queries are allowed in source nodes")
        # Also scan entire query for destructive keywords as standalone words
        for kw in DESTRUCTIVE_KW:
            if re.search(rf'\b{kw}\b', _upper):
                raise ValueError(f"Destructive SQL keyword '{kw}' is not allowed in source nodes")

        def _run():
            engine = sqlalchemy.create_engine(conn_str, pool_pre_ping=True)
            with engine.connect() as conn:
                result = conn.execute(text(parameterized_query), bind_params)
                cols = list(result.keys())
                rows = [dict(zip(cols, row)) for row in result.fetchmany(max_rows)]
            engine.dispose()
            return rows

        rows = await asyncio.get_running_loop().run_in_executor(None, _run)
        logger.info(f"SQLSource: fetched {len(rows)} rows")
        return self._make_output(items=rows, count=len(rows), source="sql")


@register_node
class MongoSourceNode(BaseNode):
    node_type = NodeType.MONGO_SOURCE
    category = NodeCategory.SOURCE
    label = "NoSQL Database"
    description = "Query a NoSQL database collection (MongoDB, DocumentDB, CosmosDB, etc.)"
    icon = "🗃️"
    color = "#3b82f6"

    @classmethod
    def get_fields(cls) -> List[NodeFieldSchema]:
        return [
            NodeFieldSchema(name="connection_id", label="Connection", type="connection_picker",
                            connection_type="mongo",
                            help_text="Select a saved NoSQL connection"),
            NodeFieldSchema(name="connection_string", label="Connection String", type="password",
                            placeholder="mongodb+srv://user:pass@cluster/db",
                            help_text="Connection URI (used if no saved connection selected)"),
            NodeFieldSchema(name="database", label="Database Name", type="text", required=True,
                            placeholder="mydb"),
            NodeFieldSchema(name="collection", label="Collection Name", type="text", required=True,
                            placeholder="applications"),
            NodeFieldSchema(name="filter", label="Filter (JSON)", type="json", default={},
                            placeholder='{"status": "new"}',
                            help_text="String values support {{variable}} placeholders from trigger data"),
            NodeFieldSchema(name="projection", label="Projection (JSON)", type="json", default=None,
                            placeholder='{"name": 1, "status": 1}'),
            NodeFieldSchema(name="limit", label="Limit", type="number", default=DEFAULT_NOSQL_LIMIT),
        ]

    async def execute(self, ctx: NodeContext) -> Any:
        collection_name = interpolate_variables(ctx.config["collection"], ctx.variables)
        filter_doc = ctx.config.get("filter", {}) or {}
        # Normalize filter: may arrive as JSON string from the UI or test
        if isinstance(filter_doc, str):
            import json as _json
            try:
                filter_doc = _json.loads(filter_doc)
            except (ValueError, TypeError):
                filter_doc = {}
        # Interpolate variables inside filter JSON string values
        if isinstance(filter_doc, dict):
            filter_doc = {k: interpolate_variables(v, ctx.variables) if isinstance(v, str) else v
                          for k, v in filter_doc.items()}
        projection = ctx.config.get("projection")
        limit = int(ctx.config.get("limit", DEFAULT_NOSQL_LIMIT))

        # Resolve connection: saved connection_id takes priority over inline
        connection_id = ctx.config.get("connection_id")

        if connection_id:
            from ..connection_resolver import resolve_connection
            resolved = await resolve_connection(
                connection_id, org_id=ctx.org_id, owner_id=ctx.owner_id,
                owner_type=ctx.owner_type, environment=ctx.environment,
            )
            from motor.motor_asyncio import AsyncIOMotorClient
            client = AsyncIOMotorClient(resolved["connection_string"], serverSelectionTimeoutMS=MONGO_CONNECT_TIMEOUT_MS)
            db_name = resolved.get("database") or ""
            db = client[db_name]
        else:
            from motor.motor_asyncio import AsyncIOMotorClient
            conn_str = ctx.config.get("connection_string", "")
            db_name = ctx.config.get("database", "")
            if not conn_str or not db_name:
                raise ValueError("A NoSQL connection is required — select a saved connection or provide a connection string and database name")
            client = AsyncIOMotorClient(conn_str, serverSelectionTimeoutMS=MONGO_CONNECT_TIMEOUT_MS)
            db = client[db_name]

        try:
            cursor = db[collection_name].find(filter_doc, projection).limit(limit)
            rows = []
            async for doc in cursor:
                doc["_id"] = str(doc["_id"])
                rows.append(doc)
        finally:
            client.close()

        logger.info(f"NoSQLSource: fetched {len(rows)} docs from {collection_name}")
        return self._make_output(items=rows, count=len(rows), source="mongo")


# RETIRED — reads from a PER-USER path (<env>/workflows/<user_id>/), which has no
# place in org/dept IT workflows (there is no "user" concept in a workflow). Use
# "External S3 File" for object storage instead. Deregistered so it no longer appears
# in the Workflow Builder palette or runs. Class kept for reference / easy restore;
# re-add @register_node to bring it back.
class CSVSourceNode(BaseNode):
    node_type = NodeType.CSV_SOURCE
    category = NodeCategory.SOURCE
    label = "Workflow Storage File (Internal)"
    description = "Read any file type from Citra's internal workflow storage — by key, no bucket or credentials needed (auto-detects by extension)"
    icon = "📄"
    color = "#3b82f6"
    ai_authoring_hint = (
        "Use this node_type when the user asks for 'Workflow Storage File' "
        "or workflow-storage outputs. Configure only `s3_key` plus file parsing "
        "options; the runtime automatically reads from <env>/workflows/<user_id>/."
    )

    @classmethod
    def get_fields(cls) -> List[NodeFieldSchema]:
        return [
            NodeFieldSchema(name="s3_key", label="S3 File Key", type="text", required=True,
                            placeholder="workflows/data/{{filename}}.csv",
                            help_text="Supports {{variable}} placeholders from trigger data"),
            NodeFieldSchema(name="bucket_name", label="S3 Bucket", type="text",
                            help_text="Bucket to read from. Falls back to the BUCKET_NAME env var; there is no built-in default."),
            NodeFieldSchema(name="file_type", label="File Type", type="select", default="auto",
                            options=[
                                {"label": "Auto-detect", "value": "auto"},
                                {"label": "CSV", "value": "csv"},
                                {"label": "Excel", "value": "excel"},
                                {"label": "JSON", "value": "json"},
                                {"label": "Plain Text", "value": "text"},
                                {"label": "PDF", "value": "pdf"},
                                {"label": "Word (docx/doc)", "value": "word"},
                                {"label": "Image (JPG/PNG/WebP — Qwen OCR)", "value": "image"},
                                {"label": "Audio (MP3/WAV — Whisper)", "value": "audio"},
                                {"label": "Binary (base64)", "value": "binary"},
                            ],
                            help_text="Auto-detect uses file extension"),
            NodeFieldSchema(name="extract_mode", label="Extract Mode", type="select", default="full_text",
                            options=[
                                {"label": "Full Text", "value": "full_text"},
                                {"label": "Per Page (PDF only)", "value": "per_page"},
                            ],
                            visible_when={"field": "file_type", "values": ["pdf", "auto"]}),
            NodeFieldSchema(name="sheet_name", label="Sheet Name (Excel)", type="text", default="",
                            help_text="Sheet to read when file type is Excel (leave blank for first sheet)",
                            visible_when={"field": "file_type", "values": ["excel", "auto"]}),
        ]

    async def execute(self, ctx: NodeContext) -> Any:
        import asyncio
        import os
        from bucket import get_s3_client

        s3_key = interpolate_variables(ctx.config["s3_key"], ctx.variables)
        bucket = require_bucket(ctx.config.get("bucket_name"), os.getenv("BUCKET_NAME"), node=self.__class__.__name__)
        env = os.getenv("ENVIRONMENT", "dev")
        full_key = f"{env}/workflows/{ctx.user_id}/{s3_key}"

        def _download():
            s3 = get_s3_client()
            obj = s3.get_object(Bucket=bucket, Key=full_key)
            return obj["Body"].read()

        raw = await asyncio.get_running_loop().run_in_executor(None, _download)

        file_type = ctx.config.get("file_type", "auto")
        if file_type == "auto":
            import os as _os
            ext = _os.path.splitext(s3_key)[1].lstrip(".").lower()
            file_type = _EXT_TO_TYPE.get(ext, "binary")

        filename = s3_key.rsplit("/", 1)[-1] if "/" in s3_key else s3_key
        return await _parse_file_by_type(raw, file_type, filename, ctx, "csv")


@register_node
class APISourceNode(BaseNode):
    node_type = NodeType.API_SOURCE
    category = NodeCategory.SOURCE
    label = "REST API"
    description = "Fetch data from an external REST API"
    icon = "🌐"
    color = "#3b82f6"

    @classmethod
    def get_fields(cls) -> List[NodeFieldSchema]:
        return [
            NodeFieldSchema(name="connection_id", label="Connection", type="connection_picker",
                            connection_type="api",
                            help_text="Select a saved API connection (overrides inline URL/headers)"),
            NodeFieldSchema(name="url", label="URL", type="text",
                            placeholder="https://api.example.com/users/{{user_id}}",
                            help_text="Supports {{variable}} placeholders from trigger data. Used if no saved connection selected"),
            NodeFieldSchema(name="method", label="Method", type="select", default="GET",
                            options=[{"label": "GET", "value": "GET"}, {"label": "POST", "value": "POST"}]),
            NodeFieldSchema(name="headers", label="Headers (JSON)", type="json", default={}),
            NodeFieldSchema(name="body", label="Request Body (JSON)", type="json", default=None),
            NodeFieldSchema(name="timeout", label="Timeout (seconds)", type="number", default=HTTP_TIMEOUT_API_SOURCE),
        ]

    async def execute(self, ctx: NodeContext) -> Any:
        import httpx
        import ipaddress
        import socket
        from urllib.parse import urlparse

        # Resolve connection: saved connection_id takes priority over inline
        connection_id = ctx.config.get("connection_id")
        if connection_id:
            from ..connection_resolver import resolve_connection
            resolved = await resolve_connection(
                connection_id, org_id=ctx.org_id, owner_id=ctx.owner_id,
                owner_type=ctx.owner_type, environment=ctx.environment,
            )
            url = resolved.get("url", "")
            conn_headers = resolved.get("headers", {})
        else:
            url = ctx.config.get("url", "")
            conn_headers = {}
        if not url:
            raise ValueError("An API URL is required — select a saved connection or provide an inline URL")

        # Interpolate variables in URL
        url = interpolate_variables(url, ctx.variables)

        # SSRF guard — shared validator: resolves the host and blocks
        # private / loopback / link-local (incl. cloud metadata) /
        # reserved / multicast addresses in any encoding.
        from ..utils.ssrf import assert_url_is_public
        assert_url_is_public(url)

        method = ctx.config.get("method", "GET").upper()
        # Merge: connection headers as base, inline headers override
        cfg_headers = ctx.config.get("headers", {}) or {}
        if isinstance(cfg_headers, str):
            import json as _json
            try:
                cfg_headers = _json.loads(cfg_headers)
            except (ValueError, TypeError):
                cfg_headers = {}
        headers = {**conn_headers, **cfg_headers}
        body = ctx.config.get("body")
        # Interpolate variables in body string values
        if isinstance(body, dict):
            body = {k: interpolate_variables(v, ctx.variables) if isinstance(v, str) else v
                    for k, v in body.items()}
        timeout = int(ctx.config.get("timeout", HTTP_TIMEOUT_API_SOURCE))

        async with httpx.AsyncClient(timeout=timeout) as client:
            if method == "POST":
                resp = await client.post(url, json=body, headers=headers)
            else:
                resp = await client.get(url, headers=headers)
            resp.raise_for_status()
            data = resp.json()

        items = data if isinstance(data, list) else [data]
        return self._make_output(items=items, status_code=resp.status_code, source="api")


@register_node
class S3SourceNode(BaseNode):
    node_type = NodeType.S3_SOURCE
    category = NodeCategory.SOURCE
    label = "External S3 File"
    description = "Read a file from your own external S3 bucket — you supply the bucket + credentials (supports all common file types)"
    icon = "☁️"
    color = "#3b82f6"
    ai_authoring_hint = (
        "Use this for external S3 / object-storage files — the user supplies the "
        "bucket and credentials (via a saved connection)."
    )

    @classmethod
    def get_fields(cls) -> List[NodeFieldSchema]:
        return [
            NodeFieldSchema(name="connection_id", label="Connection", type="connection_picker",
                            connection_type="s3",
                            help_text="Select a saved S3 connection (overrides inline credentials)"),
            NodeFieldSchema(name="s3_key", label="S3 Key", type="text", required=True,
                            placeholder="documents/{{applicant_id}}/report.pdf",
                            help_text="Supports {{variable}} placeholders from trigger data"),
            NodeFieldSchema(name="bucket_name", label="Bucket Name (override)", type="text",
                            help_text="Leave blank to use the default BUCKET_NAME env var"),
            NodeFieldSchema(name="aws_access_key_id", label="AWS Access Key ID (override)", type="text",
                            help_text="Leave blank to use the default AWS_S3_ACCESS_KEY_ID env var"),
            NodeFieldSchema(name="aws_secret_access_key", label="AWS Secret Access Key (override)", type="password",
                            help_text="Leave blank to use the default AWS_S3_SECRET_ACCESS_KEY env var"),
            NodeFieldSchema(name="aws_region", label="AWS Region (override)", type="text",
                            help_text="Leave blank to use the default AWS_S3_REGION env var"),
            NodeFieldSchema(name="use_env_prefix", label="Prepend environment prefix", type="boolean", default=True,
                            help_text="When enabled, automatically prepends 'dev/', 'prod/' etc. to the key. Disable when reading from external buckets."),
            NodeFieldSchema(name="file_type", label="File Type", type="select", default="auto",
                            options=[
                                {"label": "Auto-detect", "value": "auto"},
                                {"label": "Plain Text", "value": "text"},
                                {"label": "PDF", "value": "pdf"},
                                {"label": "Excel", "value": "excel"},
                                {"label": "CSV", "value": "csv"},
                                {"label": "JSON", "value": "json"},
                                {"label": "Word (docx/doc)", "value": "word"},
                                {"label": "Image (JPG/PNG/WebP — Qwen OCR)", "value": "image"},
                                {"label": "Audio (MP3/WAV — Whisper)", "value": "audio"},
                                {"label": "Binary (base64)", "value": "binary"},
                            ],
                            help_text="Auto-detect uses file extension"),
            NodeFieldSchema(name="extract_mode", label="Extract Mode", type="select", default="full_text",
                            options=[
                                {"label": "Full Text", "value": "full_text"},
                                {"label": "Per Page (PDF only)", "value": "per_page"},
                            ],
                            visible_when={"field": "file_type", "values": ["pdf", "auto"]}),
            NodeFieldSchema(name="sheet_name", label="Sheet Name (Excel)", type="text", default="",
                            help_text="Sheet to read when file type is Excel (leave blank for first sheet)",
                            visible_when={"field": "file_type", "values": ["excel", "auto"]}),
        ]

    async def execute(self, ctx: NodeContext) -> Any:
        import asyncio, os
        from botocore.client import Config as BotoConfig

        # ── Resolve bucket, credentials, key ──────────────────────────────
        import boto3
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

        def _build_client():
            kwargs = {}
            if endpoint_url:
                kwargs["endpoint_url"] = endpoint_url
            return boto3.client(
                's3',
                aws_access_key_id=access_key,
                aws_secret_access_key=secret_key,
                region_name=region,
                config=BotoConfig(signature_version='s3v4'),
                **kwargs
            )

        env = os.getenv("ENVIRONMENT", "dev")
        use_env_prefix = ctx.config.get("use_env_prefix", True)
        s3_key = sanitize_remote_path(interpolate_variables(ctx.config["s3_key"], ctx.variables))
        key = f"{env}/{s3_key}" if use_env_prefix else s3_key

        def _read():
            s3 = _build_client()
            obj = s3.get_object(Bucket=bucket, Key=key)
            return obj["Body"].read()

        raw = await asyncio.get_running_loop().run_in_executor(None, _read)

        file_type = ctx.config.get("file_type", "auto")

        # Auto-detect from extension
        if file_type == "auto":
            import os as _os
            ext = _os.path.splitext(s3_key)[1].lstrip(".").lower()
            file_type = _EXT_TO_TYPE.get(ext, "binary")

        filename = s3_key.rsplit("/", 1)[-1] if "/" in s3_key else s3_key
        return await _parse_file_by_type(raw, file_type, filename, ctx, "s3")


@register_node
class SFTPSourceNode(BaseNode):
    node_type = NodeType.SFTP_SOURCE
    category = NodeCategory.SOURCE
    label = "SFTP / FTP File"
    description = "Read files from an SFTP or FTP server"
    icon = "📁"
    color = "#3b82f6"

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
            NodeFieldSchema(name="username", label="Username", type="text",
                            placeholder="myuser"),
            NodeFieldSchema(name="password", label="Password", type="password",
                            placeholder="(leave blank for key-based auth)"),
            NodeFieldSchema(name="private_key", label="Private Key (PEM)", type="textarea",
                            help_text="Paste SSH private key for key-based authentication (SFTP only)"),
            NodeFieldSchema(name="protocol", label="Protocol", type="select", default="sftp",
                            options=[{"label": "SFTP (SSH)", "value": "sftp"},
                                     {"label": "FTP", "value": "ftp"},
                                     {"label": "FTPS (FTP over TLS)", "value": "ftps"}]),
            NodeFieldSchema(name="remote_path", label="Remote File Path", type="text", required=True,
                            placeholder="/data/pitchdecks/{{applicant_id}}.pdf",
                            help_text="Supports {{variable}} placeholders from trigger data. Absolute path to the file on the server"),
            NodeFieldSchema(name="file_type", label="File Type", type="select", default="auto",
                            options=[
                                {"label": "Auto-detect", "value": "auto"},
                                {"label": "Plain Text", "value": "text"},
                                {"label": "PDF", "value": "pdf"},
                                {"label": "CSV", "value": "csv"},
                                {"label": "Excel", "value": "excel"},
                                {"label": "JSON", "value": "json"},
                                {"label": "Word (docx/doc)", "value": "word"},
                                {"label": "Image (JPG/PNG/WebP — Qwen OCR)", "value": "image"},
                                {"label": "Audio (MP3/WAV — Whisper)", "value": "audio"},
                                {"label": "Binary (base64)", "value": "binary"},
                            ],
                            help_text="Auto-detect uses file extension"),
            NodeFieldSchema(name="extract_mode", label="Extract Mode", type="select", default="full_text",
                            options=[
                                {"label": "Full Text", "value": "full_text"},
                                {"label": "Per Page (PDF only)", "value": "per_page"},
                            ],
                            visible_when={"field": "file_type", "values": ["pdf", "auto"]}),
            NodeFieldSchema(name="sheet_name", label="Sheet Name (Excel)", type="text", default="",
                            help_text="Sheet to read when file type is Excel (leave blank for first sheet)",
                            visible_when={"field": "file_type", "values": ["excel", "auto"]}),
        ]

    async def execute(self, ctx: NodeContext) -> Any:
        import asyncio
        import os

        remote_path = sanitize_remote_path(interpolate_variables(ctx.config.get("remote_path", ""), ctx.variables))
        if not remote_path:
            raise ValueError("Remote file path is required")

        # Resolve connection: saved connection_id takes priority
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

        if protocol == "sftp":
            raw = await self._download_sftp(host, port, username, password, private_key, remote_path)
        else:
            raw = await self._download_ftp(host, port, username, password, remote_path, use_tls=(protocol == "ftps"))

        # Determine file type
        file_type = ctx.config.get("file_type", "auto")
        if file_type == "auto":
            ext = os.path.splitext(remote_path)[1].lstrip(".").lower()
            file_type = _EXT_TO_TYPE.get(ext, "binary")

        filename = os.path.basename(remote_path)
        return await _parse_file_by_type(raw, file_type, filename, ctx, protocol)

    async def _download_sftp(self, host, port, username, password, private_key, remote_path) -> bytes:
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
                buf = io.BytesIO()
                sftp.getfo(remote_path, buf)
                sftp.close()
                return buf.getvalue()
            finally:
                transport.close()

        return await asyncio.get_running_loop().run_in_executor(None, _do)

    async def _download_ftp(self, host, port, username, password, remote_path, use_tls=False) -> bytes:
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
            buf = io.BytesIO()
            ftp.retrbinary(f'RETR {remote_path}', buf.write)
            ftp.quit()
            return buf.getvalue()

        return await asyncio.get_running_loop().run_in_executor(None, _do)


# ---------------------------------------------------------------------------
# Folder Source Nodes — list files from a remote directory
# ---------------------------------------------------------------------------

@register_node
class FTPFolderSourceNode(BaseNode):
    """List files from an FTP directory, with optional extension filter."""
    node_type = NodeType.FTP_FOLDER_SOURCE
    category = NodeCategory.SOURCE
    label = "FTP Folder Reader"
    description = "List files in an FTP directory (filter by extension)"
    icon = "📂"
    color = "#f59e0b"
    ai_authoring_hint = (
        "Set `connection_id` to the saved FTP connection and `remote_dir` to the "
        "folder path. When `read_files` is true (default) this node ALREADY downloads "
        "and parses every file via the centralized parser — images are auto-OCR'd, "
        "PDFs/JSON/CSV/Word parsed — so downstream items carry the extracted text/fields. "
        "Do NOT add a separate OCR / Transcribe / Vision node after it. Leave `extensions` "
        "blank to read all files unless the user names specific types."
    )

    @classmethod
    def get_fields(cls) -> List[NodeFieldSchema]:
        return [
            NodeFieldSchema(name="connection_id", label="Connection", type="connection_picker",
                            connection_type="sftp",
                            help_text="Select a saved FTP connection (overrides inline fields)"),
            NodeFieldSchema(name="host", label="Host", type="text", placeholder="ftp.example.com"),
            NodeFieldSchema(name="port", label="Port", type="number", default=21),
            NodeFieldSchema(name="username", label="Username", type="text"),
            NodeFieldSchema(name="password", label="Password", type="password"),
            NodeFieldSchema(name="protocol", label="Protocol", type="select", default="ftp",
                            options=[{"label": "FTP", "value": "ftp"},
                                     {"label": "FTPS (TLS)", "value": "ftps"}]),
            NodeFieldSchema(name="remote_dir", label="Remote Directory", type="text", required=True,
                            placeholder="/data/candidates",
                            help_text="Absolute path to the directory on the FTP server"),
            NodeFieldSchema(name="extensions", label="File Extensions", type="text", default=".json",
                            help_text="Comma-separated extensions to include, e.g. .json,.csv  (blank = all)"),
            NodeFieldSchema(name="read_files", label="Read & Parse Files", type="boolean", default=True,
                            help_text="When enabled, downloads each file and parses its content using the centralized file parser. Disable to only list file paths."),
        ]

    async def execute(self, ctx: NodeContext) -> Any:
        import asyncio

        remote_dir = sanitize_remote_path(
            interpolate_variables(ctx.config.get("remote_dir", ""), ctx.variables)
        )
        if not remote_dir:
            raise ValueError("Remote directory path is required")

        connection_id = ctx.config.get("connection_id")
        if connection_id:
            from ..connection_resolver import resolve_connection
            resolved = await resolve_connection(
                connection_id, org_id=ctx.org_id, owner_id=ctx.owner_id,
                owner_type=ctx.owner_type, environment=ctx.environment,
            )
            host = resolved.get("host", "")
            port = int(resolved.get("port", 21))
            username = resolved.get("username", "")
            password = resolved.get("password", "")
            protocol = resolved.get("protocol", "ftp")
        else:
            host = ctx.config.get("host", "")
            port = int(ctx.config.get("port", 21))
            username = ctx.config.get("username", "")
            password = ctx.config.get("password", "")
            protocol = ctx.config.get("protocol", "ftp")

        if not host:
            raise ValueError("An FTP host is required")

        ext_raw = ctx.config.get("extensions", "").strip()
        allowed_exts = [e.strip().lower().lstrip(".") for e in ext_raw.split(",") if e.strip()] if ext_raw else []

        def _list_ftp():
            import os
            if protocol == "ftps":
                from ftplib import FTP_TLS
                ftp = FTP_TLS()
            else:
                from ftplib import FTP
                ftp = FTP()
            ftp.connect(host, port, timeout=FTP_CONNECT_TIMEOUT)
            ftp.login(username or "anonymous", password or "")
            if protocol == "ftps":
                ftp.prot_p()
            entries = ftp.nlst(remote_dir)
            ftp.quit()
            results = []
            for entry in entries:
                basename = os.path.basename(entry)
                if basename in (".", ".."):
                    continue
                if allowed_exts:
                    ext = os.path.splitext(basename)[1].lstrip(".").lower()
                    if ext not in allowed_exts:
                        continue
                full_path = entry if entry.startswith("/") else f"{remote_dir.rstrip('/')}/{basename}"
                results.append({"file_path": full_path, "file_name": basename})
            return results

        files = await asyncio.get_running_loop().run_in_executor(None, _list_ftp)
        logger.info("FTPFolderSource: listed %d files from %s", len(files), remote_dir)

        read_files = ctx.config.get("read_files", True)
        if not read_files or not files:
            return self._make_output(items=files, count=len(files), source="ftp_folder", directory=remote_dir)

        # Download and parse each file using the centralized parser
        import os as _os
        import io

        def _download_ftp_file(file_path):
            if protocol == "ftps":
                from ftplib import FTP_TLS
                ftp = FTP_TLS()
            else:
                from ftplib import FTP
                ftp = FTP()
            ftp.connect(host, port, timeout=FTP_CONNECT_TIMEOUT)
            ftp.login(username or "anonymous", password or "")
            if protocol == "ftps":
                ftp.prot_p()
            buf = io.BytesIO()
            ftp.retrbinary(f'RETR {file_path}', buf.write)
            ftp.quit()
            return buf.getvalue()

        all_items = []
        for f in files:
            file_path = f["file_path"]
            file_name = f["file_name"]
            try:
                raw = await asyncio.get_running_loop().run_in_executor(None, _download_ftp_file, file_path)
                ext = _os.path.splitext(file_name)[1].lstrip(".").lower()
                file_type = _EXT_TO_TYPE.get(ext, "binary")
                parsed = await _parse_file_by_type(raw, file_type, file_name, ctx, "ftp")
                parsed_items = parsed.get("items", [])
                # Tag each item with source file info
                for item in parsed_items:
                    if isinstance(item, dict):
                        item["_source_file"] = file_name
                        item["_source_path"] = file_path
                all_items.extend(parsed_items)
                logger.info("FTPFolderSource: parsed %s → %d items", file_name, len(parsed_items))
            except Exception as e:
                logger.error("FTPFolderSource: failed to read %s: %s", file_path, e)
                all_items.append({"_source_file": file_name, "_error": str(e)})

        logger.info("FTPFolderSource: read+parsed %d files → %d total items", len(files), len(all_items))
        return self._make_output(items=all_items, count=len(all_items), files_read=len(files), source="ftp_folder", directory=remote_dir)


@register_node
class SFTPFolderSourceNode(BaseNode):
    # NOTE: For single-file reads over SFTP/FTP/FTPS, use SFTPSourceNode.
    """List files from an SFTP directory, with optional extension filter."""
    node_type = NodeType.SFTP_FOLDER_SOURCE
    category = NodeCategory.SOURCE
    label = "SFTP Folder Reader"
    description = "List files in an SFTP directory (filter by extension)"
    icon = "📂"
    color = "#3b82f6"
    ai_authoring_hint = (
        "Set `connection_id` to the saved SFTP connection and `remote_dir` to the "
        "folder path. When `read_files` is true (default) this node ALREADY downloads "
        "and parses every file (images auto-OCR'd, PDFs/JSON/CSV/Word parsed), so "
        "downstream items carry the extracted text/fields. Do NOT add a separate OCR / "
        "Transcribe / Vision node after it. Leave `extensions` blank to read all files "
        "unless the user names specific types."
    )

    @classmethod
    def get_fields(cls) -> List[NodeFieldSchema]:
        return [
            NodeFieldSchema(name="connection_id", label="Connection", type="connection_picker",
                            connection_type="sftp",
                            help_text="Select a saved SFTP connection (overrides inline fields)"),
            NodeFieldSchema(name="host", label="Host", type="text", placeholder="sftp.example.com"),
            NodeFieldSchema(name="port", label="Port", type="number", default=22),
            NodeFieldSchema(name="username", label="Username", type="text"),
            NodeFieldSchema(name="password", label="Password", type="password"),
            NodeFieldSchema(name="private_key", label="Private Key (PEM)", type="textarea",
                            help_text="Paste SSH private key for key-based auth (optional)"),
            NodeFieldSchema(name="remote_dir", label="Remote Directory", type="text", required=True,
                            placeholder="/files/candidates",
                            help_text="Absolute path to the directory on the SFTP server"),
            NodeFieldSchema(name="extensions", label="File Extensions", type="text", default=".jpg,.png",
                            help_text="Comma-separated extensions to include, e.g. .jpg,.png  (blank = all)"),
            NodeFieldSchema(name="read_files", label="Read & Parse Files", type="boolean", default=True,
                            help_text="When enabled, downloads each file and parses its content using the centralized file parser. Disable to only list file paths."),
        ]

    async def execute(self, ctx: NodeContext) -> Any:
        import asyncio
        import io

        remote_dir = sanitize_remote_path(
            interpolate_variables(ctx.config.get("remote_dir", ""), ctx.variables)
        )
        if not remote_dir:
            raise ValueError("Remote directory path is required")

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
        else:
            host = ctx.config.get("host", "")
            port = int(ctx.config.get("port", 22))
            username = ctx.config.get("username", "")
            password = ctx.config.get("password", "")
            private_key = ctx.config.get("private_key", "")

        if not host:
            raise ValueError("An SFTP host is required")

        ext_raw = ctx.config.get("extensions", "").strip()
        allowed_exts = [e.strip().lower().lstrip(".") for e in ext_raw.split(",") if e.strip()] if ext_raw else []

        def _list_sftp():
            import os
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
                # Resolve the directory, tolerating an absolute path that is
                # really meant relative to the login home — common when the SFTP
                # account is NOT chrooted, so the user thinks "/candidates" but
                # the real path is "<home>/candidates". We try the path as given,
                # then the home-relative forms, and use the first that lists.
                tried = [remote_dir]
                rel = remote_dir.lstrip("/")
                if rel and rel != remote_dir:
                    try:
                        home = sftp.normalize(".")
                    except Exception:  # noqa: BLE001
                        home = ""
                    if home:
                        tried.append(f"{home.rstrip('/')}/{rel}")
                    tried.append(rel)
                entries, used_dir, last_err = None, remote_dir, None
                for cand in tried:
                    try:
                        entries = sftp.listdir(cand)
                        used_dir = cand
                        break
                    except (IOError, OSError) as exc:  # FileNotFoundError is OSError errno 2
                        last_err = exc
                        continue
                sftp.close()
                if entries is None:
                    raise last_err or FileNotFoundError(remote_dir)
            finally:
                transport.close()
            results = []
            for basename in entries:
                if basename.startswith("."):
                    continue
                if allowed_exts:
                    ext = os.path.splitext(basename)[1].lstrip(".").lower()
                    if ext not in allowed_exts:
                        continue
                full_path = f"{used_dir.rstrip('/')}/{basename}"
                results.append({"file_path": full_path, "file_name": basename})
            return results

        files = await asyncio.get_running_loop().run_in_executor(None, _list_sftp)
        logger.info("SFTPFolderSource: listed %d files from %s", len(files), remote_dir)

        read_files = ctx.config.get("read_files", True)
        if not read_files or not files:
            return self._make_output(items=files, count=len(files), source="sftp_folder", directory=remote_dir)

        # Download and parse each file using the centralized parser
        import os as _os

        def _download_sftp_file(file_path):
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
                buf = io.BytesIO()
                sftp.getfo(file_path, buf)
                sftp.close()
                return buf.getvalue()
            finally:
                transport.close()

        all_items = []
        for f in files:
            file_path = f["file_path"]
            file_name = f["file_name"]
            try:
                raw = await asyncio.get_running_loop().run_in_executor(None, _download_sftp_file, file_path)
                ext = _os.path.splitext(file_name)[1].lstrip(".").lower()
                file_type = _EXT_TO_TYPE.get(ext, "binary")
                parsed = await _parse_file_by_type(raw, file_type, file_name, ctx, "sftp")
                parsed_items = parsed.get("items", [])
                for item in parsed_items:
                    if isinstance(item, dict):
                        item["_source_file"] = file_name
                        item["_source_path"] = file_path
                all_items.extend(parsed_items)
                logger.info("SFTPFolderSource: parsed %s → %d items", file_name, len(parsed_items))
            except Exception as e:
                logger.error("SFTPFolderSource: failed to read %s: %s", file_path, e)
                all_items.append({"_source_file": file_name, "_error": str(e)})

        logger.info("SFTPFolderSource: read+parsed %d files → %d total items", len(files), len(all_items))
        return self._make_output(items=all_items, count=len(all_items), files_read=len(files), source="sftp_folder", directory=remote_dir)


# ---------------------------------------------------------------------------
# S3 Folder Source  — list and optionally read+parse files from an S3 prefix
# ---------------------------------------------------------------------------

@register_node
class S3FolderSourceNode(BaseNode):
    """List (and optionally download + parse) files under an S3 prefix."""
    node_type = NodeType.S3_FOLDER_SOURCE
    category = NodeCategory.SOURCE
    label = "S3 Folder Reader"
    description = "List files in an S3 bucket prefix, optionally reading and parsing each file"
    icon = "📂"
    color = "#f59e0b"
    ai_authoring_hint = (
        "Set `connection_id` to the saved S3 connection and `prefix` to the folder "
        "path (e.g. 'candidates/'). When `read_files` is true (default) this node "
        "ALREADY downloads and parses every object (images auto-OCR'd, PDFs/JSON/CSV/Word "
        "parsed), so downstream items carry the extracted text/fields. Do NOT add a "
        "separate OCR / Transcribe / Vision node after it. Leave `extensions` blank to "
        "read all files unless the user names specific types."
    )

    # Hard cap on downloadable file size (bytes) – avoids OOM on huge objects.
    _MAX_FILE_SIZE = 50 * 1024 * 1024  # 50 MB

    @classmethod
    def get_fields(cls) -> List[NodeFieldSchema]:
        return [
            NodeFieldSchema(name="connection_id", label="Connection", type="connection_picker",
                            connection_type="bucket",
                            help_text="Select a saved S3 connection (overrides inline fields)"),
            NodeFieldSchema(name="bucket_name", label="Bucket Name", type="text",
                            placeholder="my-bucket",
                            help_text="Leave blank to use the default BUCKET_NAME env var"),
            NodeFieldSchema(name="aws_access_key_id", label="AWS Access Key ID", type="text",
                            help_text="Leave blank to use the default AWS_S3_ACCESS_KEY_ID env var"),
            NodeFieldSchema(name="aws_secret_access_key", label="AWS Secret Access Key", type="password",
                            help_text="Leave blank to use the default AWS_S3_SECRET_ACCESS_KEY env var"),
            NodeFieldSchema(name="aws_region", label="AWS Region", type="text",
                            help_text="Leave blank to use the default AWS_S3_REGION env var"),
            NodeFieldSchema(name="prefix", label="Prefix (Folder Path)", type="text", required=True,
                            placeholder="candidates/",
                            help_text="S3 key prefix to list, e.g. 'candidates/' — supports {{variable}} placeholders"),
            NodeFieldSchema(name="use_env_prefix", label="Prepend environment prefix", type="boolean", default=False,
                            help_text="When enabled, automatically prepends 'dev/', 'prod/' etc. to the prefix"),
            NodeFieldSchema(name="extensions", label="File Extensions", type="text", default="",
                            help_text="Comma-separated extensions to include, e.g. .jpg,.png  (blank = all files)"),
            NodeFieldSchema(name="read_files", label="Read & Parse Files", type="boolean", default=True,
                            help_text="When enabled, downloads each file and parses its content. Disable to only list object keys."),
        ]

    async def execute(self, ctx: NodeContext) -> Any:
        import asyncio
        import os
        import os.path as _os_path

        # ── Resolve credentials ───────────────────────────────────────────
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

        env = os.getenv("ENVIRONMENT", "dev")
        use_env_prefix = ctx.config.get("use_env_prefix", False)
        raw_prefix = sanitize_remote_path(
            interpolate_variables(ctx.config.get("prefix", ""), ctx.variables)
        )
        # S3 keys never start with '/' — strip to avoid double-slash
        raw_prefix = raw_prefix.lstrip("/")
        # Strip bucket name from prefix if user mistakenly included it
        # (S3 keys never contain the bucket name — it's a separate namespace)
        if raw_prefix == bucket:
            raw_prefix = ""
        elif raw_prefix.startswith(f"{bucket}/"):
            raw_prefix = raw_prefix[len(bucket) + 1:]
        prefix = f"{env}/{raw_prefix}" if use_env_prefix else raw_prefix
        # Ensure prefix ends with '/' for clean listing (unless it IS empty)
        if prefix and not prefix.endswith("/"):
            prefix += "/"

        ext_raw = ctx.config.get("extensions", "").strip()
        allowed_exts = [e.strip().lower().lstrip(".") for e in ext_raw.split(",") if e.strip()] if ext_raw else []

        # ── Build S3 client ───────────────────────────────────────────────
        def _build_client():
            import boto3
            from botocore.client import Config as BotoConfig
            kwargs = {}
            if endpoint_url:
                kwargs["endpoint_url"] = endpoint_url
            return boto3.client(
                "s3",
                aws_access_key_id=access_key,
                aws_secret_access_key=secret_key,
                region_name=region,
                config=BotoConfig(signature_version="s3v4"),
                **kwargs
            )

        # ── List objects (paginated) ──────────────────────────────────────
        def _list_objects():
            s3 = _build_client()
            files = []
            paginator = s3.get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
                for obj in page.get("Contents", []):
                    key = obj["Key"]
                    # Skip "directory" markers
                    if key.endswith("/"):
                        continue
                    basename = key.rsplit("/", 1)[-1] if "/" in key else key
                    if allowed_exts:
                        ext = _os_path.splitext(basename)[1].lstrip(".").lower()
                        if ext not in allowed_exts:
                            continue
                    files.append({
                        "file_path": key,
                        "file_name": basename,
                        "size": obj.get("Size", 0),
                    })
            return files

        files = await asyncio.get_running_loop().run_in_executor(None, _list_objects)
        logger.info("S3FolderSource: listed %d files under s3://%s/%s", len(files), bucket, prefix)

        read_files = ctx.config.get("read_files", True)
        if not read_files or not files:
            return self._make_output(
                items=files, count=len(files), source="s3_folder",
                bucket=bucket, prefix=prefix,
            )

        # ── Download and parse each file ──────────────────────────────────
        def _download_object(key):
            s3 = _build_client()
            obj = s3.get_object(Bucket=bucket, Key=key)
            return obj["Body"].read()

        all_items: list = []
        for f in files:
            file_key = f["file_path"]
            file_name = f["file_name"]
            file_size = f.get("size", 0)

            # Skip oversized files
            if file_size > self._MAX_FILE_SIZE:
                logger.warning(
                    "S3FolderSource: skipping %s (%.1f MB exceeds %.0f MB limit)",
                    file_key, file_size / 1024 / 1024, self._MAX_FILE_SIZE / 1024 / 1024,
                )
                all_items.append({
                    "_source_file": file_name, "_source_path": file_key,
                    "_error": f"File too large ({file_size} bytes)",
                })
                continue

            try:
                raw = await asyncio.get_running_loop().run_in_executor(
                    None, _download_object, file_key,
                )
                ext = _os_path.splitext(file_name)[1].lstrip(".").lower()
                file_type = _EXT_TO_TYPE.get(ext, "binary")
                parsed = await _parse_file_by_type(raw, file_type, file_name, ctx, "s3")
                parsed_items = parsed.get("items", [])
                for item in parsed_items:
                    if isinstance(item, dict):
                        item["_source_file"] = file_name
                        item["_source_path"] = file_key
                all_items.extend(parsed_items)
                logger.info("S3FolderSource: parsed %s → %d items", file_name, len(parsed_items))
            except Exception as e:
                logger.error("S3FolderSource: failed to read %s: %s", file_key, e)
                all_items.append({"_source_file": file_name, "_source_path": file_key, "_error": str(e)})

        logger.info(
            "S3FolderSource: read+parsed %d files → %d total items", len(files), len(all_items),
        )
        return self._make_output(
            items=all_items, count=len(all_items), files_read=len(files),
            source="s3_folder", bucket=bucket, prefix=prefix,
        )

