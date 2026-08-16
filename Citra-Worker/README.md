# Citra-Worker

Out-of-process worker that runs long-lived jobs (workflows, batch reports,
scheduled tasks) so they don't block Citra-Service's HTTP request workers.

## Why this exists

Today, `Citra-Service/workflow_engine/` runs workflows in the same FastAPI
process that serves user requests. A multi-minute workflow stalls the
gunicorn worker that has to serve a chat turn 200 ms later. This service
takes those long jobs onto a Redis-backed queue and runs them on
dedicated worker processes.

## Architecture

```
Citra-Service (request handler)
    │
    │  queue.enqueue("workflow.run", {...})    (sync, fire-and-forget)
    ▼
Redis list:  citra:worker:queue:default
    │
    │  BLPOP (5-s window)
    ▼
Citra-Worker  (N worker tasks per process)
    │
    │  registry.get(job.handler)(payload, ctx)
    ▼
Handler runs  →  mark_done(result)  /  mark_failed(error, permanent=...)
                       │
                       ▼
                Redis hash:  citra:worker:job:<job_id>
                  status, result, started_at, finished_at, retries
```

## Files

| File                        | Role                                              |
|-----------------------------|---------------------------------------------------|
| `queue.py`                  | Producer + consumer of Redis-backed jobs          |
| `registry.py`               | `@register("name")` decorator + dispatch lookup   |
| `worker.py`                 | Main loop (pulls jobs, dispatches, records)       |
| `handlers/__init__.py`      | Side-imports every handler module to register it  |
| `handlers/workflow_handlers.py` | Stub `workflow.run` + `ping` handlers          |

## Producer side (Citra-Service)

```python
from queue import enqueue, get_status   # queue.py is here in Citra-Worker;
                                        # for the producer side, copy or
                                        # publish as a shared package once
                                        # the contract stabilises.

job_id = enqueue(
    "workflow.run",
    payload={"workflow_id": "...", "params": {...}},
    tenant_id=user.org_id,
    request_id=tracing.get_request_id(request),
)

# Later — poll:
status = get_status(job_id)
if status and status.get("status") == "done":
    return status["result"]
```

## Consumer side (this service)

Run the worker:

```
python -m worker
```

Tune via env vars:

| Env var                       | Default     | Effect                              |
|-------------------------------|-------------|-------------------------------------|
| `CITRA_WORKER_CONCURRENCY`    | 4           | Concurrent worker tasks per process |
| `CITRA_WORKER_QUEUES`         | `default`   | Comma-list of queues to watch       |
| `CITRA_WORKER_MAX_RETRIES`    | 3           | Retry count per transient failure   |
| `CITRA_WORKER_RESULT_TTL`     | 604800 (7d) | How long results stick in Redis     |
| `CITRA_WORKER_SHUTDOWN_GRACE` | 30          | Seconds to drain on SIGTERM         |
| `REDIS_HOST`/`REDIS_PORT`/etc | localhost   | Same Redis as the rest of Citra     |

Multiple queues for priority lanes:

```
CITRA_WORKER_QUEUES=high,default,low python -m worker
```

The worker BLPOPs across the list in priority order — `high` jobs are
picked up before `default` even when both have work.

## Adding a new handler

1. Drop a file in `handlers/`:

   ```python
   # handlers/report_handlers.py
   from queue import JobPermanentFailure
   from registry import register, JobContext

   @register("report.compose")
   async def compose_report(payload: dict, ctx: JobContext) -> dict:
       if not payload.get("report_id"):
           raise JobPermanentFailure("report_id is required")
       # ... do the work ...
       return {"status": "ok", "url": "..."}
   ```

2. Side-import it in `handlers/__init__.py`:

   ```python
   from . import report_handlers
   ```

3. Citra-Service enqueues:

   ```python
   enqueue("report.compose", payload={"report_id": "..."})
   ```

That's it. No service restart required when only the producer changes;
the worker needs a restart to pick up the new handler module.

## Migration plan for `Citra-Service/workflow_engine/`

Incremental — one workflow type at a time. Don't move all 120 files
in one shot.

| Step | What                                                                    |
|------|-------------------------------------------------------------------------|
| 1    | Pick a single workflow type (start with `report_compose`).              |
| 2    | Copy its orchestration into `handlers/<name>_handlers.py`.              |
| 3    | Replace the in-process call in Citra-Service with `queue.enqueue(...)`. |
| 4    | Add a polling endpoint in Citra-Service that returns `get_status()`.    |
| 5    | Verify under load. Ship.                                                |
| 6    | Repeat for the next workflow type.                                      |
| 7    | When all are migrated → delete `Citra-Service/workflow_engine/`.        |

Each migration is independent and reversible — any handler that breaks
just gets reverted in Citra-Service to the in-process call.

## Failure semantics

- Handler raises **`JobPermanentFailure`** → status=`failed`, no retry.
  Use for invalid input or non-retryable conditions.
- Handler raises **any other exception** → status=`queued` again, retry
  count incremented, re-pushed to the queue. After `max_retries` exhausted
  → status=`failed`.
- Handler returns normally → status=`done`, return value JSON-serialised
  into the `result` field.
- Worker process killed mid-job → the job is "lost" (still has status=`running`
  in Redis but no one's working on it). Next iteration of the producer can
  detect stale `running` jobs (started_at older than N minutes) and re-enqueue.
  TODO when migration starts.
