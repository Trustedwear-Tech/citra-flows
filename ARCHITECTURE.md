# Architecture

Citra Flows is an **AI-authored workflow engine**: you describe a pipeline in
plain English, the builder assembles it from typed nodes, and the engine runs it
on a schedule, a webhook, or a manual trigger.

> **Pre-release.** This tree was cut from a larger platform on 2026-08-08. The
> cross-system wiring has been removed (see `PORTING.md`), but the two
> replacement nodes have not been exercised against live backends and there is
> no host UI shell yet. Read `PORTING.md` before running this anywhere real.

---

## 1. The execution model

```
   Trigger              Nodes                          Outputs
   ───────              ─────                          ───────
   manual      ┐                                  ┌──► SQL / Mongo writer
   scheduled   ├──►  source ──► processor ──►     ├──► PDF / Excel / CSV
   webhook     ┘        │          │       logic  ├──► email / webhook / notify
                        │          │         │    └──► bucket / SFTP
                        ▼          ▼         ▼
                    items      items      branch / loop / approve
```

Every node takes **items** (a list of dicts) and returns items. That uniformity
is why nodes compose without adapters, and why adding a node is cheap.

Two execution shapes:

- **Batch** — a source emits items, they flow through the graph, outputs land.
- **Event** — a webhook trigger fires per event, the same graph runs for one item.

**Human checkpoints are optional.** A `human_approval` node parks the run until
someone approves. Most pipelines do not use one; the ones touching money usually
should.

---

## 2. Why an engine rather than a script

Anyone can write a Python script. The engine earns its place with what surrounds
the logic:

| Concern | What the engine provides |
|---|---|
| Retries | per-node, with backoff |
| Durability | Redis Streams job queue; a restart resumes rather than restarts |
| Scheduling | leader-elected cron, so replicas do not double-fire |
| Audit | every node's input, output and timing recorded per run |
| Credentials | saved connections, resolved at run time — never in the graph |
| Concurrency | bounded, per node and per workflow |

The AI builder assembles graphs from those nodes. It cannot invent a node, so a
generated workflow is made only of primitives you can inspect.

---

## 3. Components

| Component | What it is |
|---|---|
| `citra-workflow/` | The engine: node registry, executor, scheduler, AI assistant, HTTP API. |
| `Citra-Worker/` | The job runner. Pulls from the durable queue and executes workflow jobs. |
| `ui/` | The web app: landing, sign-in, and the builder canvas (`components/WorkflowBuilder/`). Expo + react-native-web. |
| `citra-*/` | Six vendored packages — auth, mongo, cache, queue, llm, service-utils. See `VENDORED.md`. |
| `bucket.py`, `services/` | Vendored object-storage and sandbox helpers. |

Data stores: **Mongo** (definitions, runs), **Redis** (queue, scheduler leases),
**object storage** (file outputs). No vector database is required — see §5.

---

## 4. Nodes

Node types are declared in `citra_workflow/models.py` and implemented under
`citra_workflow/nodes/`. Each class declares `node_type`, `category`, `label`
and a `get_fields()` schema.

**The UI is data-driven from that schema.** The palette renders whatever the
backend registry returns, and the config panel renders each field by its `type`.
Adding a node therefore needs **no UI work** — implement the class, register it,
and it appears in both the palette and the AI builder's vocabulary.

Categories: `trigger` · `source` · `agent` · `processor` · `logic` · `output`.

---

## 5. Connecting to the outside world

Two general-purpose nodes replaced the platform-specific ones this tree used to
carry:

**`vector_search`** — query any vector database: Qdrant, Milvus, Weaviate,
pgvector, Chroma. URL, credential, collection. Drivers are imported **lazily**,
so a deployment that never uses the node installs no vector client, and a
missing one fails naming the package to install.

> Vectors are searched, never assumed. If `embedding_model` differs from the
> model the collection was built with, retrieval returns confident nonsense
> rather than an error. This is a known sharp edge — see `PORTING.md`.

**`mcp_server`** — call any standards-compliant Model Context Protocol server
over JSON-RPC: `list_tools` to discover, `call_tool` to invoke. Base URL,
credential, tool name, arguments.

Neither node assumes a particular platform is running. That is the whole point
of the port.

---

## 6. The write-guard

The AI agent node can call tools. Tools can mutate things. So calls are checked
before dispatch:

- `http_request` — only GET / HEAD / OPTIONS.
- Known read-only built-ins pass.
- Anything else whose **name** matches a write verb (`delete_`, `update_`,
  `send_`, …) is blocked.

The `mcp_server` node adds its own, in terms the protocol actually provides:

1. An explicit **`allow_writes` opt-in, default OFF**.
2. The MCP `readOnlyHint` / `destructiveHint` annotations when the server
   publishes them.
3. A name check when it does not.

**Absence of metadata is never read as permission.** The guard this replaced
keyed off a platform registry's write-verb metadata; ported unchanged into a
standalone deployment it would have found no metadata, passed everything, and
looked like it was working. That failure mode is the reason the new guard is
visible in node config rather than hidden in a lookup.

---

## 7. What is not here yet

- **The builder canvas has been driven end to end in a browser**, against a live
  API + worker + Mongo + Redis: landing → sign-in → template gallery → canvas
  (5 nodes and 4 edges render) → edit a node's config → delete nodes → save →
  run → **execution completed** → run history → run detail with per-node timings
  and raw output. Dropping a node from the palette onto the canvas was exercised
  too. Four defects found this way are fixed (see below).

  One gesture was **not** exercised: dragging from one node's handle to another
  to create an edge. React Flow tracks that through pointer events with
  container-relative transforms that the test harness could not reproduce
  faithfully; it is a limitation of the harness, not a known defect — the canvas
  sets `nodesConnectable` and edges created any other way render and execute
  correctly. It still needs one manual confirmation by a human with a mouse.
- **Live-backend proof — `vector_search` is now covered.** All five backends
  (Qdrant, Milvus, Weaviate, pgvector, Chroma) have been run against live
  servers, asserting nearest-neighbour order, `top_k`, payload flattening and
  the metadata filter on each. Reproduce with
  `pytest tests/test_vector_backends_live.py -m integration`; the file's
  docstring has the `docker run` line for each server, and any backend whose
  server (or optional client library) is absent skips rather than fails. `mcp_server` has been run against a live JSON-RPC
  MCP server. Running them found four defects that reading them had not:
  - Milvus and pgvector returned **the embedding itself** in every result
    (`SELECT *` / `output_fields=["*"]`) — at 1536 dims and `top_k=10` that is
    ~15k floats copied into each downstream item, and into the prompt verbatim
    when the next node is an LLM. Both now drop vector-typed fields, identified
    from the collection/table schema rather than by guessing at a field name, so
    dynamic and oddly-named fields still come through.
  - Weaviate **accepted a filter and ignored it**, returning every tenant's rows
    for a single-tenant query — the least visible place for that to happen.
  - Weaviate and Chroma returned **no relevance score at all**, so ranking or
    thresholding on relevance silently had nothing to work with there while
    working correctly on Qdrant and Milvus.

  Two caveats remain. The five backends return relevance under different names
  and directions — Qdrant and Milvus give `score` (higher is nearer), pgvector,
  Chroma and Weaviate give `distance` (lower is nearer) — because normalising
  them would mean assuming a metric the node does not know. And pgvector still
  requires the embedding column to be named `embedding`; it is not configurable.
- **Authoring affordances.** No "list tools" picker for MCP, no collection
  picker or test-connection for vectors. You type the values.
- **Packaging for the vector clients.** Deliberately absent from
  `requirements.txt` because they are optional; the extras story is unwritten.

---

## Licence

Business Source License 1.1. See `LICENSE` and `NOTICE`. Portions vendored from the Citra platform — see
`VENDORED.md`. Copyright (c) 2024–2026 Trustedwear Tech Private Limited.
