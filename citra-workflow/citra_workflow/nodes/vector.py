# Copyright (c) 2026 Trustedwear Tech Private Limited (https://citra-ai.com)
# Author: Rohit Kumar Chandan
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may not
# use this file except in compliance with the License. You may obtain a copy of
# the License at http://www.apache.org/licenses/LICENSE-2.0

"""
Vector database node — search ANY vector store.

Replaces the removed Citra-coupled ingestion nodes (`chunk_embed`,
`vector_sink`, `sample_vector_sink`). Those wrote into a Citra Decision
System's Milvus using its `mcp_<dept>_<source_id>` collection convention, and
assumed a dept-MCP would serve the result. This node assumes nothing: you give
it a URL, a credential and a collection name, and it returns matches.

Supported backends are selected by `provider`. Each driver is imported lazily
so a workflow that never uses this node does not need any vector client
installed -- and so a missing driver fails with a message naming the package to
install rather than an ImportError at process start.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from ..models import NodeType, NodeCategory
from . import BaseNode, NodeContext, NodeFieldSchema, register_node

logger = logging.getLogger(__name__)

_DRIVER_HINT = {
    "milvus": "pymilvus",
    "qdrant": "qdrant-client",
    "weaviate": "weaviate-client",
    "pgvector": "psycopg[binary] + pgvector",
    "chroma": "chromadb",
}


@register_node
class VectorSearchNode(BaseNode):
    node_type = NodeType.VECTOR_SEARCH
    category = NodeCategory.SOURCE
    label = "Vector Search"
    description = "Query any vector database (Milvus, Qdrant, Weaviate, pgvector, Chroma)"
    icon = "🧭"
    color = "#0ea5e9"

    @classmethod
    def get_fields(cls) -> List[NodeFieldSchema]:
        return [
            NodeFieldSchema(
                name="provider", label="Vector Database", type="select", required=True,
                default="qdrant",
                options=[{"label": "Qdrant", "value": "qdrant"},
                         {"label": "Milvus", "value": "milvus"},
                         {"label": "Weaviate", "value": "weaviate"},
                         {"label": "pgvector (PostgreSQL)", "value": "pgvector"},
                         {"label": "Chroma", "value": "chroma"}],
                help_text="Which vector store to query. The client library is imported only when the node runs."),
            NodeFieldSchema(name="url", label="URL", type="text", required=True,
                            placeholder="http://vector-db.internal:6333",
                            help_text="Base URL of your vector database."),
            NodeFieldSchema(name="api_key", label="API Key / Token", type="password",
                            help_text="Leave blank if the database needs no auth."),
            NodeFieldSchema(name="collection", label="Collection", type="text", required=True,
                            placeholder="documents",
                            help_text="Collection / index / table to search."),
            NodeFieldSchema(name="query_text", label="Query Text", type="textarea",
                            placeholder="{{ item.question }}",
                            help_text="Text to embed and search with. Supports {{ }} interpolation. "
                                      "Leave blank and supply `query_vector` to search a precomputed vector."),
            NodeFieldSchema(name="query_vector_field", label="Query Vector Field", type="text",
                            placeholder="embedding",
                            help_text="Item field holding a precomputed vector. Used when Query Text is blank."),
            NodeFieldSchema(name="top_k", label="Top K", type="number", default=5,
                            help_text="How many matches to return per query."),
            NodeFieldSchema(name="filter", label="Filter (JSON)", type="textarea",
                            placeholder='{"tenant": "acme"}',
                            help_text="Optional metadata filter, passed through to the backend."),
            NodeFieldSchema(name="embedding_model", label="Embedding Model", type="text",
                            placeholder="text-embedding-3-small",
                            help_text="Used only when Query Text is set. Must match the model the "
                                      "collection was built with -- a mismatch returns confident nonsense "
                                      "rather than an error."),
        ]

    async def execute(self, ctx: NodeContext) -> Any:
        from ..utils.template import interpolate_dotted

        provider = (ctx.config.get("provider") or "qdrant").lower()
        url = (ctx.config.get("url") or "").strip()
        collection = (ctx.config.get("collection") or "").strip()
        if not url or not collection:
            raise ValueError("Vector Search needs both a URL and a collection name.")

        top_k = int(ctx.config.get("top_k") or 5)
        api_key = ctx.config.get("api_key") or ""
        raw_query = (ctx.config.get("query_text") or "").strip()
        vec_field = (ctx.config.get("query_vector_field") or "").strip()
        if not raw_query and not vec_field:
            raise ValueError(
                "Vector Search needs either Query Text (to embed) or a Query Vector Field "
                "(a precomputed vector on the item)."
            )

        query_filter = _parse_filter(ctx.config.get("filter"))

        try:
            search = _SEARCHERS[provider]
        except KeyError:
            raise ValueError(
                f"Unsupported vector provider '{provider}'. "
                f"Supported: {', '.join(sorted(_SEARCHERS))}."
            )

        variables = getattr(ctx, "variables", {}) or {}
        items = list(ctx.items or [{}])

        results: List[Dict[str, Any]] = []
        for item in items:
            # Query Text is interpolated PER ITEM. The field's own help text
            # advertises {{ }} support and its placeholder is {{ item.question }};
            # before this it was read raw, so a templated query searched for the
            # literal string "{{ item.question }}" and returned whatever that
            # embeds to. Same defect this repo already fixed twice (reranker,
            # mcp_server) — a documented placeholder syntax that silently did
            # nothing.
            query_text = interpolate_dotted(raw_query, item=item, variables=variables) if raw_query else ""

            vector = None
            if not raw_query:
                vector = (item or {}).get(vec_field)
                if vector is None:
                    raise ValueError(
                        f"Item has no '{vec_field}' field to search with. "
                        "Set Query Text instead, or correct the field name."
                    )
            try:
                matches = await search(ctx, url, api_key, collection, query_text,
                                       vector, top_k, query_filter)
            except ImportError as exc:
                hint = _DRIVER_HINT.get(provider, provider)
                raise ValueError(
                    f"The {provider} client is not installed in this deployment. "
                    f"Add `{hint}` to your requirements and redeploy. ({exc})"
                ) from exc
            results.extend(matches)

            # A STATIC query (no {{ }}) is the same search every iteration, so
            # run it once instead of returning top_k x len(items) duplicates.
            if raw_query and query_text == raw_query:
                break

        logger.info("VectorSearch(%s): %d match(es) from %s", provider, len(results), collection)
        return self._make_output(items=results, matched=len(results),
                                 provider=provider, collection=collection)


async def _embed(ctx: NodeContext, text: str) -> List[float]:
    """Embed with the workflow's configured embedding client."""
    from ..utils.embedding import embed_texts
    vectors = await embed_texts([text], model=ctx.config.get("embedding_model") or None)
    return vectors[0]


async def _search_qdrant(ctx, url, api_key, collection, query_text, vector, top_k, query_filter=None):
    from qdrant_client import QdrantClient
    if vector is None:
        vector = await _embed(ctx, query_text)
    client = QdrantClient(url=url, api_key=api_key or None)
    # query_points, not the long-removed `search`. Verified against a live
    # Qdrant 1.19: `QdrantClient.search` does not exist and fails with
    # AttributeError at call time -- i.e. inside a workflow run, not at import.
    kwargs = {}
    if query_filter:
        # Accepts a raw Qdrant filter dict, e.g. {"must":[{"key":"tenant","match":{"value":"acme"}}]}.
        kwargs["query_filter"] = query_filter
    res = client.query_points(collection_name=collection, query=vector, limit=top_k,
                              with_payload=True, **kwargs)
    return [{"id": p.id, "score": p.score, **(p.payload or {})} for p in res.points]


def _milvus_vector_fields(client, collection) -> set:
    """Names of the collection's vector-typed fields, from its schema.

    Milvus numbers every vector DataType at 100+ (FLOAT_VECTOR, BINARY_VECTOR,
    SPARSE_FLOAT_VECTOR, ...), so they are identified by type rather than by
    guessing at a field called "vector" — a collection can name it anything.
    """
    from pymilvus import DataType
    names = set()
    for f in client.describe_collection(collection).get("fields") or []:
        try:
            if DataType(f.get("type")).name.endswith("VECTOR"):
                names.add(f.get("name"))
        except ValueError:
            # An unknown type code is not a vector we know how to drop; keeping
            # the field is the safe direction (worst case it is verbose).
            continue
    return names


async def _search_milvus(ctx, url, api_key, collection, query_text, vector, top_k, query_filter=None):
    from pymilvus import MilvusClient
    if vector is None:
        vector = await _embed(ctx, query_text)
    client = MilvusClient(uri=url, token=api_key or "")
    kwargs = {}
    if query_filter:
        # Milvus takes a boolean expression string, not a dict.
        kwargs["filter"] = query_filter if isinstance(query_filter, str) else _dict_to_milvus_expr(query_filter)
    res = client.search(collection_name=collection, data=[vector], limit=top_k,
                        output_fields=["*"], **kwargs)
    # "*" returns the embeddings too. At 1536 dims and top_k=10 that is ~15k
    # floats copied into every downstream item — and into the prompt verbatim if
    # the next node is an LLM. Qdrant never returns vectors (`with_payload=True`),
    # so "*" also left the two backends disagreeing on result shape. Drop them
    # here rather than by narrowing output_fields, because a narrowed list would
    # also drop dynamic fields, which "*" does return.
    drop = _milvus_vector_fields(client, collection)
    out = []
    for group in res:
        for h in group:
            entity = {k: v for k, v in (h.get("entity") or {}).items() if k not in drop}
            out.append({"id": h.get("id"), "score": h.get("distance"), **entity})
    return out


def _dict_to_weaviate_filter(d):
    """Flat {property: value} -> a Weaviate v4 Filter (AND-combined)."""
    from weaviate.classes.query import Filter
    if not isinstance(d, dict) or not d:
        raise ValueError(
            f"Weaviate filters must be a JSON object of property/value pairs, got {d!r}"
        )
    combined = None
    for key, value in d.items():
        clause = Filter.by_property(str(key)).equal(value)
        combined = clause if combined is None else (combined & clause)
    return combined


async def _search_weaviate(ctx, url, api_key, collection, query_text, vector, top_k, query_filter=None):
    import weaviate
    from weaviate.classes.init import Auth
    from weaviate.classes.query import MetadataQuery
    from urllib.parse import urlparse

    # connect_to_custom takes host/port PARTS, not a URL. Passing the whole
    # "http://host:8080" as http_host produced an unreachable client.
    parsed = urlparse(url if "://" in url else f"http://{url}")
    secure = parsed.scheme == "https"
    host = parsed.hostname or url
    http_port = parsed.port or (443 if secure else 80)
    client = weaviate.connect_to_custom(
        http_host=host, http_port=http_port, http_secure=secure,
        # v4 requires gRPC params even for REST-only reads; 50051 is the default.
        grpc_host=host, grpc_port=50051, grpc_secure=secure,
        auth_credentials=Auth.api_key(api_key) if api_key else None,
    )
    try:
        coll = client.collections.get(collection)
        # `distance` was never requested, so every Weaviate result came back with
        # no relevance number while Qdrant/Milvus/Chroma all return one.
        kwargs = {"limit": top_k, "return_metadata": MetadataQuery(distance=True)}
        if query_filter:
            # This argument was accepted and then dropped on the floor: a search
            # with a tenant filter returned every tenant's rows, on the backend
            # where that failure is least visible.
            kwargs["filters"] = _dict_to_weaviate_filter(query_filter)
        if vector is None:
            res = coll.query.near_text(query=query_text, **kwargs)
        else:
            res = coll.query.near_vector(near_vector=vector, **kwargs)
        return [
            {
                "id": str(o.uuid),
                "distance": getattr(o.metadata, "distance", None) if o.metadata else None,
                **(o.properties or {}),
            }
            for o in res.objects
        ]
    finally:
        client.close()


# pgvector's own column types. `bit` (binary quantisation) is deliberately not
# listed: it is a general-purpose Postgres type, and excluding every bit column
# from results would drop legitimate scalar data.
_PGVECTOR_COLUMN_TYPES = ("vector", "halfvec", "sparsevec")


def _pg_scalar_columns(cur, table: str) -> list:
    """Every non-vector column of `table`, in declaration order."""
    cur.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = %s AND table_schema = ANY(current_schemas(false)) "
        "AND udt_name <> ALL(%s) ORDER BY ordinal_position",
        (table, list(_PGVECTOR_COLUMN_TYPES)),
    )
    cols = [r[0] for r in cur.fetchall()]
    if not cols:
        raise ValueError(
            f'Table "{table}" has no non-vector columns (or is not visible on the '
            f"current search_path); nothing could be returned from a search."
        )
    return cols


async def _search_pgvector(ctx, url, api_key, collection, query_text, vector, top_k, query_filter=None):
    import psycopg
    if vector is None:
        vector = await _embed(ctx, query_text)
    # `collection` is the table name. Identifiers cannot be parameterised, so it
    # is quoted and validated rather than interpolated raw.
    if not collection.replace("_", "").isalnum():
        raise ValueError(f"Unsafe table name for pgvector: {collection!r}")
    # pgvector wants its literal text form ('[1,2,3]'); a bare Python list has
    # no registered adapter and raises ProgrammingError at execute time.
    vec_literal = "[" + ",".join(str(float(x)) for x in vector) + "]"
    where, params = "", [vec_literal]
    if isinstance(query_filter, dict) and query_filter:
        clauses = []
        for key in query_filter:
            if not str(key).replace("_", "").isalnum():
                raise ValueError(f"Unsafe filter column for pgvector: {key!r}")
            clauses.append(f'"{key}" = %s')
            params.append(query_filter[key])
        where = " WHERE " + " AND ".join(clauses)
    params.append(top_k)
    with psycopg.connect(url) as conn, conn.cursor() as cur:
        # `SELECT *` also returns the embedding column itself — a 1536-dim vector
        # rendered as text in every row. Ask the catalog which columns are vectors
        # and select the rest by name instead. (Same leak as the Milvus path.)
        select_list = ", ".join(f'"{c}"' for c in _pg_scalar_columns(cur, collection))
        sql = (f'SELECT {select_list}, embedding <=> %s::vector AS distance '
               f'FROM "{collection}"{where} ORDER BY distance LIMIT %s')
        cur.execute(sql, tuple(params))
        cols = [c.name for c in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


async def _search_chroma(ctx, url, api_key, collection, query_text, vector, top_k, query_filter=None):
    import chromadb
    from urllib.parse import urlparse

    # HttpClient takes host/port parts, not a URL (same defect as weaviate).
    parsed = urlparse(url if "://" in url else f"http://{url}")
    ssl = parsed.scheme == "https"
    client = chromadb.HttpClient(host=parsed.hostname or url,
                                 port=parsed.port or (443 if ssl else 8000),
                                 ssl=ssl)
    coll = client.get_collection(collection)
    kwargs = {"n_results": top_k}
    if query_filter:
        kwargs["where"] = query_filter
    if vector is None:
        kwargs["query_texts"] = [query_text]
    else:
        kwargs["query_embeddings"] = [vector]
    res = coll.query(**kwargs)
    out = []
    docs = (res.get("documents") or [[]])[0]
    # Chroma returns the relevance number under "distances"; it was being dropped,
    # so a workflow that ranked or thresholded on relevance silently had nothing
    # to rank on when pointed at Chroma while working fine on Qdrant/Milvus.
    dists = (res.get("distances") or [[]])[0]
    for i, doc_id in enumerate((res.get("ids") or [[]])[0]):
        meta = ((res.get("metadatas") or [[]])[0] or [{}])[i] if res.get("metadatas") else {}
        out.append({
            "id": doc_id,
            "document": docs[i] if i < len(docs) else None,
            "distance": dists[i] if i < len(dists) else None,
            **(meta or {}),
        })
    return out


def _parse_filter(raw):
    """Parse the Filter (JSON) field. Declared but never read before this —
    the AI builder was told the field was 'passed through to the backend' and
    it silently did nothing."""
    if not raw:
        return None
    if isinstance(raw, dict):
        return raw
    import json
    try:
        return json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Filter must be valid JSON (or a backend filter string). Got: {raw!r}") from exc


def _dict_to_milvus_expr(d: dict) -> str:
    """Flat {field: value} -> Milvus boolean expression."""
    parts = []
    for k, v in d.items():
        parts.append(f'{k} == "{v}"' if isinstance(v, str) else f"{k} == {v}")
    return " and ".join(parts)


_SEARCHERS = {
    "qdrant": _search_qdrant,
    "milvus": _search_milvus,
    "weaviate": _search_weaviate,
    "pgvector": _search_pgvector,
    "chroma": _search_chroma,
}
