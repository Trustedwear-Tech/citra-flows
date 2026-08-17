# Copyright (c) 2026 Trustedwear Tech Private Limited (https://citra-ai.com)
# Author: Rohit Kumar Chandan
# SPDX-License-Identifier: BUSL-1.1
#
# Licensed under the Business Source License 1.1. Non-production use is granted;
# production use requires a commercial licence until the Change Date, after
# which this file converts to Apache-2.0. See LICENSE at the repository root.

"""
Text chunking utility for the Dept Data Flow pipeline.

Used by `ChunkEmbedProcessorNode` to split arbitrary text records into
fixed-size chunks suitable for embedding. Replaces the per-connector
chunking logic that lived in `source-mcp-template/connectors/*` so
ingestion can be driven by the workflow engine instead of a per-MCP
APScheduler.

This is intentionally simple — paragraph-aware char-window splitter with
overlap. Heavy structured-document handling (PDF/DOCX page extraction,
header detection) stays in the source nodes; here we just turn text into
embeddable chunks.
"""

from __future__ import annotations

from typing import List

DEFAULT_CHUNK_SIZE = 1000   # characters per chunk
DEFAULT_OVERLAP = 150       # characters carried over between chunks
MAX_CHUNK_CHARS = 65000     # Milvus VARCHAR ceiling (with headroom)


def _split_paragraphs(text: str) -> List[str]:
    """Coarse paragraph split — preserves blank-line boundaries."""
    parts = [p.strip() for p in text.replace("\r\n", "\n").split("\n\n")]
    return [p for p in parts if p]


def chunk_text(
    text: str,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    overlap: int = DEFAULT_OVERLAP,
) -> List[str]:
    """Split *text* into chunks ≤ ``chunk_size`` chars with ``overlap``.

    Tries to keep paragraph boundaries when possible; falls back to a
    sliding char window when a single paragraph exceeds ``chunk_size``.
    """
    if not text:
        return []
    chunk_size = max(50, min(int(chunk_size), MAX_CHUNK_CHARS))
    overlap = max(0, min(int(overlap), chunk_size - 1))

    paragraphs = _split_paragraphs(text)
    chunks: List[str] = []
    buf: List[str] = []
    buf_len = 0

    def flush():
        nonlocal buf, buf_len
        if buf:
            chunks.append("\n\n".join(buf).strip())
            buf, buf_len = [], 0

    for para in paragraphs:
        # Long single paragraph → window-split
        if len(para) > chunk_size:
            flush()
            start = 0
            step = chunk_size - overlap
            while start < len(para):
                chunks.append(para[start : start + chunk_size])
                start += step
            continue

        if buf_len + len(para) + 2 > chunk_size:
            flush()
        buf.append(para)
        buf_len += len(para) + 2

    flush()
    return [c[:MAX_CHUNK_CHARS] for c in chunks if c]
