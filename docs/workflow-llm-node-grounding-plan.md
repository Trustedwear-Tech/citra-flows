> **RETIRED 2026-07-17.** Skill-Service and the Skills UI were deleted from the
> platform. This document is kept as a historical record; the design it describes
> no longer exists. Domain knowledge now lives in the SOP Library (documents) and
> app memory (learned decisions); the builder's vocabulary ships as file-based
> skills under `smart-app-service/skills/`.

---
# Workflow LLM-node grounding — plan

**Status:** Plan — not yet implemented
**Last updated:** 2026-05-22
**Related:** [`smart-app-fewshot-from-history-plan.md`](smart-app-fewshot-from-history-plan.md),
[`workflow-visibility-ownership.md`](workflow-visibility-ownership.md)

---

## What this is

Let an LLM / AI-Agent node in a workflow **optionally** be grounded in the
customer's own historical data, so it decides "like the record" instead of
on a generic prompt alone. The node carries a visible state badge; grounding
is a one-click, opt-in step the user may skip — **~70% of nodes never need
it** and work fine on the prompt the AI builder wrote.

This is the [smart-app few-shot pattern](smart-app-fewshot-from-history-plan.md)
re-aimed from smart-app agents at workflow LLM nodes.

## Naming — this is NOT model training

There is **no model training, no LoRA, no GPU, no inference change.** It is
**few-shot grounding**: building prompt context from historical records (+
optional RAG). The original request called it "training" — that word sets a
false expectation (cost, model weights). Throughout the product and this
plan the term is **grounding**.

- Node badge: **grey "Not grounded"** (default) → **amber "Grounding…"** →
  **green "Grounded"**.
- The amber/grey state is *not an error*. An ungrounded node is fully valid.
  **Deploy must never be blocked on grounding state.**

## The two-layer model

The original two-layer instinct is correct. Both layers are served from a
**single shared `samples` Milvus collection**, with per-row metadata filters
identifying which org / agent / workflow / node a row belongs to (see
[Vector storage](#vector-storage--single-samples-collection) below):

| Layer | What | When fetched | Mechanism |
|---|---|---|---|
| **Layer 1 — canonical** | 5–15 curated examples that anchor the decision schema | Same set, **once per execution** | filter `org_id=… AND node_id=… AND is_canonical=true` |
| **Layer 2 — neighbor** | Top-K examples most similar to the actual record | Per record (see cost note) | vector search, same scope filter, `is_canonical=false` |

## Decision: do NOT write many skills to Skill-Service

The original idea — generate many skill YAMLs, upload them to Skill-Service,
let the selector match one per request — was **already evaluated and dropped
by the smart-app team.** The fewshot plan's 2026-05-10 header records it:

> *"dropped the third artifact (separately written skill YAML in
> Skill-Service)… No Skill-Service writeback."*

Two reasons, both still true:

1. **Skill-Service has no write API.** Adding `POST /skills` + a
   `skill_writer` security scope is real work and a new attack surface.
2. **The Skill-Service selector is keyword/schema-scored** (`schema 0.6 +
   keywords 0.3 + tags 0.1`), not embedding-based. Matching "what kind of
   request came in" by keywords is materially weaker than vector similarity.

Generated examples live as **vector data in Milvus**, never as Skill-Service
skills — see the two sections below. A future phase may still write *one*
skill (the canonical block) to Skill-Service for cross-node reuse — deferred
until a concrete reuse case appears (Phase 4 below).

## Skill-Service scope — simplified to global / org / dept

Skill-Service holds **deliberately authored, governed, reusable** domain
skills — *few, stable, shared*. It is **not** a home for machine-generated
per-app examples (those are vector data — next section). The two systems
have opposite lifecycles and must not be conflated.

Decision (safe to apply now — the platform is pre-launch, no migration):
**drop the `service_account` and `user` owner levels.** Skill-Service keeps
**`global | org | dept`** only.

- **`user` removed** — a user-level skill is orphaned when the user leaves.
  The platform already made this exact call for workflows (`owner_type=user`
  is rejected). A user's domain knowledge belongs at **dept** level so it
  survives and is shared.
- **`service_account` removed** — an SA-private skill is a silo; anything
  worth hand-authoring is worth sharing at dept level. SA-level only ever
  really served the *generated-skill* case, which now lives in Milvus.

Concrete changes (small, pre-launch): drop `SERVICE_ACCOUNT` + `USER` from
`OwnerType` in `Skill-Service/models.py` (and `OWNER_PRECEDENCE_BONUS`);
selector precedence becomes `dept > org > global`; `select_skills` callers
pass the resource owner's `org_id` + `dept_ids`, not `service_account_id` /
`user_id` (drop those params from `citra-skill-client`).

## Vector storage — single `samples` collection

Generated examples — smart-app few-shot **and** workflow-node grounding —
live in **one shared Milvus collection**, not one-collection-per-agent.
Collection-per-agent is a Milvus anti-pattern at scale: thousands of tiny
collections exhaust per-collection memory / loaded-segment overhead / cloud
collection caps.

One `samples` collection; every row carries filterable metadata:

| Field | Purpose |
|---|---|
| `org_id` | tenant scope — **Milvus partition key** |
| `agent_id` | set for smart-app rows, null otherwise |
| `workflow_id` + `node_id` | set for workflow-grounding rows, null otherwise |
| `is_canonical`, `decision`, `severity`, `version` | runtime filters |
| `dense_vector`, `source_id`, `input_json`, … | payload |

Three must-dos:

1. **`org_id` is the Milvus partition key** — physical per-tenant separation
   inside the one collection, not just a scalar filter. Set `num_partitions`
   for the expected tenant count.
2. **One query helper that always injects `org_id`** — with a shared
   collection, isolation is logical; a query that forgets the `org_id`
   filter leaks across tenants. Never hand-write the filter.
3. **Refresh = versioned delete-then-insert, not atomic-swap** — you cannot
   swap a whole shared collection for one owner's refresh. Each row has a
   `version`; insert the new version, flip the owner's "current version"
   pointer (in Mongo `agent_samples` meta), query filters `version=current`,
   GC old versions async.

Collection name: `samples_<env>` — the env suffix avoids dev/prod collision
on a shared Milvus instance. `VectorSinkNode` must **reject** user-authored
collection names using the reserved prefixes `samples_` / `mcp_`.

**Refactor required (pre-launch, no data migration):** `SampleVectorSinkNode`
today writes one collection per agent (`samples_<agent_id>`) and
`NeighborSamplesTool` reads it. Both move to the single-collection + filter
model above.

## Runtime cost rule

A workflow LLM node can fire in a batch loop (e.g. 1000 job applications).

- **Layer 1 (canonical)** — fetch/build **once per workflow execution**, never
  per record.
- **Layer 2 (neighbor)** — per record only when records differ meaningfully;
  otherwise cache per execution. Never a network round-trip per item by
  default. (The smart-app runtime deliberately avoids per-call RAG re-fetch —
  heed the same.)

## What already exists (verified 2026-05-22)

The hard part — the sample pipeline — **is already built**, despite the
fewshot plan still being marked "not yet implemented" (doc drift):

| Component | Location | Reuse |
|---|---|---|
| `SamplePackagerNode` | `citra-workflow/.../nodes/processors.py:1486` | PII-scrub, dedupe, embed samples |
| `FewShotSelectorNode` | `citra-workflow/.../nodes/processors.py:1664` | stratified canonical selection |
| `SampleStoreSinkNode` | `citra-workflow/.../nodes/outputs.py:1892` | raw samples → Mongo `agent_samples` |
| `SampleVectorSinkNode` | `citra-workflow/.../nodes/outputs.py:2026` | embedded samples → Milvus — **refactor to single `samples` collection** |
| `NeighborSamplesTool` | `smart-app-service/models.py:489` + `tools_v2_dispatch.py` | runtime neighbor retrieval — **refactor to filter the shared collection** |
| `VectorSinkNode` / atomic-swap | `citra-workflow/.../nodes/outputs.py:1292` | generic vector sink — **add reserved-prefix guard** |

**Implication:** grounding a workflow LLM node is mostly *wiring* — node
state, a UI, and runtime injection — **not** rebuilding the sample pipeline.
The one structural change is the per-agent → single-collection refactor
([Vector storage](#vector-storage--single-samples-collection)).

## Plan

### Phase 0 — node grounding state (~150 LOC)

Add a `grounding` block to the LLM / AI-Agent node config:

```
grounding: {
  status: "none" | "grounding" | "grounded",
  examples_collection: str | null,   # Milvus collection name
  canonical_block: str | null,       # cached layer-1 markdown
  canonical_count: int,
  source_ref: { node_id, filters },  # which upstream source was sampled
  last_grounded_at: datetime | null,
}
```

The node card renders the badge off `status`. A "Ground this node" button
on the card (and in `NodeConfigPanel`) opens the Phase 1 modal.

### Phase 1 — grounding modal (UI, ~300 LOC)

On open: resolve the node's **upstream** source node (the trigger/source
feeding this LLM node), propose a sample query, let the user confirm filters
+ sample size and optionally pick a RAG / data-store collection. "Generate
examples" kicks Phase 2. Progress streams back into the modal.

### Phase 2 — grounding job (~300–450 LOC — mostly wiring existing nodes)

Reuse the existing pipeline: pull historical records → `SamplePackagerNode`
→ `FewShotSelectorNode` → `SampleVectorSinkNode` → `SampleStoreSinkNode`.
Rows land in the shared `samples` collection tagged with `org_id` +
`workflow_id` + `node_id` and `is_canonical` flags; the cached canonical
block is written onto the node config. Runs as an inline job or a hidden
sub-workflow scoped to the workflow's SA.

Includes the **per-agent → single-collection refactor** of
`SampleVectorSinkNode` and `NeighborSamplesTool` (the one-time structural
change; pre-launch, no data migration).

### Phase 3 — runtime injection (~250 LOC)

When the LLM / AI-Agent node executes and `status == "grounded"`:
inject the canonical block into the system prompt (once per execution), and
do a neighbor vector search per record (cost rule above). Reuse the existing
`=== APPLY THESE DOMAIN SKILLS ===` injection slot.

### Phase 4 — optional, deferred

If grounded nodes should also surface as reusable **skills** in Skill-Service
for cross-node reuse: write **one** skill (the canonical block) via a new
Skill-Service write API. Not needed for v1 — defer until a real reuse case
appears.

## Ownership & scoping

Sample rows are owned by the **workflow's Service Account**, scoped exactly
like the workflow itself — see
[`workflow-visibility-ownership.md`](workflow-visibility-ownership.md). In
the shared `samples` collection that ownership is expressed as the `org_id`
partition key plus the `workflow_id` / `node_id` tags; the always-injected
`org_id` filter is the tenant-isolation boundary.

## Scope

| Area | LOC |
|---|---|
| Phase 0 — node state + badge | ~150 |
| Phase 1 — grounding modal UI | ~300 |
| Phase 2 — grounding job (wiring existing nodes) | ~300–450 |
| Phase 2 — per-agent → single-collection refactor | ~200 |
| Phase 3 — runtime injection | ~250 |
| Skill-Service simplification (drop user + SA levels) | ~120 |
| **Total v1** | **~1,300–1,450** |

No GPU, no new service. Phase 4 (if ever) adds ~190 LOC of Skill-Service
write API.

## Biggest efficiency call

The few-shot capability must be **one shared capability** consumed by both
smart-app agents and workflow LLM nodes — not built twice. The pipeline
nodes already exist; build the grounding *wrapper* (Phases 0–3) so it is
node-agnostic and a smart-app agent could call the same job.

## Out of scope

- Model training / LoRA / adapter serving — explicitly not this.
- Skill-Service write API — deferred to Phase 4, may never be needed.
- Auto re-grounding on data drift — future.
- Grounding non-LLM nodes — only LLM / AI-Agent nodes have a prompt to ground.

## Open questions

1. Per-node grounding vs per-workflow — current plan is **per-node** (each
   LLM node does a different job). Confirm.
2. Who may ground a node — workflow owner / SA admin only, or any editor?
   Default: same as edit rights on the workflow.
3. Neighbor retrieval per-record vs per-execution caching — needs a real
   batch-workflow benchmark to tune the default.
4. `org_id` partition-key `num_partitions` — sized to the expected tenant
   count; if the deployment ever exceeds the Milvus partition cap, fall back
   to hashing `org_id`. Decide the cap at build time.

## Single-sentence summary

Give every workflow LLM node an opt-in "Ground" button that runs the
already-built few-shot pipeline over the node's own upstream historical data,
producing canonical + neighbor examples in one shared `samples` Milvus
collection (filtered by org / workflow / node) injected at runtime — the
smart-app grounding pattern, re-aimed at workflow nodes, with no model
training and no Skill-Service writeback.
