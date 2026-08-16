# Citra Flows

**AI-authored workflow automation you run on your own infrastructure.**

Describe a pipeline in plain English. The builder assembles it from typed nodes
you can open, inspect and edit on a canvas — it cannot invent one. Run it,
watch every node's input and output, then deploy it behind a webhook or a cron
schedule. Point it at your own models and nothing leaves your network.

```bash
make wizard                      # generates .env with fresh secrets, then starts everything
python scripts/smoke_test.py     # signs in, runs a workflow, asserts it completed
```

Then open <http://localhost:8088>. Full guide: [`INSTALL.md`](INSTALL.md).

---

## Draft → debug → ship

The point of the product is how short that loop is.

### 1. Draft it by describing it

Open the AI panel on the canvas and say what you want:

> *Every morning, pull yesterday's failed payments from Postgres, ask the model
> which are worth retrying, write those to a sheet and email me the rest.*

You get a real graph — nodes, edges, config — not a suggestion to copy out.
Three things make it trustworthy rather than a party trick:

- **It can only use nodes that exist.** The system prompt is built per request
  from the live node registry, so the model has no vocabulary for a node the
  engine cannot run.
- **It knows your connections.** Your saved connections are in the prompt too,
  so it wires `payments-db` rather than inventing a placeholder you have to
  find and fix.
- **Refining returns a diff, not a rewrite.** Ask for a change and the API
  answers with nodes/edges added, removed and updated. Your manual edits
  between AI turns survive; you review the patch before it lands.

Everything it produces is an ordinary workflow. Drag a node, retype a field,
delete half of it — the AI is a way to start, not a format you are locked into.

### 2. Debug it by running it

Hit **Run**, pick `test`, and watch it execute.

When something fails you get the node that failed, its error, how long it took,
how many times it was retried, and the raw input and output of every node that
ran before it. A failing REST call shows you the response. A transform that
dropped every row shows you the rows it started with.

Errors are meant to be actionable rather than merely honest. A misconfigured
transform tells you which parameter is missing and what a correct one looks
like; it does not surface a bare Python `KeyError`. A node that tries to reach
a private address says so, because that is a deliberate guard and not a bug.

### 3. Ship it

Deploying validates the graph, mints a webhook token, and registers the cron
schedule if the workflow has one. From there:

- **Versions** — every deploy snapshots the graph. Diff any two, roll back to
  any of them.
- **Test and prod are the same graph, different credentials.** A saved
  connection carries a separate config per environment, resolved at run time.
  You promote a workflow without editing it, and staging credentials cannot
  leak into a production run.
- **Triggers** — manual, cron, webhook (with a rotatable token), or a typed
  start that declares the inputs a run expects.
- **Human checkpoints** — drop in an approval node and the run pauses until
  someone decides.

## What you can build

Screen a thousand applicants down to ten. Pull yesterday's orders, evaluate
each against your rules, write the exceptions to a sheet and email the summary.
Watch an SFTP folder and process what lands. Reconcile two systems nightly and
raise only the differences. Retrieve from your vector store, rerank, and draft
a grounded answer a human signs off on.

The pipeline is the unit of work: the AI writes it, you review it, the engine
runs it on schedule with retries and an audit trail.

## Why an engine rather than a script

A script does the logic. The engine does everything around it:

| | |
|---|---|
| Retries | per-node, with backoff |
| Durability | Redis Streams — a restart resumes, it does not restart |
| Scheduling | leader-elected cron; replicas do not double-fire |
| Audit | every node's input, output and timing, per run |
| Credentials | saved connections resolved at run time, never in the graph |
| Isolation | connections are tenant-scoped and fail closed |

## The node catalogue

52 nodes, all typed and inspectable:

| | |
|---|---|
| **11 sources** | SQL, MongoDB, REST, S3, SFTP/FTP, folder readers, file fetch, vector search, MCP |
| **16 processors** | LLM, classify, extract, summarise, validate, dedupe, merge, transform, OCR, transcribe, embed, rerank, code |
| **8 logic** | condition, switch, loop, parallel split, merge/wait, delay, set variable, human approval |
| **11 outputs** | SQL write, Excel/CSV/PDF export, email, S3, SFTP, webhook, notify |
| **4 triggers** | manual, scheduled, webhook, typed start |
| **2 agents** | tool-calling agent, vision |

Two are deliberately generic:

- **`vector_search`** — any vector database: Qdrant, Milvus, Weaviate, pgvector,
  Chroma. Each is exercised by an integration test against a live server.
  Clients import lazily, so you install only the one you use.
- **`mcp_server`** — any standards-compliant Model Context Protocol server.
  `list_tools` to discover, `call_tool` to invoke.

Adding a node needs **no UI work** — the palette and the AI builder's vocabulary
are both generated from the backend registry.

## Safety

The agent node can call tools, and tools can mutate things. Write-capable tools
are blocked unless you opt in on that node. For MCP servers that means an
explicit `allow_writes` flag (default off), plus the protocol's `readOnlyHint` /
`destructiveHint` annotations, plus a name check for servers that publish
neither.

**Absence of metadata is never treated as permission.** A blocked write fails
the node loudly and is audited — it is not fed back to the model as a tool
result, and it is not retried, because retrying would only re-attempt the same
write. See [`ARCHITECTURE.md`](ARCHITECTURE.md) §6 for why that rule exists.

## Requirements

Mongo · Redis · object storage (S3/MinIO) · Docker for the `code_block` sandbox
· an OpenAI-compatible model endpoint.

The model endpoint is the only one you have to choose. Start on a hosted API to
evaluate; point `LLM_BASE_URL` at your own vLLM or Ollama for production and
nothing leaves your network. Everything except the AI features — the builder,
sources, transforms, logic, outputs, scheduling, approvals — runs without a
model at all.

Three processes make up the product and **all three are required**: the API, the
web UI, and the worker that executes runs. The API only enqueues a run; without
a worker, every run sits in `queued` while the stack reports itself healthy.

Sign-in is required rather than optional: workflows, runs and saved connections
belong to a user, and every API call is authorised on that identity. The first
account is created once at startup from `WORKFLOW_BOOTSTRAP_*`; there is no
public sign-up.

## Architecture

See [`ARCHITECTURE.md`](ARCHITECTURE.md) for the execution model, provenance in
[`VENDORED.md`](VENDORED.md), and known gaps in [`PORTING.md`](PORTING.md).

## Community

**Discord:** https://discordapp.com/channels/1519703038724669551/1519703039416467518
— shared with Citra Decks and Citra Projects. Questions, setup issues, or what
a real deployment needs that isn't here yet.

## Licence

Business Source License 1.1. See [`LICENSE`](LICENSE) and [`NOTICE`](NOTICE).
Non-production use is granted; production use needs a commercial licence until
the Change Date, after which this converts to Apache-2.0.

The `citra-common` submodule is separate and is Apache-2.0 — see its own
LICENSE and NOTICE.

Trustedwear Tech Private Limited · contact@citra-ai.com · https://citra-ai.com
