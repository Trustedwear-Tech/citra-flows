<!--
  Copyright (c) 2026 Trustedwear Tech Private Limited (https://citra-ai.com)
  Author: Rohit Kumar Chandan
  SPDX-License-Identifier: Apache-2.0

  Licensed under the Apache License, Version 2.0 (the "License"); you may not
  use this file except in compliance with the License. You may obtain a copy of
  the License at http://www.apache.org/licenses/LICENSE-2.0
-->

# citra-workflow

Workflow engine package — visual agent workflows, node runtime, executor,
scheduler, models. Single canonical home, imported by **two consumers**:

| Consumer       | What it imports                                                           |
|----------------|---------------------------------------------------------------------------|
| Citra-Service  | `router` (HTTP CRUD), `models` (data shapes for dept_sources)             |
| Citra-Worker   | `executor` (runs workflows), `scheduler` (cron/interval triggers), `nodes`|

Previously this lived at `Citra-Service/workflow_engine/` and ran in
the FastAPI request process — a multi-minute workflow blocked gunicorn
workers serving chat. Moved to a shared package so Citra-Worker can run
the actual execution out-of-process.

## Layout

```
citra_workflow/
  __init__.py
  config.py              # engine config (timeouts, limits)
  models.py              # WorkflowDefinition, ExecutionRecord, etc.
  router.py              # FastAPI router for /workflows/* CRUD + manual run
  executor.py            # WorkflowExecutor — the runtime
  scheduler.py           # WorkflowSchedulerManager — cron/interval triggers
                         #   (single-leader via Redis lease)
  notifications.py       # Webhooks / outbound notifications
  schema_cache.py        # Cached schemas
  schema_discovery.py    # Source schema discovery
  tool_skills.py         # Tool/skill node integration
  workflow_context_extractor.py
  connection_crypto.py   # Encrypted connection secrets
  connection_resolver.py # Resolve named connections to live credentials
  nodes/
    __init__.py          # NodeContext, NodeResult
    triggers.py          # WebhookTrigger, ScheduleTrigger
    sources.py           # DataSource nodes
    processors.py        # Transform / filter / map nodes
    logic.py             # Branch / SetVariable / Loop
    agents.py            # AI agent nodes (LLM-driven)
    outputs.py            # Webhook / email / Slack outputs
  utils/
    chunking.py
    embedding.py
    milvus_atomic_swap.py
```

## Install (in-monorepo, editable)

```
# In Citra-Service/requirements.txt and Citra-Worker/requirements.txt:
-e ../citra-workflow
```

## Producer / consumer split

**Citra-Service** mounts the HTTP router so the UI can do CRUD on
workflows, but **does not execute** them in-process. Manual triggers
enqueue a `workflow.run` job in Redis:

citra-workflow now runs as its OWN FastAPI service. Other services
talk to it over HTTP:

```python
# smart-app-service / any consumer
import httpx
async with httpx.AsyncClient() as client:
    resp = await client.post(
        f"{settings.workflow_service_url}/api/workflows",
        headers={"Authorization": auth_header},
        json=workflow_payload,
    )
```

Inside this service, `/execute` enqueues to the Citra-Worker via the
shared `citra-queue` package:

```python
from citra_queue import enqueue
job_id = enqueue("workflow.run", {"workflow_id": wid, "user_id": uid, "trigger": "manual"})
return {"execution_id": eid, "status": "queued"}
```

**Citra-Worker** consumes the queue and runs the executor:

```python
# Citra-Worker/handlers/workflow_handlers.py
from citra_workflow.executor import WorkflowExecutor
from citra_workflow.models import WorkflowDefinition

@register("workflow.run")
async def run_workflow(payload, ctx):
    workflow = await WorkflowDefinition.load(payload["workflow_id"])
    executor = WorkflowExecutor()
    result = await executor.execute(workflow, ...)
    return result.to_dict()
```

**Citra-Worker** also runs the scheduler (cron/interval triggers) since
the scheduler is itself a long-running task:

```python
# Citra-Worker/worker.py boot path
from citra_workflow.scheduler import scheduler_manager
asyncio.create_task(scheduler_manager.run_forever())
```

The scheduler uses a Redis lease for leader election so multiple
worker replicas don't double-fire scheduled workflows.

## Models still importable from Citra-Service

`citra_workflow.models.WorkflowDefinition` is also imported by
`Citra-Service/dept_sources/` for source-template generation. Pure
data shapes, no runtime — stays cheap to import.
