# Copyright (c) 2026 Trustedwear Tech Private Limited (https://citra-ai.com)
# Author: Rohit Kumar Chandan
# SPDX-License-Identifier: BUSL-1.1
#
# Licensed under the Business Source License 1.1. Non-production use is granted;
# production use requires a commercial licence until the Change Date, after
# which this file converts to Apache-2.0. See LICENSE at the repository root.

"""Multimodal processor nodes — turn image / audio media into text or LLM
reasoning, regardless of WHERE the media came from.

Media is NOT tied to one source: an item may carry it as a ``{"_blob": {...}}``
reference (upload / file-fetch), a URL on any field (an API record, a catalogue
row), or a base64 string (an S3/SFTP file the parser emitted as
``content_base64``). These nodes resolve the bytes from whatever shape the item
has, then feed the model — so an image/audio can flow in from any upstream
source. They FAIL LOUD when an item carries no resolvable media (wired wrong),
never silently skip.

Backing models already exist in Citra and are wired in the worker env:
  * OCR  / Vision → Qwen-VL / GLM-4.6V via ``qwen_ocr_proxy`` + ``citra_llm`` vision client
  * Audio → Whisper-large-v3-turbo via ``whisper_proxy``
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import logging
import os
from typing import Any, Dict, List, Optional, Tuple

from ..models import NodeType, NodeCategory, NodeFieldSchema
from . import BaseNode, NodeContext, register_node, interpolate_variables

logger = logging.getLogger(__name__)

_IMAGE_MIME_DEFAULT = "image/png"
_AUDIO_MIME_DEFAULT = "audio/mpeg"

# Hard cap on how large a single media item may be. The bytes are loaded whole
# into memory (and base64-expanded for vision), so an unbounded file is an OOM
# risk. Above this, fail loud and tell the user to stage it as a file/blob.
MAX_MEDIA_BYTES = int(os.getenv("WF_MAX_MEDIA_BYTES", str(50 * 1024 * 1024)))


def _sniff_mime(data: bytes, default: str = "") -> str:
    """Detect MIME from magic bytes so the model/proxy gets the correct format
    even when no header/descriptor provided one (or provided a wrong/generic
    one). Returns ``default`` when the format isn't recognised."""
    if len(data) < 12:
        return default
    h = data[:16]
    if h.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if h.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if h[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if h[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if h[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return "audio/wav"
    if h[:4] == b"%PDF":
        return "application/pdf"
    if h[:3] == b"ID3" or h[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"):
        return "audio/mpeg"
    if h[:4] == b"OggS":
        return "audio/ogg"
    if h[:4] == b"fLaC":
        return "audio/flac"
    if data[4:8] == b"ftyp":
        return "audio/mp4"  # m4a / mp4 container
    return default

# Item fields that commonly carry a media URL or base64 payload, so media can
# arrive from ANY upstream source (API record, catalogue row, file parser, …),
# not only the dedicated blob reference.
_URL_FIELDS = ("url", "image_url", "audio_url", "media_url", "file_url", "src", "href", "uri", "link")
_B64_FIELDS = ("data_base64", "content_base64", "image_base64", "audio_base64", "base64", "b64", "data")
_MIME_FIELDS = ("mime", "mime_type", "content_type", "format")


def _looks_base64(s: str) -> bool:
    """Heuristic: a longish base64-ish string (not a URL/JSON/plain text)."""
    s = s.strip()
    if len(s) < 16 or "://" in s or s[0] in "{[":
        return False
    # data: URL form
    if s.startswith("data:"):
        return True
    import re
    return bool(re.fullmatch(r"[A-Za-z0-9+/=\s]+", s[:512]))


def _decode_base64(value: str) -> bytes:
    """Decode a base64 string, tolerating a ``data:<mime>;base64,`` prefix."""
    s = value.strip()
    if s.startswith("data:") and "," in s:
        s = s.split(",", 1)[1]
    try:
        return base64.b64decode(s, validate=False)
    except (binascii.Error, ValueError) as exc:
        raise ValueError(f"field is not valid base64: {exc}") from exc


async def _fetch_url_bytes(url: str, timeout: float = 30.0) -> Tuple[bytes, str]:
    """SSRF-guarded download → (bytes, mime)."""
    import httpx
    from ..utils.ssrf import assert_url_is_public
    assert_url_is_public(url)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        mime = (resp.headers.get("content-type") or "").split(";")[0].strip()
        return resp.content, mime


async def _resolve_item_media(
    node: BaseNode, ctx: NodeContext, item: Any, *,
    media_field: str, default_mime: str,
) -> Tuple[bytes, str, str]:
    """Resolve one input item's media to (bytes, mime, filename), trying, in
    order: an explicit ``media_field``, a ``{_blob}`` ref, a URL field, a base64
    field. Raises (fail-loud) if nothing resolves."""
    from ..blob_store import is_blob

    if not isinstance(item, dict):
        raise ValueError(f"expected a dict item carrying media, got {type(item).__name__}")

    sibling_mime = next((str(item[m]) for m in _MIME_FIELDS if item.get(m)), "") or default_mime

    async def _from_value(val: Any) -> Tuple[bytes, str, str]:
        # A blob descriptor (either the full {_blob: …} item or the inner dict).
        if isinstance(val, dict):
            blob_item = val if "_blob" in val else {"_blob": val}
            if is_blob(blob_item):
                d = blob_item["_blob"]
                return (await node.get_blob(blob_item),
                        d.get("mime") or sibling_mime, d.get("filename") or "media")
            raise ValueError("media field is a dict but not a {_blob} reference")
        s = str(val).strip()
        # data: URL or raw base64 → decode locally.
        if s.startswith("data:") or _looks_base64(s):
            return _decode_base64(s), sibling_mime, "media"
        # http(s)/ftp URL → SSRF-guarded fetch.
        if "://" in s or s.startswith(("http", "ftp")):
            data, mime = await _fetch_url_bytes(s)
            return data, mime or sibling_mime, s.rsplit("/", 1)[-1] or "media"
        raise ValueError("media field value is neither a URL nor base64 media")

    # 1. Explicit field pinned by the user.
    if media_field:
        if item.get(media_field) in (None, ""):
            raise ValueError(f"configured media field '{media_field}' is empty on this item")
        return await _from_value(item[media_field])

    # 2. Blob reference.
    if is_blob(item):
        d = item["_blob"]
        return await node.get_blob(item), d.get("mime") or sibling_mime, d.get("filename") or "media"

    # 3. A URL on any common field.
    for f in _URL_FIELDS:
        if item.get(f):
            data, mime = await _fetch_url_bytes(str(item[f]))
            return data, mime or sibling_mime, str(item[f]).rsplit("/", 1)[-1] or "media"

    # 4. A base64 payload on any common field.
    for f in _B64_FIELDS:
        if item.get(f):
            return _decode_base64(str(item[f])), sibling_mime, str(item.get("filename") or "media")

    raise ValueError(
        "item carries no resolvable media — expected a {'_blob': …} reference, a "
        f"URL field ({', '.join(_URL_FIELDS[:4])}…), or a base64 field "
        f"({', '.join(_B64_FIELDS[:4])}…). Set the node's 'media_field' to pin one."
    )


async def _resolve_all(node: BaseNode, ctx: NodeContext, *, default_mime: str):
    """Resolve media for every input item. Fails loud on an empty input."""
    items = ctx.items
    if not items:
        raise ValueError("No input items to process.")
    media_field = (ctx.config.get("media_field") or "").strip()
    out = []
    for item in items:
        data, mime, filename = await _resolve_item_media(
            node, ctx, item, media_field=media_field, default_mime=default_mime,
        )
        # Memory guard — bytes are held whole (and base64-expanded for vision).
        if len(data) > MAX_MEDIA_BYTES:
            raise ValueError(
                f"media '{filename}' is {len(data) // (1024*1024)} MB, over the "
                f"{MAX_MEDIA_BYTES // (1024*1024)} MB limit. Stage very large media "
                f"as a file/blob (S3/SFTP source or upload) rather than inline."
            )
        # Correct the MIME from the actual bytes so the model/proxy reads it as
        # the right format (a wrong/generic mime can break OCR/vision).
        mime = _sniff_mime(data, default=mime or default_mime)
        out.append((item, data, mime, filename))
    return out


def _media_field_schema() -> NodeFieldSchema:
    return NodeFieldSchema(
        name="media_field", label="Media field (optional)", type="text",
        placeholder="image_url",
        help_text=(
            "Field on each input item that holds the media (a URL, a base64 "
            "string, or a blob ref). Leave blank to auto-detect — the node "
            "accepts a {_blob} ref, a URL field, or a base64 field from ANY "
            "upstream source."
        ),
    )


@register_node
class FileFetchSourceNode(BaseNode):
    """Download a file from a URL into a binary blob.

    The entry point for media that lives at an http(s) URL (an image link, an
    audio file, a PDF). Emits a ``{"_blob": {...}}`` item the OCR / Audio /
    Vision nodes consume. The URL supports ``{{variable}}`` placeholders so a
    trigger payload can supply it. SSRF-guarded.
    """

    node_type = NodeType.FILE_FETCH
    category = NodeCategory.SOURCE
    label = "Fetch File (URL → blob)"
    description = (
        "Download a file (image, audio, PDF, any binary) from an http(s) URL "
        "into a blob that downstream OCR / transcribe / vision nodes can use."
    )
    icon = "📥"
    color = "#06b6d4"
    ai_authoring_hint = (
        "Use to bring an image/audio/file at a URL into the workflow as a blob. "
        "The URL can come from trigger data via {{variable}}. Wire its output "
        "into an OCR, Transcribe Audio, or Vision LLM node."
    )

    @classmethod
    def get_fields(cls) -> List[NodeFieldSchema]:
        return [
            NodeFieldSchema(
                name="url", label="File URL", type="text", required=True,
                placeholder="https://example.com/{{filename}}",
                help_text="http(s) URL of the file. Supports {{variable}} placeholders.",
            ),
            NodeFieldSchema(
                name="filename", label="Filename (optional)", type="text",
                placeholder="scan.png",
                help_text="Override the stored filename; otherwise derived from the URL.",
            ),
            NodeFieldSchema(
                name="timeout_seconds", label="Timeout (seconds)", type="number", default=30,
            ),
        ]

    async def execute(self, ctx: NodeContext) -> Any:
        import os
        import httpx
        from urllib.parse import urlparse, unquote

        url = interpolate_variables((ctx.config.get("url") or "").strip(), ctx.variables)
        if not url:
            raise ValueError("'File URL' is required.")

        from ..utils.ssrf import assert_url_is_public
        assert_url_is_public(url)

        try:
            timeout = float(ctx.config.get("timeout_seconds", 30))
        except (TypeError, ValueError):
            timeout = 30.0

        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.content
            ctype = (resp.headers.get("content-type") or "").split(";")[0].strip()

        # Filename: explicit config → URL path basename → fallback.
        filename = (ctx.config.get("filename") or "").strip()
        if not filename:
            path = urlparse(url).path
            filename = unquote(os.path.basename(path)) or "download"
        mime = ctype or "application/octet-stream"

        descriptor = await self.put_blob(ctx, data, mime=mime, filename=filename)
        logger.info("FileFetch: %d bytes (%s) from %s → blob", len(data), mime, filename)
        return self._make_output(items=[descriptor], count=1, source="file_fetch",
                                 mime=mime, filename=filename, size=len(data))


@register_node
class OcrNode(BaseNode):
    """Extract text from image blobs via the vision OCR model (Qwen-VL)."""

    node_type = NodeType.OCR
    category = NodeCategory.PROCESSOR
    label = "OCR (image → text)"
    description = (
        "Read text out of images (scans, photos, screenshots) using the vision "
        "OCR model. The image can come from ANY upstream source — a blob, a URL "
        "field, or a base64 field. Emits one text item per image."
    )
    icon = "🔎"
    color = "#0ea5e9"
    ai_authoring_hint = (
        "Use when an upstream item carries a RAW image (a {_blob} ref, a URL field "
        "like image_url, or base64) and you need its text. Sources: a file upload, a "
        "Fetch File node, an API/source row with an image URL, etc. Do NOT place this "
        "after an FTP/SFTP/S3 Folder Reader (or S3 File / SFTP File) that has"
        "Read & Parse Files enabled — those already OCR images into text, so this node "
        "would have no raw image to read and would fail."
    )

    @classmethod
    def get_fields(cls) -> List[NodeFieldSchema]:
        return [_media_field_schema()]

    async def execute(self, ctx: NodeContext) -> Any:
        resolved = await _resolve_all(self, ctx, default_mime=_IMAGE_MIME_DEFAULT)
        results: List[Dict[str, Any]] = []
        for _item, data, mime, filename in resolved:
            def _ocr(_data=data, _mime=mime, _fn=filename):
                from qwen_ocr_proxy import extract_text_from_image
                return extract_text_from_image(
                    image_bytes=_data, filename=_fn, mime_type=_mime,
                    user_id=ctx.user_id,
                )

            text = await asyncio.get_running_loop().run_in_executor(None, _ocr)
            logger.info("OcrNode: extracted %d chars from %s", len(text or ""), filename)
            results.append({"text": text or "", "filename": filename, "format": "image"})
        return self._make_output(items=results, count=len(results), source="ocr")


@register_node
class AudioTranscribeNode(BaseNode):
    """Transcribe audio blobs to text via the speech-to-text model (Whisper)."""

    node_type = NodeType.AUDIO_TRANSCRIBE
    category = NodeCategory.PROCESSOR
    label = "Transcribe Audio (audio → text)"
    description = (
        "Convert audio (recordings, voicemails, calls) to text with the Whisper "
        "speech-to-text model. The audio can come from ANY upstream source — a "
        "blob, a URL field, or base64. Emits one transcript item per clip."
    )
    icon = "🎙️"
    color = "#8b5cf6"
    ai_authoring_hint = (
        "Use when an upstream item carries audio (a {_blob} ref, a URL field "
        "like audio_url, or base64) and you need the transcript. Sources: a "
        "file upload, a Fetch File node, an API/source row with an audio URL, etc."
    )

    @classmethod
    def get_fields(cls) -> List[NodeFieldSchema]:
        return [_media_field_schema()]

    async def execute(self, ctx: NodeContext) -> Any:
        resolved = await _resolve_all(self, ctx, default_mime=_AUDIO_MIME_DEFAULT)
        results: List[Dict[str, Any]] = []
        for _item, data, mime, filename in resolved:
            def _transcribe(_data=data, _mime=mime, _fn=filename):
                from whisper_proxy import transcribe_audio
                return transcribe_audio(
                    audio_bytes=_data, mime_type=_mime, filename=_fn,
                    user_id=ctx.user_id,
                )

            text = await asyncio.get_running_loop().run_in_executor(None, _transcribe)
            logger.info("AudioTranscribeNode: %d chars from %s", len(text or ""), filename)
            results.append({"text": text or "", "filename": filename, "format": "audio"})
        return self._make_output(items=results, count=len(results), source="audio_transcribe")


@register_node
class VisionLlmNode(BaseNode):
    """Reason over an image with a vision LLM (not just OCR text).

    Sends the image as a proper multimodal ``image_url`` content block plus a
    natural-language prompt, so the model can describe, classify, or extract
    structured fields from what it SEES — beyond flat OCR.
    """

    node_type = NodeType.VISION_LLM
    category = NodeCategory.AGENT
    label = "Vision LLM (image reasoning)"
    description = (
        "Ask a vision model a question about each image (describe, classify, "
        "extract fields). The image can come from ANY source — a blob, a URL "
        "field, or base64. Emits one answer item per image."
    )
    icon = "👁️"
    color = "#7c3aed"
    ai_authoring_hint = (
        "Use when you need to REASON about an image (what is it? extract these "
        "fields) rather than just OCR its text. The image can come from a blob, "
        "a URL field (image_url), or base64 on any upstream item."
    )

    @classmethod
    def get_fields(cls) -> List[NodeFieldSchema]:
        return [
            NodeFieldSchema(
                name="prompt", label="Prompt", type="textarea", required=True,
                placeholder="Describe this image. Supports {{variable}} placeholders.",
                help_text="What to ask the vision model about each image.",
            ),
            NodeFieldSchema(
                name="max_tokens", label="Max output tokens", type="number",
                default=1024, help_text="Cap on the model's answer length.",
            ),
            _media_field_schema(),
        ]

    async def execute(self, ctx: NodeContext) -> Any:
        prompt_template = (ctx.config.get("prompt") or "").strip()
        if not prompt_template:
            raise ValueError("'Prompt' is required for the Vision LLM node.")
        prompt = interpolate_variables(prompt_template, ctx.variables)
        try:
            max_tokens = int(ctx.config.get("max_tokens", 4000))
        except (TypeError, ValueError):
            max_tokens = 4000

        resolved = await _resolve_all(self, ctx, default_mime=_IMAGE_MIME_DEFAULT)
        results: List[Dict[str, Any]] = []
        for _item, data, mime, filename in resolved:
            b64 = base64.b64encode(data).decode("ascii")

            def _call(_b64=b64, _mime=mime):
                from citra_llm import (
                    get_vision_client, get_vision_model, get_vision_extra_body,
                )
                client = get_vision_client()
                resp = client.chat.completions.create(
                    model=get_vision_model(),
                    messages=[{
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt},
                            {"type": "image_url",
                             "image_url": {"url": f"data:{_mime};base64,{_b64}"}},
                        ],
                    }],
                    max_tokens=max_tokens,
                    extra_body=get_vision_extra_body() or None,
                )
                return resp.choices[0].message.content or ""

            answer = await asyncio.get_running_loop().run_in_executor(None, _call)
            logger.info("VisionLlmNode: %d-char answer for %s",
                        len(answer or ""), filename or "image")
            results.append({
                "answer": answer or "",
                "filename": filename or "",
                "format": "vision",
            })
        return self._make_output(items=results, count=len(results), source="vision_llm")
