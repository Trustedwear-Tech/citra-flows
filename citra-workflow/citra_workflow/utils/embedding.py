# Copyright (c) 2026 Trustedwear Tech Private Limited (https://citra-ai.com)
# Author: Rohit Kumar Chandan
# SPDX-License-Identifier: BUSL-1.1
#
# Licensed under the Business Source License 1.1. Non-production use is granted;
# production use requires a commercial licence until the Change Date, after
# which this file converts to Apache-2.0. See LICENSE at the repository root.

"""
Embedding helper for workflow nodes.

Wraps the shared ``llm_client`` AsyncOpenAI embedding client so workflow
nodes (`ChunkEmbedProcessorNode`, structured schema sink) can produce
vectors using the same model the rest of Citra-Service uses.

A small async semaphore caps concurrent embedding calls per process,
replacing the per-dept ``embed_queue`` singleton in
``source-mcp-template/ingestion/embed_queue.py`` (since workflows already
have node-level concurrency limits).
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import List

logger = logging.getLogger(__name__)

# Per-process cap on simultaneous embedding calls. Tunable via env so
# operators can lower it on shared / rate-limited embedding endpoints.
_MAX_CONCURRENCY = max(1, int(os.getenv("WF_EMBED_MAX_CONCURRENCY", "8")))
_DEFAULT_BATCH = max(1, int(os.getenv("WF_EMBED_BATCH_SIZE", "64")))

_semaphore: asyncio.Semaphore | None = None


def _get_semaphore() -> asyncio.Semaphore:
    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(_MAX_CONCURRENCY)
    return _semaphore


async def embed_texts(texts: List[str], batch_size: int = _DEFAULT_BATCH,
                      model: str | None = None) -> List[List[float]]:
    """Embed *texts* in sub-batches; return one vector per input, in order.

    ``model`` overrides the configured default for one call. It exists because
    a node must be able to embed with the SAME model a target collection was
    built with — embedding a query with a different model than the index
    returns confident nonsense rather than an error, so this is a correctness
    control, not a convenience.

    Concurrency is bounded by ``WF_EMBED_MAX_CONCURRENCY``.
    """
    if not texts:
        return []

    # Lazy import — llm_client pulls AsyncOpenAI which is heavy.
    from citra_llm import get_embedding_client, get_embedding_model

    client = get_embedding_client()
    model = model or get_embedding_model()
    sem = _get_semaphore()

    out: List[List[float]] = []
    for i in range(0, len(texts), batch_size):
        batch = texts[i : i + batch_size]
        async with sem:
            resp = await client.embeddings.create(model=model, input=batch)
        # OpenAI client returns objects sorted by index already, but be defensive.
        items = sorted(resp.data, key=lambda d: d.index)
        out.extend([list(d.embedding) for d in items])
    return out


async def embed_one(text: str, model: str | None = None) -> List[float]:
    """Convenience wrapper for embedding a single string."""
    vectors = await embed_texts([text], model=model)
    return vectors[0] if vectors else []
