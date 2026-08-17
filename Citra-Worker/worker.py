# Copyright (c) 2026 Trustedwear Tech Private Limited (https://citra-ai.com)
# Author: Rohit Kumar Chandan
# SPDX-License-Identifier: BUSL-1.1
#
# Licensed under the Business Source License 1.1. Non-production use is granted;
# production use requires a commercial licence until the Change Date, after
# which this file converts to Apache-2.0. See LICENSE at the repository root.

"""
Citra-Worker main loop.

Pulls jobs off Redis queues, dispatches to registered handlers,
records results. One process = N concurrent worker tasks (default 4)
running on the same asyncio event loop.

Run::

    python -m worker
    # or with multiple worker tasks per process:
    CITRA_WORKER_CONCURRENCY=8 python -m worker
    # or watching priority queues:
    CITRA_WORKER_QUEUES=high,default,low python -m worker
"""
from __future__ import annotations

# Load .env BEFORE any module reads os.getenv. This must happen first so
# REDIS_HOST, MONGODB_CONN_STRING etc. are populated when worker_queue
# and the deferred-imported workflow modules pick them up.
from env_loader import load_env
load_env()

# ── Local-dev sys.path shim for Citra-Service ────────────────────────────
# Node implementations in citra-workflow defer-import Citra-Service modules
# (bucket, qwen_ocr_proxy, whisper_proxy, citra_internet_service,
# services.code_executor, services.enterprise_*, agent_samples.*,
# config.milvus_config). In prod the Dockerfile bakes Citra-Service into
# the image and sets PYTHONPATH=/app/citra-service:/app/Citra-Worker, so
# those imports resolve. In local dev the worker runs out of its own venv
# with Citra-Service sitting as a sibling directory in the monorepo — add
# it to sys.path here so the two environments behave identically. No-op if
# PYTHONPATH already contains it or the sibling doesn't exist.
import sys as _sys
from pathlib import Path as _Path
_citra_service_dir = _Path(__file__).resolve().parent.parent / "Citra-Service"
if _citra_service_dir.is_dir() and str(_citra_service_dir) not in _sys.path:
    _sys.path.insert(0, str(_citra_service_dir))

import asyncio
import logging
import os
import signal
import time
from typing import Any, List, Optional

import handlers  # noqa: F401 — side-import populates the registry
import registry
from citra_queue import (
    Job,
    JobPermanentFailure,
    consume_one,
    claim_stale,
    queue_depth,
    dlq_depth,
    mark_done,
    mark_failed,
    mark_running,
    default_consumer_name,
)

# Prometheus metrics. Defined unconditionally so handler/loop code can import
# them; only actually exported if prometheus_client is installed (it always is
# in the deployed image). Without these, a stalled/backlogged worker was
# completely invisible to ops (prod-readiness HIGH #11).
try:
    from prometheus_client import Counter as _PromCounter, Gauge as _PromGauge

    QUEUE_DEPTH = _PromGauge(
        "citra_worker_queue_depth",
        "Jobs currently waiting in a watched queue (Redis LLEN).",
        ["queue"],
    )
    DLQ_DEPTH = _PromGauge(
        "citra_worker_dlq_depth",
        "Dead-lettered jobs (exhausted retries / permanent failure) awaiting inspection.",
        ["queue"],
    )
    JOBS_PROCESSED = _PromCounter(
        "citra_worker_jobs_processed_total",
        "Jobs processed by the worker, by handler and outcome.",
        ["handler", "outcome"],  # outcome: done | failed_transient | failed_permanent
    )
    _METRICS_ON = True
except Exception:  # noqa: BLE001 — metrics are best-effort, never block the worker
    QUEUE_DEPTH = None
    DLQ_DEPTH = None
    JOBS_PROCESSED = None
    _METRICS_ON = False


def _metric_outcome(outcome: str, handler: str) -> None:
    if _METRICS_ON and JOBS_PROCESSED is not None:
        try:
            JOBS_PROCESSED.labels(handler=handler, outcome=outcome).inc()
        except Exception:  # noqa: BLE001
            pass

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger("citra-worker")

# Error tracker (GlitchTip / Sentry) — no-op unless SENTRY_DSN is set.
# Critical for the worker: deployed + scheduled workflows execute here, so
# without this their failure ERROR logs would reach Loki but NOT GlitchTip,
# and the GlitchTip → Monitoring-Service → support-email path would never
# fire for the most common execution path. LoggingIntegration captures any
# logger.error (e.g. the executor's workflow-failure log) as a GlitchTip event.
try:
    import sentry_sdk
    from sentry_sdk.integrations.asyncio import AsyncioIntegration
    from sentry_sdk.integrations.logging import LoggingIntegration

    _sentry_dsn = os.getenv("SENTRY_DSN", "").strip()
    if _sentry_dsn:
        sentry_sdk.init(
            dsn=_sentry_dsn,
            environment=os.getenv("ENVIRONMENT", "prod"),
            release=os.getenv("GIT_SHA", "unknown"),
            traces_sample_rate=float(os.getenv("SENTRY_TRACES_SAMPLE_RATE", "0.05")),
            send_default_pii=False,
            attach_stacktrace=True,
            max_breadcrumbs=50,
            integrations=[
                AsyncioIntegration(),
                LoggingIntegration(level=logging.INFO, event_level=logging.ERROR),
            ],
        )
        sentry_sdk.set_tag("service", "citra-worker")
        logger.info("Sentry/GlitchTip error tracking enabled")
except Exception as _sentry_exc:
    logger.warning("Sentry init skipped: %s", _sentry_exc)


# Defaults — tuneable per-deployment.
CONCURRENCY = int(os.getenv("CITRA_WORKER_CONCURRENCY", "4"))
QUEUES: List[str] = [
    q.strip()
    for q in os.getenv("CITRA_WORKER_QUEUES", "default").split(",")
    if q.strip()
]
# Grace period for in-flight jobs to finish on SIGTERM before force-exit. MUST be
# >= citra-workflow's NODE_TIMEOUT_SECONDS (default 300s) — at 30s a rolling
# deploy cancelled a worker mid-node, abandoning the run as `running` in Mongo
# until the stuck-execution reaper crash-resumed it (i.e. every deploy orphaned
# in-flight work). Default now 330s (node 300 + margin). Align the orchestrator's
# terminationGracePeriodSeconds to be >= this, or the platform SIGKILLs first.
SHUTDOWN_GRACE_SECONDS = int(os.getenv("CITRA_WORKER_SHUTDOWN_GRACE", "330"))

# Stable per-process consumer identity for the Redis Streams group. All worker
# tasks in this process share it; on crash the whole process's pending entries
# are reclaimable by ``claim_stale`` in one sweep.
_CONSUMER = default_consumer_name()

# How often to sweep for entries abandoned by a crashed worker and re-deliver
# them (the at-least-once safety net). Cheap XAUTOCLAIM call.
RECLAIM_INTERVAL_SECONDS = int(os.getenv("CITRA_WORKER_RECLAIM_INTERVAL", "60"))


_stopped = asyncio.Event()


async def _process_job(job: Job) -> None:
    """Run one job through its handler and persist the outcome."""
    handler = registry.get(job.handler)
    if handler is None:
        await mark_failed(job, f"no handler registered for '{job.handler}'", permanent=True)
        _metric_outcome("failed_permanent", job.handler)
        logger.error(
            f"❌ job {job.id}: no handler '{job.handler}' (registered: {registry.names()})"
        )
        return

    await mark_running(job)
    started = time.perf_counter()
    ctx = registry.JobContext(
        job_id=job.id,
        tenant_id=job.tenant_id,
        request_id=job.request_id,
        retries=job.retries,
    )

    try:
        result = await handler(job.payload, ctx)
    except JobPermanentFailure as exc:
        elapsed = time.perf_counter() - started
        logger.warning(
            f"🛑 job {job.id} ({job.handler}) PERMANENT failure in {elapsed:.2f}s: {exc}"
        )
        await mark_failed(job, str(exc), permanent=True)
        _metric_outcome("failed_permanent", job.handler)
    except Exception as exc:  # noqa: BLE001
        elapsed = time.perf_counter() - started
        logger.warning(
            f"⚠️  job {job.id} ({job.handler}) transient failure in {elapsed:.2f}s: {exc!r}"
        )
        will_retry = await mark_failed(job, repr(exc), permanent=False)
        _metric_outcome("failed_transient", job.handler)
        if will_retry:
            logger.info(
                f"   → retrying ({job.retries + 1}/{job.max_retries}) — re-enqueued"
            )
        else:
            logger.error(
                f"   → exhausted {job.max_retries} retries — giving up"
            )
    else:
        elapsed = time.perf_counter() - started
        await mark_done(job, result)
        _metric_outcome("done", job.handler)
        logger.info(
            f"✅ job {job.id} ({job.handler}) completed in {elapsed:.2f}s"
        )


async def _queue_depth_sampler(interval_seconds: int = 15) -> None:
    """Periodically export the depth of each watched queue as a Prometheus
    gauge so ops can alert on a non-draining backlog. Best-effort: a Redis
    blip just skips a sample, never crashes the worker."""
    if not _METRICS_ON or QUEUE_DEPTH is None:
        return
    # Sample depth + DLQ for the worker's own queues PLUS any extra platform
    # queues consumed elsewhere (e.g. 'smartapp', drained by smart-app-service)
    # so their backlog/DLQ are visible + alertable from this one exporter. All
    # live on the same (dedicated) queue Redis, so XLEN works regardless of who
    # consumes them.
    _extra = [q.strip() for q in os.getenv("CITRA_WORKER_EXTRA_DEPTH_QUEUES", "smartapp").split(",") if q.strip()]
    _depth_queues = list(dict.fromkeys(list(QUEUES) + _extra))
    while not _stopped.is_set():
        for q in _depth_queues:
            try:
                QUEUE_DEPTH.labels(queue=q).set(await queue_depth(q))
                if DLQ_DEPTH is not None:
                    DLQ_DEPTH.labels(queue=q).set(await dlq_depth(q))
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"depth sample for {q!r} failed: {exc}")
        try:
            await asyncio.wait_for(_stopped.wait(), timeout=interval_seconds)
        except asyncio.TimeoutError:
            pass


async def _worker_loop(worker_idx: int) -> None:
    """One concurrent worker task. Pulls jobs and processes them
    serially. The asyncio event loop interleaves I/O across all worker
    tasks for parallelism within the process."""
    logger.info(f"🟢 worker[{worker_idx}] watching queues: {QUEUES} (consumer={_CONSUMER})")
    while not _stopped.is_set():
        try:
            job: Optional[Job] = await consume_one(QUEUES, block_seconds=5, consumer=_CONSUMER)
        except Exception as exc:  # noqa: BLE001
            # Redis blip — back off briefly and try again.
            logger.warning(f"worker[{worker_idx}] consume failed: {exc}")
            await asyncio.sleep(1.0)
            continue
        if job is None:
            # No work in this window. Loop and read again.
            continue
        await _process_job(job)
    logger.info(f"🟡 worker[{worker_idx}] stopped")


async def _reclaim_loop() -> None:
    """Periodically reclaim jobs abandoned by a crashed worker (entries left
    pending past the visibility timeout) and re-process them. This is what makes
    the Streams queue at-least-once: a job a dead worker was holding is
    re-delivered here instead of being lost."""
    logger.info(f"🟢 reclaim loop active (every {RECLAIM_INTERVAL_SECONDS}s)")
    while not _stopped.is_set():
        try:
            await asyncio.wait_for(_stopped.wait(), timeout=RECLAIM_INTERVAL_SECONDS)
            break  # stopped
        except asyncio.TimeoutError:
            pass
        try:
            reclaimed = await claim_stale(QUEUES, consumer=_CONSUMER)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"reclaim sweep failed: {exc}")
            continue
        for job in reclaimed:
            if _stopped.is_set():
                break
            logger.warning(f"♻️  re-processing reclaimed job {job.id} ({job.handler})")
            await _process_job(job)
    logger.info("🟡 reclaim loop stopped")


def _install_signal_handlers(loop: asyncio.AbstractEventLoop) -> None:
    def _stop(*_: object) -> None:
        if not _stopped.is_set():
            logger.info("🛑 shutdown signal received — stopping after current jobs")
            _stopped.set()

    # Windows asyncio doesn't support add_signal_handler for SIGTERM;
    # fall back to signal.signal which is good enough for a worker.
    try:
        loop.add_signal_handler(signal.SIGTERM, _stop)
        loop.add_signal_handler(signal.SIGINT, _stop)
    except (NotImplementedError, RuntimeError):
        signal.signal(signal.SIGTERM, _stop)
        signal.signal(signal.SIGINT, _stop)


async def _start_workflow_scheduler() -> Any:
    """Boot the workflow scheduler if `citra_workflow` is installed.

    The scheduler uses Redis-leased leader election internally, so
    multiple Citra-Worker replicas can start it safely — only the
    elected leader actually fires cron triggers; followers stand by
    for failover. Returns the manager (or None if unavailable) so the
    caller can shutdown() it on graceful stop.
    """
    if os.getenv("CITRA_WORKER_RUN_SCHEDULER", "true").lower() != "true":
        logger.info("⏰ workflow scheduler disabled by env (CITRA_WORKER_RUN_SCHEDULER=false)")
        return None
    try:
        from citra_workflow.scheduler import scheduler_manager
    except ImportError as exc:
        logger.info(f"⏰ workflow scheduler not available: {exc}")
        return None
    try:
        await scheduler_manager.start()
        logger.info("⏰ workflow scheduler started (leader-elected)")
        return scheduler_manager
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"⚠️ workflow scheduler failed to start: {exc}")
        return None


async def _main() -> None:
    loop = asyncio.get_running_loop()
    _install_signal_handlers(loop)

    # Prometheus /metrics on :8080 (separate from any worker HTTP).
    # Scraped by Prometheus job 'citra-worker'. Counters are defined where used
    # (e.g. queue handlers can `from prometheus_client import Counter`).
    try:
        from prometheus_client import start_http_server
        start_http_server(8080)
        logger.info("📊 prometheus metrics exposed on :8080/metrics")
    except ImportError:
        logger.warning("prometheus_client not installed; /metrics disabled")
    except OSError as exc:
        logger.warning(f"prometheus metrics port :8080 in use, skipping: {exc}")

    logger.info(f"🚀 Citra-Worker starting | concurrency={CONCURRENCY} queues={QUEUES}")
    logger.info(f"   handlers registered: {registry.names()}")

    scheduler_manager = await _start_workflow_scheduler()

    tasks = [asyncio.create_task(_worker_loop(i)) for i in range(CONCURRENCY)]
    tasks.append(asyncio.create_task(_queue_depth_sampler()))
    tasks.append(asyncio.create_task(_reclaim_loop()))
    try:
        await _stopped.wait()
    finally:
        # Stop the scheduler first so no new cron triggers enqueue mid-shutdown.
        if scheduler_manager is not None:
            try:
                scheduler_manager.shutdown()
                logger.info("⏰ workflow scheduler stopped")
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"scheduler shutdown error: {exc}")

        # Drain: workers finish their current job, then exit on next BLPOP timeout.
        try:
            await asyncio.wait_for(
                asyncio.gather(*tasks, return_exceptions=True),
                timeout=SHUTDOWN_GRACE_SECONDS,
            )
        except asyncio.TimeoutError:
            logger.warning(
                f"shutdown grace ({SHUTDOWN_GRACE_SECONDS}s) exceeded; forcing exit"
            )
            for t in tasks:
                t.cancel()


if __name__ == "__main__":
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        pass
