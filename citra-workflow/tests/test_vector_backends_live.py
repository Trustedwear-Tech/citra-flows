"""Live-server tests for every `vector_search` backend.

These are marked `integration` and each one SKIPS unless its server is actually
reachable, so the default unit run is unaffected. Bring them up with:

    docker run -d -p 6333:6333 qdrant/qdrant
    docker run -d -p 19530:19530 milvusdb/milvus:v2.5.4       # (standalone)
    docker run -d -p 8080:8080 -p 50051:50051 \
        -e AUTHENTICATION_ANONYMOUS_ACCESS_ENABLED=true \
        -e DEFAULT_VECTORIZER_MODULE=none \
        cr.weaviate.io/semitechnologies/weaviate:1.27.0
    docker run -d -p 5432:5432 -e POSTGRES_PASSWORD=pw pgvector/pgvector:pg16
    docker run -d -p 8000:8000 chromadb/chroma

Override any endpoint with the matching env var (see `_ENDPOINTS`). The client
libraries are optional extras and are NOT in requirements.txt, so a missing
import skips too.

Reading these drivers was not enough — running them is what found the four
defects these tests now pin down:
  * Milvus and pgvector returned the embedding itself in every row.
  * Weaviate accepted a metadata filter and silently ignored it.
  * Weaviate and Chroma returned no relevance number at all.
"""

import os
import socket
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from citra_workflow.nodes.vector import (  # noqa: E402
    _dict_to_milvus_expr,
    _search_chroma,
    _search_milvus,
    _search_pgvector,
    _search_qdrant,
    _search_weaviate,
)

pytestmark = pytest.mark.integration

DIM = 4
NAME = "citraflows_verify"
# The probe is the third basis vector, so the nearest neighbour is always the
# row carrying [0,0,1,0] — asserted by name rather than by position.
PROBE = [0.0, 0.0, 1.0, 0.0]
NEAREST_BODY = "gamma"

_ENDPOINTS = {
    "qdrant": os.getenv("TEST_QDRANT_URL", "http://localhost:6333"),
    "milvus": os.getenv("TEST_MILVUS_URL", "http://localhost:19530"),
    "weaviate": os.getenv("TEST_WEAVIATE_URL", "http://localhost:8080"),
    "chroma": os.getenv("TEST_CHROMA_URL", "http://localhost:8000"),
    "pgvector": os.getenv(
        "TEST_PGVECTOR_DSN", "postgresql://postgres:pw@localhost:5432/postgres"
    ),
}


def _require(name, module=None):
    """Skip unless the client library imports AND the server answers."""
    if module:
        pytest.importorskip(module, reason=f"{module} is an optional extra")
    url = _ENDPOINTS[name]
    if name == "pgvector":
        host, port = "localhost", 5432
        if "@" in url:
            hostport = url.rsplit("@", 1)[1].split("/", 1)[0]
            host = hostport.split(":")[0]
            port = int(hostport.split(":")[1]) if ":" in hostport else 5432
    else:
        from urllib.parse import urlparse

        p = urlparse(url)
        host, port = p.hostname or "localhost", p.port or 80
    sock = socket.socket()
    sock.settimeout(2)
    reachable = sock.connect_ex((host, port)) == 0
    sock.close()
    if not reachable:
        pytest.skip(f"no {name} listening on {host}:{port}")
    return url


def _assert_common(rows, relevance_key):
    """Every backend must agree on this much, whatever it is called underneath."""
    assert rows, "search returned no rows"
    assert rows[0]["body"] == NEAREST_BODY, f"wrong nearest neighbour: {rows[0]}"
    assert rows[0].get("tenant") == "acme", f"metadata not flattened in: {rows[0]}"
    assert rows[0].get(relevance_key) is not None, (
        f"no relevance number under {relevance_key!r}: {sorted(rows[0])}"
    )
    # The embedding must never be echoed back: at production dims it would be
    # copied into every downstream item and into the LLM prompt verbatim.
    leaked = [k for k, v in rows[0].items() if isinstance(v, (list, tuple)) and len(v) == DIM]
    assert not leaked, f"embedding leaked into result under {leaked}"


# ── rows shared by every backend ──────────────────────────────────────
# body/tenant pairs; the vector is the i-th basis vector.
ROWS = [("alpha", "acme"), ("beta", "globex"), ("gamma", "acme"), ("delta", "globex")]


def _basis(i):
    return [1.0 if j == i else 0.0 for j in range(DIM)]


async def test_qdrant():
    url = _require("qdrant", "qdrant_client")
    from qdrant_client import QdrantClient
    from qdrant_client.models import Distance, PointStruct, VectorParams

    c = QdrantClient(url=url)
    if c.collection_exists(NAME):
        c.delete_collection(NAME)
    c.create_collection(NAME, vectors_config=VectorParams(size=DIM, distance=Distance.COSINE))
    c.upsert(NAME, points=[
        PointStruct(id=i + 1, vector=_basis(i), payload={"body": b, "tenant": t})
        for i, (b, t) in enumerate(ROWS)
    ])
    try:
        _assert_common(await _search_qdrant(None, url, "", NAME, None, PROBE, 2), "score")
        # The code documents that a raw Qdrant filter dict is accepted; prove it.
        filtered = await _search_qdrant(
            None, url, "", NAME, None, PROBE, 5,
            query_filter={"must": [{"key": "tenant", "match": {"value": "globex"}}]})
        assert filtered and all(r["tenant"] == "globex" for r in filtered), filtered
    finally:
        c.delete_collection(NAME)


async def test_milvus():
    url = _require("milvus", "pymilvus")
    from pymilvus import DataType, MilvusClient

    c = MilvusClient(uri=url)
    if c.has_collection(NAME):
        c.drop_collection(NAME)
    schema = c.create_schema(auto_id=False, enable_dynamic_field=True)
    schema.add_field("id", DataType.INT64, is_primary=True)
    # Deliberately NOT called "vector": the driver must find it from the schema.
    schema.add_field("my_embedding", DataType.FLOAT_VECTOR, dim=DIM)
    schema.add_field("body", DataType.VARCHAR, max_length=64)
    schema.add_field("tenant", DataType.VARCHAR, max_length=64)
    idx = c.prepare_index_params()
    idx.add_index(field_name="my_embedding", index_type="FLAT", metric_type="COSINE")
    c.create_collection(NAME, schema=schema, index_params=idx)
    c.insert(NAME, [
        {"id": i, "my_embedding": _basis(i), "body": b, "tenant": t, "extra": f"dyn-{i}"}
        for i, (b, t) in enumerate(ROWS)
    ])
    c.load_collection(NAME)
    try:
        rows = await _search_milvus(None, url, "", NAME, None, PROBE, 2)
        _assert_common(rows, "score")
        # Narrowing output_fields would have cost us dynamic fields; it must not.
        assert rows[0].get("extra") == "dyn-2", f"dynamic field lost: {rows[0]}"

        assert _dict_to_milvus_expr({"tenant": "globex"}) == 'tenant == "globex"'
        by_dict = await _search_milvus(None, url, "", NAME, None, PROBE, 5,
                                       query_filter={"tenant": "globex"})
        assert by_dict and all(r["tenant"] == "globex" for r in by_dict), by_dict
        # A raw expression string must pass through untouched.
        by_str = await _search_milvus(None, url, "", NAME, None, PROBE, 5,
                                      query_filter='tenant == "acme"')
        assert by_str and all(r["tenant"] == "acme" for r in by_str), by_str
    finally:
        c.drop_collection(NAME)


async def test_weaviate():
    url = _require("weaviate", "weaviate")
    import weaviate
    import weaviate.classes.config as wc

    klass = "CitraflowsVerify"  # Weaviate class names must be CapitalCase
    conn = dict(http_host="localhost", http_port=int(url.rsplit(":", 1)[1]), http_secure=False,
                grpc_host="localhost", grpc_port=50051, grpc_secure=False)
    c = weaviate.connect_to_custom(**conn)
    try:
        if c.collections.exists(klass):
            c.collections.delete(klass)
        c.collections.create(
            klass,
            vectorizer_config=wc.Configure.Vectorizer.none(),
            properties=[wc.Property(name="body", data_type=wc.DataType.TEXT),
                        wc.Property(name="tenant", data_type=wc.DataType.TEXT)],
        )
        coll = c.collections.get(klass)
        with coll.batch.dynamic() as b:
            for i, (body, tenant) in enumerate(ROWS):
                b.add_object(properties={"body": body, "tenant": tenant}, vector=_basis(i))
    finally:
        c.close()

    try:
        _assert_common(await _search_weaviate(None, url, "", klass, None, PROBE, 2), "distance")
        # This filter used to be accepted and then dropped, returning every
        # tenant's rows for a single-tenant query.
        filtered = await _search_weaviate(None, url, "", klass, None, PROBE, 5,
                                          query_filter={"tenant": "globex"})
        assert filtered and all(r["tenant"] == "globex" for r in filtered), filtered
    finally:
        c = weaviate.connect_to_custom(**conn)
        try:
            c.collections.delete(klass)
        finally:
            c.close()


async def test_pgvector():
    dsn = _require("pgvector", "psycopg")
    import psycopg

    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
        cur.execute(f'DROP TABLE IF EXISTS "{NAME}"')
        cur.execute(f'CREATE TABLE "{NAME}" (id serial PRIMARY KEY, '
                    f"embedding vector({DIM}), body text, tenant text)")
        for i, (body, tenant) in enumerate(ROWS):
            cur.execute(f'INSERT INTO "{NAME}" (embedding, body, tenant) '
                        f"VALUES (%s::vector, %s, %s)",
                        ("[" + ",".join(map(str, _basis(i))) + "]", body, tenant))
        conn.commit()
    try:
        _assert_common(await _search_pgvector(None, dsn, "", NAME, None, PROBE, 2), "distance")

        filtered = await _search_pgvector(None, dsn, "", NAME, None, PROBE, 5,
                                          query_filter={"tenant": "globex"})
        assert filtered and all(r["tenant"] == "globex" for r in filtered), filtered

        # Filter VALUES must be bound, not interpolated. A quote-heavy value has
        # to come back as zero rows with the table still standing.
        hostile = await _search_pgvector(None, dsn, "", NAME, None, PROBE, 5,
                                         query_filter={"tenant": f'x\'; DROP TABLE "{NAME}"; --'})
        assert hostile == [], hostile
        with psycopg.connect(dsn) as conn, conn.cursor() as cur:
            cur.execute("SELECT to_regclass(%s)", (NAME,))
            assert cur.fetchone()[0] is not None, "table was dropped — value is interpolated"

        # IDENTIFIERS cannot be bound, so they must be rejected outright.
        for bad_table in ('users"; DROP TABLE x; --', "a-b"):
            with pytest.raises(ValueError):
                await _search_pgvector(None, dsn, "", bad_table, None, PROBE, 1)
        with pytest.raises(ValueError):
            await _search_pgvector(None, dsn, "", NAME, None, PROBE, 1,
                                   query_filter={'bad"col': "x"})
    finally:
        with psycopg.connect(dsn) as conn, conn.cursor() as cur:
            cur.execute(f'DROP TABLE IF EXISTS "{NAME}"')
            conn.commit()


async def test_chroma():
    url = _require("chroma", "chromadb")
    import chromadb
    from urllib.parse import urlparse

    p = urlparse(url)
    c = chromadb.HttpClient(host=p.hostname, port=p.port or 8000)
    try:
        c.delete_collection(NAME)
    except Exception:
        pass
    coll = c.create_collection(NAME, metadata={"hnsw:space": "cosine"})
    coll.add(
        ids=[b for b, _ in ROWS],
        embeddings=[_basis(i) for i in range(len(ROWS))],
        documents=[f"{b} doc" for b, _ in ROWS],
        metadatas=[{"body": b, "tenant": t} for b, t in ROWS],
    )
    try:
        # Both URL spellings must work — HttpClient takes host/port parts, so a
        # full URL has to be parsed rather than passed through.
        for spelling in (url, f"{p.hostname}:{p.port or 8000}"):
            rows = await _search_chroma(None, spelling, "", NAME, None, PROBE, 2)
            _assert_common(rows, "distance")
            assert rows[0]["document"] == f"{NEAREST_BODY} doc", rows[0]

        # ids/documents/metadatas/distances are four parallel lists; an
        # off-by-one silently pairs the wrong document with the wrong id.
        every = await _search_chroma(None, url, "", NAME, None, PROBE, 4)
        for r in every:
            assert r["document"] == f"{r['id']} doc", f"id/document misaligned: {r}"

        filtered = await _search_chroma(None, url, "", NAME, None, PROBE, 5,
                                        query_filter={"tenant": "globex"})
        assert filtered and all(r["tenant"] == "globex" for r in filtered), filtered
    finally:
        c.delete_collection(NAME)
