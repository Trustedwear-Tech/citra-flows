# Copyright (c) 2026 Trustedwear Tech Private Limited (https://citra-ai.com)
# Author: Rohit Kumar Chandan
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may not
# use this file except in compliance with the License. You may obtain a copy of
# the License at http://www.apache.org/licenses/LICENSE-2.0

"""
Embedding and reranking nodes.

`vector_search` embeds its own query, which is enough to RETRIEVE but leaves two
gaps these nodes fill:

  vector_embed  — embedding otherwise exists only inside the search node, so
                  there is no way to build an ingestion pipeline at all
                  (source -> chunk -> embed -> store).
  reranker      — raw approximate-nearest-neighbour ordering is a blunt
                  instrument. Retrieval systems that cite sources over-fetch and
                  re-score with a cross-encoder, because "closest in embedding
                  space" and "actually answers the question" are different
                  things.

Both talk to endpoints you configure. Nothing here assumes a particular vendor.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from ..models import NodeType, NodeCategory
from . import BaseNode, NodeContext, NodeFieldSchema, register_node

logger = logging.getLogger(__name__)

DEFAULT_RERANK_TIMEOUT = 60


@register_node
class VectorEmbedNode(BaseNode):
    node_type = NodeType.VECTOR_EMBED
    category = NodeCategory.PROCESSOR
    label = "Embed Text"
    description = "Turn a text field into a vector, ready to store or search with"
    icon = "🧬"
    color = "#0ea5e9"

    @classmethod
    def get_fields(cls) -> List[NodeFieldSchema]:
        return [
            NodeFieldSchema(name="text_field", label="Text Field", type="text", required=True,
                            default="text", placeholder="text",
                            help_text="Item field holding the text to embed."),
            NodeFieldSchema(name="output_field", label="Output Field", type="text",
                            default="embedding",
                            help_text="Where to put the vector on each item."),
            NodeFieldSchema(name="model", label="Embedding Model", type="text",
                            placeholder="leave blank for the configured default",
                            help_text="MUST match the model the target collection was built "
                                      "with. A mismatch is not an error — retrieval just "
                                      "returns nonsense from a different vector space."),
            NodeFieldSchema(name="batch_size", label="Batch Size", type="number", default=64,
                            help_text="Texts per request to the embedding endpoint."),
            NodeFieldSchema(name="skip_empty", label="Skip items with no text", type="boolean",
                            default=True,
                            help_text="On: items whose text field is empty pass through "
                                      "unembedded. Off: they fail the run."),
        ]

    async def execute(self, ctx: NodeContext) -> Any:
        from ..utils.embedding import embed_texts

        field = (ctx.config.get("text_field") or "text").strip()
        out_field = (ctx.config.get("output_field") or "embedding").strip()
        model = (ctx.config.get("model") or "").strip() or None
        batch = int(ctx.config.get("batch_size") or 64)
        skip_empty = bool(ctx.config.get("skip_empty", True))

        items = list(ctx.items or [])
        if not items:
            return self._make_output(items=[], embedded=0)

        todo, texts = [], []
        for idx, item in enumerate(items):
            raw = (item or {}).get(field)
            text = "" if raw is None else str(raw).strip()
            if not text:
                if skip_empty:
                    continue
                raise ValueError(
                    f"Item {idx} has no text in field {field!r}. Fix the upstream node, "
                    "or turn on 'Skip items with no text' if that is expected."
                )
            todo.append(idx)
            texts.append(text)

        if not texts:
            logger.warning("VectorEmbed: no item had text in %r — nothing embedded", field)
            return self._make_output(items=items, embedded=0, skipped=len(items))

        vectors = await embed_texts(texts, batch_size=batch, model=model)
        if len(vectors) != len(texts):
            # Silent truncation here would poison a collection with vectors
            # attached to the wrong rows, and nothing downstream could detect it.
            raise ValueError(
                f"Embedding endpoint returned {len(vectors)} vectors for {len(texts)} "
                "inputs. Refusing to attach them — the pairing would be wrong."
            )

        for idx, vec in zip(todo, vectors):
            items[idx] = {**(items[idx] or {}), out_field: vec}

        dim = len(vectors[0]) if vectors else 0
        logger.info("VectorEmbed: %d item(s) embedded, dim=%d", len(todo), dim)
        return self._make_output(items=items, embedded=len(todo),
                                 skipped=len(items) - len(todo), dimension=dim)


@register_node
class RerankerNode(BaseNode):
    node_type = NodeType.RERANKER
    category = NodeCategory.PROCESSOR
    label = "Rerank"
    description = "Re-score retrieved items against a query with a cross-encoder"
    icon = "🎯"
    color = "#8b5cf6"

    @classmethod
    def get_fields(cls) -> List[NodeFieldSchema]:
        return [
            NodeFieldSchema(name="endpoint", label="Rerank Endpoint", type="text", required=True,
                            placeholder="http://reranker.internal:7302/rerank",
                            help_text="Any service speaking the standard rerank shape "
                                      "(Cohere, Jina, Voyage, OpenRouter, or your own)."),
            NodeFieldSchema(name="api_key", label="API Key", type="password",
                            help_text="Sent as `Authorization: Bearer`. Blank for none."),
            NodeFieldSchema(name="query", label="Query", type="textarea", required=True,
                            placeholder="{{ vars.question }}",
                            help_text="What the items are being scored against. Supports {{ }}."),
            NodeFieldSchema(name="text_field", label="Text Field", type="text", default="text",
                            help_text="Item field holding the text to score."),
            NodeFieldSchema(name="model", label="Model", type="text",
                            placeholder="rerank-english-v3.0",
                            help_text="Optional; sent through when the endpoint expects it."),
            NodeFieldSchema(name="top_n", label="Keep Top N", type="number", default=10,
                            help_text="How many items survive. 0 keeps all, reordered."),
            NodeFieldSchema(name="score_field", label="Score Field", type="text",
                            default="relevance_score",
                            help_text="Where to record each item's score."),
            NodeFieldSchema(name="timeout_seconds", label="Timeout (s)", type="number",
                            default=DEFAULT_RERANK_TIMEOUT),
        ]

    async def execute(self, ctx: NodeContext) -> Any:
        import httpx

        endpoint = (ctx.config.get("endpoint") or "").strip().rstrip("/")
        if not endpoint:
            raise ValueError("Rerank node needs an endpoint.")
        field = (ctx.config.get("text_field") or "text").strip()
        score_field = (ctx.config.get("score_field") or "relevance_score").strip()
        top_n = int(ctx.config.get("top_n") or 0)
        timeout = float(ctx.config.get("timeout_seconds") or DEFAULT_RERANK_TIMEOUT)

        query = _render(ctx.config.get("query") or "", ctx)
        if not query.strip():
            raise ValueError("Rerank node needs a non-empty query after interpolation.")

        items = list(ctx.items or [])
        docs, keep_idx = [], []
        for i, it in enumerate(items):
            raw = (it or {}).get(field)
            if raw is None or not str(raw).strip():
                continue
            docs.append(str(raw))
            keep_idx.append(i)
        if not docs:
            logger.warning("Rerank: no item had text in %r — passing input through", field)
            return self._make_output(items=items, reranked=0)

        headers = {"Content-Type": "application/json"}
        key = ctx.config.get("api_key") or ""
        if key:
            headers["Authorization"] = f"Bearer {key}"
        payload: Dict[str, Any] = {"query": query, "documents": docs}
        if ctx.config.get("model"):
            payload["model"] = ctx.config["model"]
        if top_n:
            payload["top_n"] = top_n

        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(endpoint, headers=headers, json=payload)
            resp.raise_for_status()
            body = resp.json()

        ranked = _parse_rerank(body, len(docs))
        out = []
        for pos, score in ranked:
            src = items[keep_idx[pos]]
            out.append({**(src or {}), score_field: score})
        if top_n:
            out = out[:top_n]

        logger.info("Rerank: %d in -> %d out (query=%.40s)", len(docs), len(out), query)
        return self._make_output(items=out, reranked=len(out), candidates=len(docs))


def _render(template: str, ctx: NodeContext) -> str:
    import re
    variables = getattr(ctx, "variables", {}) or {}
    item = (ctx.items or [{}])[0] if ctx.items else {}

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

    return re.sub(r"\{\{\s*([^}]+?)\s*\}\}", lambda m: _resolve(m.group(1)), template)


def _parse_rerank(body: Any, n_docs: int) -> List[tuple]:
    """Normalise the several shapes rerank services return.

    Cohere / Jina / Voyage / OpenRouter:  {"results": [{"index": i, "relevance_score": s}]}
    Some self-hosted services:            {"reranked_chunks": [{"index": i, "score": s}]}
    Bare list:                            [{"index": i, "score": s}]

    An index outside the input range is a protocol violation, not something to
    clamp: silently dropping or wrapping it would reorder someone's citations.
    """
    rows = None
    if isinstance(body, dict):
        for key in ("results", "reranked_chunks", "data", "documents"):
            if isinstance(body.get(key), list):
                rows = body[key]
                break
    elif isinstance(body, list):
        rows = body
    if rows is None:
        raise ValueError(
            f"Rerank response had no recognisable results list. Got keys: "
            f"{sorted(body)[:8] if isinstance(body, dict) else type(body).__name__}"
        )

    out = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        idx = r.get("index", r.get("idx"))
        score = r.get("relevance_score", r.get("score", r.get("_score")))
        if idx is None:
            continue
        idx = int(idx)
        if not 0 <= idx < n_docs:
            raise ValueError(
                f"Rerank service returned index {idx} for {n_docs} document(s). "
                "Refusing to guess which item it meant."
            )
        out.append((idx, float(score) if score is not None else 0.0))

    if not out:
        raise ValueError("Rerank response contained no usable {index, score} rows.")
    # Trust the service's ordering only if it scored; otherwise sort ourselves.
    out.sort(key=lambda t: t[1], reverse=True)
    return out
