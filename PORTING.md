# Porting checklist — cross-system wiring to remove

This repo was cut from a tree where the workflow engine ran **inside** the Citra
platform. Several nodes only work when a Citra Decision System is running. They
must go before this is a standalone product, and two generic replacements come
in.

**Status: sections 1-8 are DONE** (section 8, the UI shell, landed 2026-08-09).
Verified after the surgery: 52 nodes register and appear in the palette, none of
them cross-system, and both replacements are among them. All five `vector_search`
backends have since been run against live servers — see ARCHITECTURE.md §7 for
what that found and what is still unproven.

## 1. Remove — Citra dept-MCP / Milvus ingestion (`dept_flow` category)

- [x] `dept_mcp_source` — calls a Citra dept-MCP `/query`
- [x] `dept_mcp_action` — calls a Citra dept-MCP `/execute_action`
- [x] `dept_mcp_historical_pull` — bulk paginated dept-MCP pull
- [x] `chunk_embed` — exists only to feed `vector_sink`
- [x] `vector_sink` — writes Citra's `mcp_<dept>_<source_id>` collection naming
- [x] `structured_schema_sink` — already deprecated
- [x] `catalogue_sink` — already retired
- [x] `NodeCategory.DEPT_FLOW` itself
- [x] `'dept_flow'` in `NodePalette.js` `CATEGORY_ORDER` / `CATEGORY_META`

## 2. Remove — Decision System agent plumbing

These build few-shot samples for a Decision App's agent and mean nothing here.

- [x] `sample_packager`  - [x] `fewshot_selector`  - [x] `sample_refresh_guard`
- [x] `sample_store_sink`  - [x] `sample_vector_sink`

## 3. Remove — direct Decision System calls

- [x] `smart_app_invoke` — POST `/apps/{slug}/run`
- [x] `staging_writer` — POST `/workflow-staging`, parks a run for approval in
      the Citra UI

## 4. Add — generic replacements

- [x] **`vector_search`** — connect to ANY vector database (Qdrant, Weaviate,
      pgvector, Chroma, Milvus-as-a-server) by URL + credential + collection.
      No Citra naming convention, no discovery lookup, no assumption about who
      wrote the collection.
- [x] **`mcp_server`** — call ANY standards-compliant MCP server: base URL,
      credential, tool name, arguments. The industry protocol, not Citra's
      dept-MCP contract.

Both belong in the ordinary `source` / `processor` categories.

## 5. Rewrite the prompts that name the old nodes

The node palette is data-driven, so deleting a node removes it from the UI
automatically. **Prompts are not.** Two files hard-code the vocabulary and will
keep proposing nodes that no longer exist:

- [x] `citra_workflow/ai_assistant.py` — instructs the model to use
      `dept_mcp_source` / `dept_mcp_action`, and to "never touch dept-flow
      internal nodes (chunk_embed / vector_sink …)".
- [x] `citra_workflow/nodes/agents.py` — the AI-agent node's MCP tool layer.

  Two more turned up later than this list, in places a search for "prompt"
  would not reach, and both were reaching the model:
- [x] `citra_workflow/nodes/outputs.py` — `webhook_output`'s **description**
      told the model to use `dept_mcp_action` instead. Node descriptions are
      copied verbatim into the AI palette, so a removed node kept being
      advertised long after its class was deleted.
- [x] `citra_workflow/router.py` — the generation system prompt still had rules
      for `dept_mcp_source` / `dept_mcp_action` and the "Dept MCP Catalogue",
      and the /refine user message told the model to preserve `chunk_embed` /
      `vector_sink` nodes "bound to a Dept Source registry entry".
- [x] `citra_workflow/workflow_reference_validator.py` — validated those same
      node types plus `ai_agent.mcp_tools` against a catalogue that is now
      always empty. Dead branches removed.

  A regression test now asserts the palette contains none of the removed node
  names (`tests/test_workflow_ai_context.py::test_palette_never_offers_removed_citra_nodes`),
  because the palette is registry-driven but descriptions and hints are free
  text — the registry being clean does not make the prompt clean.

## 6. ⚠️ The write-guard will silently stop guarding

`nodes/agents.py` blocks write-capable MCP tools using two Citra-specific
signals: a `dept_*` tool-name prefix, and **write-verb metadata from the Citra
discovery service**. Neither exists once the dept-MCP is gone, so the guard
keeps running and keeps passing — protecting nothing.

- [x] Re-implement it against the generic MCP server's own tool metadata, **or**
      remove agent MCP tools entirely.

Do not ship a guard that no longer guards.

## 6b. The build-time tripwire — CLEARED

`services/enterprise_mcp_client.py` is **deliberately NOT vendored**. It is the
Citra dept-MCP client, and everything in sections 1 and 6 depends on it, so
leaving it out turns "cross-system wiring still present" from a documentation
claim into an import that does not resolve.

It is referenced from exactly two live call sites plus tests:

- `citra_workflow/ai_context.py` — builds dept-MCP tool context for the assistant
- `citra_workflow/router.py` — `discover_tools` for the `/agent-tools` endpoint
- `tests/conftest.py` stubs it into `sys.modules`, so the suite runs regardless

Both call sites are gone. `GET /api/workflows/agent-tools` and
`GET /api/workflows/mcp-sources` were removed with them, as was the builder's
dept-MCP catalogue context (which called Citra's data-discovery-service).
Every remaining `from bucket|services.* import` in the engine now resolves.

## 7. Connectivity

- [x] The MCP connection must be an ordinary user-configured endpoint (base URL
      + credential). No discovery-service lookup; no assumption a Citra
      Decision System is reachable.

## 8. UI shell — BUILT

`ui/` is now a runnable Expo / react-native-web app, not just components:

- [x] App entry (`package.json`, `app.json`, `babel.config.js`, `index.js`).
- [x] **Landing page** — what the product is, what it needs to run, one "Sign
      in" action. No remote assets, so it renders identically air-gapped.
- [x] **Sign-in** against `POST /api/auth/login`, which did not exist: this tree
      vendored `citra_auth` (which VERIFIES tokens and mints SYSTEM tokens for
      scheduled runs) but nothing issued a token for a human. `auth_routes.py`
      adds login + `/me` over a bcrypt-hashed `workflow_users` collection, and
      the first account comes from `WORKFLOW_BOOTSTRAP_*` at startup.
- [x] Shell around `WorkflowBuilderScreen` — header, signed-in identity, sign
      out. Thin on purpose: the builder owns its own navigation already.
- [x] `HowToUseModal`, rewritten for the workflow builder alone.

Route: landing → sign in → workflow list → canvas. Sign-in is required, not
optional — workflows, runs and connections are per-user and every API call
authorises on the JWT.

Two things found while wiring it up:

- `GET /api/workflows/agent-tools` had been removed along with the dept-MCP
  discovery it also served, but `NodeConfigPanel`'s tool picker still called it
  and swallowed the 404 into an empty list — so the agent node looked like it
  had no tools. Restored, serving `AVAILABLE_TOOLS` (the built-ins the executor
  can actually dispatch) and nothing else.
- `WorkflowService.getMcpSources` was dropped: its endpoint is genuinely gone
  and no component calls it.

- [ ] Not yet done: click through the flow against a live backend.
