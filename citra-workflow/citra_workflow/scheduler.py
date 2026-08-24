# Copyright (c) 2026 Trustedwear Tech Private Limited (https://citra-ai.com)
# Author: Rohit Kumar Chandan
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may not
# use this file except in compliance with the License. You may obtain a copy of
# the License at http://www.apache.org/licenses/LICENSE-2.0

"""
Workflow Scheduler
==================
Background scheduler for:
1. Cron-triggered workflow execution (deployed workflows with schedule)
2. Approval timeout enforcement (auto-reject expired approvals)

Distributed leader election:
  ALL Citra-Service replicas participate in a Redis leader election.  Only
  the elected leader runs APScheduler and registers cron jobs.  If the
  leader's VM dies the Redis key expires after SCHEDULER_LEADER_TTL seconds
  and the next follower to poll wins, re-loading all schedules from MongoDB.
  Failover window ≈ SCHEDULER_LEADER_TTL + SCHEDULER_LEADER_POLL_INTERVAL
  (≤ 45 s by default).

Execution deduplication:
  Even with a single leader, the cron fire lock (lock:wf_cron:{id}:{ts})
  ensures at-most-one execution per fire window across every instance,
  acting as a safety net during leader transitions.

Lock Renewal:
  Long-running workflows are protected by a background heartbeat that
  extends both the cron-fire lock and the overlap lock every
  LOCK_RENEWAL_INTERVAL seconds, preventing premature expiry.

Instance identification:
  Every replica stamps its logs with SCHEDULER_INSTANCE_ID (hostname-PID)
  so operators can trace which container is the current leader.
"""

from __future__ import annotations
import asyncio
import json
import logging
import uuid
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from .config import (
    APPROVAL_CHECK_INTERVAL_MIN, SCHEDULER_CRON_LOCK_TIMEOUT,
    SCHEDULER_APPROVAL_LOCK_TIMEOUT, DEFAULT_APPROVAL_TIMEOUT_HOURS,
    SCHEDULER_INSTANCE_ID, SCHEDULER_ENABLED, SCHEDULER_MISFIRE_GRACE_TIME,
    SCHEDULER_OVERLAP_LOCK_TIMEOUT, SCHEDULER_TIMEZONE,
    SCHEDULER_LEADER_TTL, SCHEDULER_LEADER_RENEW_INTERVAL,
    SCHEDULER_LEADER_POLL_INTERVAL, SCHEDULER_LEADER_KEY,
    SCHEDULER_REFRESH_CHANNEL,
    EXECUTION_TIMEOUT_SECONDS, STUCK_EXECUTION_GRACE_SECONDS,
    MIN_CRON_INTERVAL_SECONDS, MAX_CRASH_RESUME_ATTEMPTS,
)

logger = logging.getLogger(__name__)

# Prefix all scheduler logs so they are easy to grep
_LOG_PREFIX = f"[Scheduler:{SCHEDULER_INSTANCE_ID}]"

# Lock renewal runs every N seconds (must be well under SCHEDULER_CRON_LOCK_TIMEOUT)
LOCK_RENEWAL_INTERVAL = max(SCHEDULER_CRON_LOCK_TIMEOUT // 3, 30)


# Common aliases users type that are not canonical IANA zone names.
_TZ_ALIASES = {
    "IST": "Asia/Kolkata",
}


def normalize_timezone(name: Optional[str]) -> str:
    """Canonicalize a user-supplied timezone string.

    Blank / None  -> ``"UTC"``
    ``"IST"``     -> ``"Asia/Kolkata"`` (case-insensitive alias)
    everything else is returned trimmed and otherwise unchanged (its validity is
    checked separately by ``zoneinfo``).
    """
    tz = (name or "").strip()
    if not tz:
        return "UTC"
    return _TZ_ALIASES.get(tz.upper(), tz)


def validate_cron_schedule(cron_expr: str, timezone: str = SCHEDULER_TIMEZONE) -> List[str]:
    """Validate a scheduled-workflow cron for DEPLOY. Returns a list of
    human-readable errors (empty = OK) so the caller can fail loud with a 400
    instead of deploying a workflow whose schedule silently never registers.

    Checks, in order:
      1. exactly 5 fields (minute hour day month day_of_week)
      2. a known timezone
      3. parseable by APScheduler (syntax)
      4. SAFETY CAP: does not fire more often than ``MIN_CRON_INTERVAL_SECONDS``
         — an unattended every-minute cron against a production source system
         is the runaway pattern this guards against.
    The frequency check is computed from the next several real fire times, so it
    correctly handles ``*/N`` steps and comma lists, not just the literal text.
    """
    errors: List[str] = []
    expr = (cron_expr or "").strip()
    if not expr:
        return ["A cron expression is required for a scheduled workflow."]
    parts = expr.split()
    if len(parts) != 5:
        return [
            f"Cron expression '{expr}' must have exactly 5 fields "
            f"(minute hour day month day_of_week)."
        ]

    # Use stdlib zoneinfo (APScheduler 3.11 dropped pytz, so pytz is no longer
    # installed; importing it raised ModuleNotFoundError which the broad except
    # turned into "Unknown timezone" for EVERY zone, including UTC). zoneinfo
    # reads the OS tzdata in the image, with the `tzdata` PyPI package as a
    # slim-image fallback.
    tz = None
    tz_name = normalize_timezone(timezone)
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(tz_name)
    except Exception:
        errors.append(f"Unknown timezone '{tz_name}'.")
        try:
            from zoneinfo import ZoneInfo
            tz = ZoneInfo("UTC")
        except Exception:
            tz = None

    try:
        from apscheduler.triggers.cron import CronTrigger
        trigger = CronTrigger(
            minute=parts[0], hour=parts[1], day=parts[2],
            month=parts[3], day_of_week=parts[4], timezone=tz,
        )
    except Exception as exc:
        errors.append(f"Invalid cron expression '{expr}': {exc}")
        return errors

    # Frequency cap — measure the smallest gap across the next several fires.
    try:
        from datetime import datetime, timedelta
        now = datetime.now(tz) if tz is not None else datetime.utcnow()
        prev, ref, fires = None, now, []
        for _ in range(12):
            nxt = trigger.get_next_fire_time(prev, ref)
            if nxt is None:
                break
            fires.append(nxt)
            prev = nxt
            ref = nxt + timedelta(microseconds=1)
        gaps = [(fires[i + 1] - fires[i]).total_seconds() for i in range(len(fires) - 1)]
        if gaps:
            min_gap = min(gaps)
            if min_gap < MIN_CRON_INTERVAL_SECONDS:
                errors.append(
                    f"Schedule fires too frequently (about every {int(min_gap)}s). "
                    f"The minimum allowed interval is {MIN_CRON_INTERVAL_SECONDS}s "
                    f"(~{MIN_CRON_INTERVAL_SECONDS // 60} min). Use a less frequent cron."
                )
    except Exception as exc:  # noqa: BLE001 — never block deploy on a check bug
        logger.warning("%s cron frequency check skipped for %r: %s", _LOG_PREFIX, expr, exc)

    return errors


def _get_cache():
    """Return the COORDINATION-tier manager (durable, noeviction Redis).

    The scheduler's leader-election, cron-fire, overlap, and approval locks are
    COORDINATION state, not cache. On the LRU cache tier an evicted (or
    restart-wiped) leader/cron lock can resurrect DUPLICATE scheduled runs — the
    exact thing leader election exists to prevent — so these must live on the
    durable noeviction tier (see citra_cache.get_coordination_manager)."""
    from citra_cache import get_coordination_manager
    return get_coordination_manager()


class WorkflowSchedulerManager:
    """Manages cron jobs for deployed workflows and approval timeouts.

    Uses Redis leader election so any replica can become the active scheduler.
    Only the elected leader runs APScheduler.  All instances participate in the
    election; followers automatically take over if the leader key expires.
    """

    def __init__(self):
        self._scheduler = None
        self._jobs: Dict[str, str] = {}  # workflow_id -> apscheduler job id
        self._started = False
        self._is_leader: bool = False
        self._started_at: Optional[float] = None  # monotonic timestamp of leadership start
        self._election_task: Optional[asyncio.Task] = None
        self._pubsub_task: Optional[asyncio.Task] = None  # strong ref — prevents GC of listener

    async def start(self):
        """Launch the leader election loop (non-blocking).

        All instances call this.  The internal election loop races for the
        Redis leader key; the winner starts APScheduler and registers cron
        jobs.  If SCHEDULER_ENABLED=false this instance is excluded from the
        election (global kill-switch).
        """
        if self._election_task is not None:
            return  # already running

        if not SCHEDULER_ENABLED:
            logger.info(f"{_LOG_PREFIX} ⚡ Scheduler excluded from election via SCHEDULER_ENABLED=false")
            return

        self._election_task = asyncio.create_task(self._run_election_loop())
        logger.info(
            f"{_LOG_PREFIX} 🗳️  Leader election started  |  "
            f"leader_ttl={SCHEDULER_LEADER_TTL}s  |  "
            f"renew_interval={SCHEDULER_LEADER_RENEW_INTERVAL}s  |  "
            f"poll_interval={SCHEDULER_LEADER_POLL_INTERVAL}s"
        )

    # ── Leader election core ─────────────────────────────────────────────────

    async def _run_election_loop(self):
        """Continuously try to win/hold the scheduler leader lock.

        Winning:  starts APScheduler + pub/sub listener.
        Losing:   sleeps SCHEDULER_LEADER_POLL_INTERVAL then retries.
        Lock lost mid-term: tears down scheduler, re-enters follower mode.
        """
        while True:
            try:
                if await self._try_acquire_leadership():
                    await self._on_become_leader()
                    await self._leader_heartbeat_loop()  # blocks until lock lost
                    await self._on_lose_leadership()
                else:
                    try:
                        current = _get_cache().get(SCHEDULER_LEADER_KEY)
                    except Exception:
                        logger.error(
                            f"{_LOG_PREFIX} leader-key read failed in follower "
                            f"poll; the lock cache may be down",
                            exc_info=True,
                        )
                        current = "unknown"
                    logger.debug(
                        f"{_LOG_PREFIX} ⏳ Follower mode  |  "
                        f"current_leader={current}  |  "
                        f"retry_in={SCHEDULER_LEADER_POLL_INTERVAL}s"
                    )
                    await asyncio.sleep(SCHEDULER_LEADER_POLL_INTERVAL)
            except asyncio.CancelledError:
                return
            except Exception as e:
                logger.error(f"{_LOG_PREFIX} ❌ Election loop error: {e}", exc_info=True)
                await asyncio.sleep(SCHEDULER_LEADER_POLL_INTERVAL)

    async def _try_acquire_leadership(self) -> bool:
        """Attempt to SET NX the leader key.  Returns True if we are now leader."""
        try:
            cache = _get_cache()
            if not cache.use_redis:
                # No Redis available — fall back to static SCHEDULER_ENABLED flag
                # so the designated container still works without Redis pub/sub.
                return SCHEDULER_ENABLED
            acquired = cache.set_nx(
                SCHEDULER_LEADER_KEY, SCHEDULER_INSTANCE_ID, ex=SCHEDULER_LEADER_TTL
            )
            if acquired:
                return True
            # Already the leader (e.g. brief Redis blip caused us to re-enter loop)
            current = cache.get(SCHEDULER_LEADER_KEY)
            return current == SCHEDULER_INSTANCE_ID
        except Exception as e:
            # Fail CLOSED. If Redis is configured but erroring, multiple
            # replicas may be running — falling back to the static flag
            # would let every one of them become leader and fire each cron
            # workflow N times. Declining leadership for this cycle is the
            # safe choice; leadership re-acquires once Redis recovers.
            logger.warning(
                f"{_LOG_PREFIX} ⚠️  Leadership election Redis error: {e} — "
                f"failing closed (declining leadership this cycle to avoid "
                f"duplicate cron fires across replicas)"
            )
            return False

    async def _leader_heartbeat_loop(self):
        """Renew the leader key every SCHEDULER_LEADER_RENEW_INTERVAL seconds.

        Returns when renewal fails (lock lost / Redis unreachable), signalling
        the caller to tear down APScheduler and re-enter the election loop.
        """
        _lua_renew = """
        if redis.call("get", KEYS[1]) == ARGV[1] then
            return redis.call("expire", KEYS[1], ARGV[2])
        else
            return 0
        end
        """
        while self._is_leader:
            await asyncio.sleep(SCHEDULER_LEADER_RENEW_INTERVAL)
            try:
                cache = _get_cache()
                result = cache._execute(
                    'eval', _lua_renew, 1,
                    cache._pk(SCHEDULER_LEADER_KEY),
                    SCHEDULER_INSTANCE_ID,
                    str(SCHEDULER_LEADER_TTL),
                )
                if result != 1:
                    logger.warning(
                        f"{_LOG_PREFIX} ⚠️  Lost scheduler leadership — "
                        "another instance took over or Redis key expired"
                    )
                    return
            except Exception as e:
                logger.warning(f"{_LOG_PREFIX} ⚠️  Leadership heartbeat error: {e}")
                return

    async def _on_become_leader(self):
        """Start APScheduler and load all cron schedules.  Called on every takeover."""
        import time as _time
        self._is_leader = True
        self._started_at = _time.monotonic()

        try:
            from apscheduler.schedulers.asyncio import AsyncIOScheduler

            self._scheduler = AsyncIOScheduler(
                timezone=SCHEDULER_TIMEZONE,
                job_defaults={
                    "coalesce": True,
                    "max_instances": 1,
                    "misfire_grace_time": SCHEDULER_MISFIRE_GRACE_TIME,
                },
            )
            self._scheduler.add_job(
                _check_approval_timeouts,
                "interval",
                minutes=APPROVAL_CHECK_INTERVAL_MIN,
                id="approval_timeout_checker",
                replace_existing=True,
            )
            self._scheduler.add_job(
                _check_stuck_executions,
                "interval",
                minutes=APPROVAL_CHECK_INTERVAL_MIN,
                id="stuck_execution_reaper",
                replace_existing=True,
            )
            self._scheduler.start()
            self._started = True

            await self._load_deployed_schedules()

            # Record in Redis so dashboards can see who the leader is
            try:
                _get_cache().set(
                    "scheduler:metrics:leader_since",
                    datetime.utcnow().isoformat(),
                    ex=7 * 24 * 3600,
                )
            except Exception:
                logger.error(
                    f"{_LOG_PREFIX} failed to write leader_since metric; "
                    f"leadership observability will be stale",
                    exc_info=True,
                )

            logger.info(
                f"{_LOG_PREFIX} 🏆 Became scheduler leader  |  "
                f"loaded={len(self._jobs)} job(s)  |  "
                f"approval_check={APPROVAL_CHECK_INTERVAL_MIN}m  |  "
                f"cron_lock_timeout={SCHEDULER_CRON_LOCK_TIMEOUT}s  |  "
                f"renewal_interval={LOCK_RENEWAL_INTERVAL}s  |  "
                f"refresh_channel={self._REFRESH_CHANNEL}"
            )

            # Keep a strong reference so the task is not silently garbage-collected.
            self._pubsub_task = asyncio.create_task(self.start_refresh_listener())

        except ImportError:
            logger.warning(
                f"{_LOG_PREFIX} APScheduler not installed — cron scheduling disabled. "
                "Install with: pip install apscheduler"
            )
        except Exception as e:
            logger.error(f"{_LOG_PREFIX} ❌ Failed to start as leader: {e}", exc_info=True)

    async def _on_lose_leadership(self):
        """Tear down APScheduler cleanly and enter follower mode."""
        self._is_leader = False
        # Cancel pub/sub listener task (it also checks _is_leader, but
        # cancellation ensures faster teardown on rapid leader transitions).
        if getattr(self, '_pubsub_task', None) is not None:
            self._pubsub_task.cancel()
            self._pubsub_task = None
        if self._scheduler and self._started:
            try:
                self._scheduler.shutdown(wait=False)
            except Exception:
                pass
        self._scheduler = None
        self._jobs.clear()
        self._started = False
        logger.warning(f"{_LOG_PREFIX} 🔄 Leadership lost — entering follower mode")

    async def _load_deployed_schedules(self):
        """On startup, load all deployed workflows with active cron schedules."""
        try:
            from citra_mongo import get_async_mongo_client, MONGODB_DATABASE
            client = get_async_mongo_client()
            db = client[MONGODB_DATABASE]

            cursor = db["Workflows"].find({
                "status": "deployed",
                "is_active": True,
                "schedule.enabled": True,
                "schedule.cron_expression": {"$ne": None},
            }, {"workflow_id": 1, "user_id": 1, "name": 1, "schedule": 1})

            count = 0
            async for doc in cursor:
                schedule = doc.get("schedule", {})
                self.register_workflow(
                    doc["workflow_id"],
                    doc["user_id"],
                    schedule,
                    workflow_name=doc.get("name", "Unknown"),
                )
                count += 1

            logger.info(f"{_LOG_PREFIX} 📋 Loaded {count} deployed cron schedule(s) from DB")
        except Exception as e:
            logger.error(f"{_LOG_PREFIX} ❌ Failed to load deployed schedules: {e}", exc_info=True)

    def register_workflow(
        self,
        workflow_id: str,
        user_id: str,
        schedule: dict,
        workflow_name: str = "",
    ):
        """Register a cron job for a deployed workflow."""
        if not self._scheduler:
            logger.debug(f"{_LOG_PREFIX} Scheduler not active on this replica — skipping cron for {workflow_id}")
            return

        from apscheduler.triggers.cron import CronTrigger

        cron_expr = schedule.get("cron_expression")
        timezone = schedule.get("timezone", SCHEDULER_TIMEZONE)
        if not cron_expr:
            return

        job_id = f"wf_cron_{workflow_id}"

        # Remove existing job if any (idempotent registration)
        self.unregister_workflow(workflow_id)

        try:
            # Defense-in-depth: deploy already validates, but the cross-replica
            # schedule-refresh path also lands here, so re-validate (syntax +
            # the safe frequency cap) and refuse to register anything abusive.
            cron_errors = validate_cron_schedule(cron_expr, timezone)
            if cron_errors:
                logger.error(
                    f"{_LOG_PREFIX} ❌ Refusing to register cron for workflow={workflow_id} "
                    f"name='{workflow_name}': {' '.join(cron_errors)}"
                )
                return

            # Parse cron expression: "minute hour day month day_of_week"
            parts = cron_expr.strip().split()

            # Validate + canonicalize timezone (blank -> UTC, IST -> Asia/Kolkata)
            timezone = normalize_timezone(timezone)
            try:
                from zoneinfo import ZoneInfo
                tz = ZoneInfo(timezone)
            except Exception:
                logger.warning(
                    f"{_LOG_PREFIX} ⚠️ Invalid timezone '{timezone}' for workflow={workflow_id} — falling back to UTC"
                )
                timezone = "UTC"
                from zoneinfo import ZoneInfo
                tz = ZoneInfo("UTC")

            trigger = CronTrigger(
                minute=parts[0],
                hour=parts[1],
                day=parts[2],
                month=parts[3],
                day_of_week=parts[4],
                timezone=tz,
            )

            self._scheduler.add_job(
                _execute_scheduled_workflow,
                trigger,
                args=[workflow_id, user_id, workflow_name],
                id=job_id,
                replace_existing=True,
            )
            self._jobs[workflow_id] = job_id

            # Log next run time
            job = self._scheduler.get_job(job_id)
            next_run = job.next_run_time if job else "unknown"
            logger.info(
                f"{_LOG_PREFIX} ✅ Registered cron  |  "
                f"workflow={workflow_id}  |  name='{workflow_name}'  |  "
                f"cron='{cron_expr}'  |  tz={timezone}  |  next_run={next_run}"
            )

        except Exception as e:
            logger.error(
                f"{_LOG_PREFIX} ❌ Failed to register cron for workflow={workflow_id}: {e}"
            )

    def unregister_workflow(self, workflow_id: str):
        """Remove the cron job for a workflow."""
        job_id = self._jobs.pop(workflow_id, None)
        if job_id and self._scheduler:
            try:
                self._scheduler.remove_job(job_id)
                logger.info(f"{_LOG_PREFIX} 🗑️ Unregistered cron for workflow={workflow_id}")
            except Exception:
                pass  # Job may not exist

    def get_registered_jobs(self) -> Dict[str, str]:
        """Return dict of workflow_id -> apscheduler job_id for diagnostics."""
        return dict(self._jobs)

    def shutdown(self):
        """Shut down the scheduler gracefully."""
        if self._scheduler and self._started:
            logger.info(f"{_LOG_PREFIX} ⏹️ Shutting down scheduler...")
            self._scheduler.shutdown(wait=False)
            self._started = False
            logger.info(f"{_LOG_PREFIX} ⏹️ Scheduler shutdown complete")

    # ── Redis pub/sub for schedule refresh ──────────────────────────
    # Channel name is scoped by APP_ENV (prod:/test:/dev:) so that PROD and
    # TEST containers sharing the same Redis server do not cross-deliver events.
    # Redis pub/sub is server-global — DB number does NOT isolate channels.

    _REFRESH_CHANNEL = SCHEDULER_REFRESH_CHANNEL

    @staticmethod
    def _make_async_redis():
        """Create an async Redis client using the same env-var config as CacheManager."""
        import os
        try:
            from redis import asyncio as aioredis
        except ImportError:
            import aioredis  # fallback for older aioredis package

        params = {
            "host": os.getenv("REDIS_HOST", "localhost"),
            "port": int(os.getenv("REDIS_PORT", "6379")),
            "db": int(os.getenv("REDIS_DB", "0")),
            "socket_connect_timeout": int(os.getenv("REDIS_CONNECT_TIMEOUT", "5")),
            "socket_timeout": int(os.getenv("REDIS_SOCKET_TIMEOUT", "5")),
            "decode_responses": True,
        }
        redis_username = os.getenv("REDIS_USERNAME")
        if redis_username:
            params["username"] = redis_username
        redis_password = os.getenv("REDIS_PASSWORD")
        if redis_password:
            params["password"] = redis_password
        return aioredis.Redis(**params)

    async def start_refresh_listener(self):
        """Listen for schedule-refresh pub/sub messages from any replica.

        Wraps the subscription in a reconnect loop so transient Redis
        disconnections (network blip, failover) do not permanently silence
        hot-reload.  Exits cleanly when this instance loses leadership.
        """
        if not self._scheduler:
            return  # Only the active leader listens

        logger.info(f"{_LOG_PREFIX} 🔄 Pub/sub listener starting on channel '{self._REFRESH_CHANNEL}'")
        while self._is_leader:
            async_redis = None
            try:
                async_redis = self._make_async_redis()
                await async_redis.ping()
                pubsub = async_redis.pubsub()
                await pubsub.subscribe(self._REFRESH_CHANNEL)
                logger.info(
                    f"{_LOG_PREFIX} 🔄 Listening for schedule refresh events on "
                    f"Redis channel '{self._REFRESH_CHANNEL}'"
                )

                async for message in pubsub.listen():
                    if not self._is_leader:
                        break
                    if message["type"] != "message":
                        continue
                    try:
                        payload = json.loads(message["data"])
                        wf_id = payload["workflow_id"]
                        action = payload.get("action", "register")
                        if action == "unregister":
                            self.unregister_workflow(wf_id)
                            logger.info(f"{_LOG_PREFIX} 🔄 Refresh: unregistered cron for {wf_id}")
                        else:
                            schedule = payload.get("schedule", {})
                            user_id = payload.get("user_id", "")
                            name = payload.get("workflow_name", "")
                            self.register_workflow(wf_id, user_id, schedule, workflow_name=name)
                            logger.info(f"{_LOG_PREFIX} 🔄 Refresh: registered cron for {wf_id}")
                    except Exception as e:
                        logger.warning(f"{_LOG_PREFIX} 🔄 Refresh listener parse error: {e}")

            except asyncio.CancelledError:
                return
            except Exception as e:
                if not self._is_leader:
                    return
                logger.warning(
                    f"{_LOG_PREFIX} 🔄 Pub/sub connection error, reconnecting in 5s: {e}"
                )
                await asyncio.sleep(5)
            finally:
                if async_redis is not None:
                    try:
                        await async_redis.aclose()
                    except Exception:
                        pass

    @staticmethod
    async def publish_schedule_refresh(workflow_id: str, user_id: str, schedule: dict,
                                       workflow_name: str = "", action: str = "register"):
        """Publish a schedule refresh event so the scheduler replica picks it up."""
        try:
            async_redis = WorkflowSchedulerManager._make_async_redis()
            payload = json.dumps({
                "workflow_id": workflow_id,
                "user_id": user_id,
                "schedule": schedule,
                "workflow_name": workflow_name,
                "action": action,
            })
            await async_redis.publish(WorkflowSchedulerManager._REFRESH_CHANNEL, payload)
            await async_redis.aclose()
            logger.debug(f"{_LOG_PREFIX} 🔄 Published schedule refresh: action={action} workflow={workflow_id}")
        except Exception as e:
            logger.warning(f"{_LOG_PREFIX} 🔄 Could not publish schedule refresh: {e}")
            # Best-effort — scheduler will pick up on next restart from DB


# ── Lock renewal heartbeat ──────────────────────────────────────────

async def _lock_renewal_loop(
    cache,
    keys_and_values: list,
    ttl: int,
    interval: int,
    stop_event: asyncio.Event,
    workflow_id: str,
):
    """Background task that extends Redis locks while a workflow runs.

    Args:
        cache:           CacheManager singleton.
        keys_and_values: List of (lock_key, lock_value) tuples to renew.
        ttl:             Seconds to extend each key per renewal.
        interval:        Seconds between renewals.
        stop_event:      Set by the caller to terminate this loop.
        workflow_id:     For logging only.
    """
    renewals = 0
    try:
        while not stop_event.is_set():
            await asyncio.sleep(interval)
            if stop_event.is_set():
                break
            for lock_key, lock_value in keys_and_values:
                try:
                    # Atomically extend TTL only if we still own the lock
                    lua = """
                    if redis.call("get", KEYS[1]) == ARGV[1] then
                        return redis.call("expire", KEYS[1], ARGV[2])
                    else
                        return 0
                    end
                    """
                    result = cache._execute('eval', lua, 1, cache._pk(lock_key), lock_value, str(ttl))
                    if result == 1:
                        renewals += 1
                    else:
                        logger.warning(
                            f"{_LOG_PREFIX} ⚠️ Lock renewal LOST ownership  |  "
                            f"key={lock_key}  |  workflow={workflow_id}"
                        )
                except Exception as e:
                    logger.warning(
                        f"{_LOG_PREFIX} ⚠️ Lock renewal failed  |  "
                        f"key={lock_key}  |  error={e}"
                    )
    except asyncio.CancelledError:
        pass
    finally:
        if renewals > 0:
            logger.info(
                f"{_LOG_PREFIX} 🔄 Lock renewal stopped  |  workflow={workflow_id}  |  "
                f"total_renewals={renewals}"
            )


# ── Scheduled job functions ─────────────────────────────────────────

async def _execute_scheduled_workflow(
    workflow_id: str,
    user_id: str,
    workflow_name: str = "",
):
    """Execute a deployed workflow on its cron schedule.

    Uses Redis SET NX EX (async-safe) so only one instance runs per cron
    window. The lock key includes minute-resolution timestamp so each fire
    gets its own lock, and the next fire isn't blocked.

    A background heartbeat extends the lock while the workflow runs,
    preventing premature expiry for long-running workflows.
    """
    cache = _get_cache()

    fire_ts = datetime.utcnow().strftime("%Y%m%dT%H%M")
    lock_key = f"lock:wf_cron:{workflow_id}:{fire_ts}"
    lock_value = f"{SCHEDULER_INSTANCE_ID}:{uuid.uuid4().hex[:8]}"

    logger.info(
        f"{_LOG_PREFIX} ⏰ Cron fired  |  workflow={workflow_id}  |  "
        f"name='{workflow_name}'  |  fire_ts={fire_ts}  |  "
        f"attempting lock={lock_key}"
    )

    # Async-safe lock acquisition: SET NX EX
    try:
        acquired = cache.set_nx(lock_key, lock_value, ex=SCHEDULER_CRON_LOCK_TIMEOUT)
    except Exception as e:
        logger.error(
            f"{_LOG_PREFIX} ❌ Redis lock failed for workflow={workflow_id}: {e}  |  "
            f"Skipping execution to avoid duplicates"
        )
        return

    if not acquired:
        # Another instance already claimed this cron fire
        try:
            owner = cache.get(lock_key)
        except Exception:
            owner = "unknown"
        logger.info(
            f"{_LOG_PREFIX} 🔒 Lock REJECTED  |  workflow={workflow_id}  |  "
            f"name='{workflow_name}'  |  fire_ts={fire_ts}  |  "
            f"owner={owner}  |  this_instance={SCHEDULER_INSTANCE_ID}"
        )
        return

    logger.info(
        f"{_LOG_PREFIX} ✅ Lock ACQUIRED  |  workflow={workflow_id}  |  "
        f"name='{workflow_name}'  |  fire_ts={fire_ts}  |  "
        f"lock_holder={lock_value}"
    )

    # ── Overlap prevention: check if this workflow already has a running execution
    overlap_key = f"lock:wf_running:{workflow_id}"
    overlap_acquired = False
    try:
        overlap_acquired = cache.set_nx(overlap_key, lock_value, ex=SCHEDULER_OVERLAP_LOCK_TIMEOUT)
        if not overlap_acquired:
            logger.warning(
                f"{_LOG_PREFIX} ⚠️ Overlap SKIPPED  |  workflow={workflow_id}  |  "
                f"name='{workflow_name}'  |  Previous execution still running"
            )
            return
    except Exception as e:
        logger.warning(f"{_LOG_PREFIX} ⚠️ Overlap check failed (proceeding): {e}")

    # ── Start lock renewal heartbeat ─────────────────────────────
    stop_renewal = asyncio.Event()
    renewal_keys = [(lock_key, lock_value)]
    if overlap_acquired:
        renewal_keys.append((overlap_key, lock_value))

    renewal_task = asyncio.create_task(
        _lock_renewal_loop(
            cache=cache,
            keys_and_values=renewal_keys,
            ttl=SCHEDULER_CRON_LOCK_TIMEOUT,
            interval=LOCK_RENEWAL_INTERVAL,
            stop_event=stop_renewal,
            workflow_id=workflow_id,
        )
    )

    db = None
    doc = None
    try:
        from citra_mongo import get_async_mongo_client, MONGODB_DATABASE
        client = get_async_mongo_client()
        db = client[MONGODB_DATABASE]

        doc = await db["Workflows"].find_one({
            "workflow_id": workflow_id,
            "user_id": user_id,
            "status": "deployed",
            "is_active": True,
        })
        if not doc:
            logger.warning(
                f"{_LOG_PREFIX} ⚠️ Workflow {workflow_id} not found/not deployed/not active — skipping  |  "
                f"Unregistering stale cron job"
            )
            # Self-heal: unregister the stale APScheduler job on this replica
            # Use module-level singleton directly — avoids circular import.
            scheduler_manager.unregister_workflow(workflow_id)
            return

        # Check if schedule is still enabled (another replica may have disabled it)
        schedule = doc.get("schedule", {})
        if not schedule.get("enabled", False):
            logger.info(
                f"{_LOG_PREFIX} ⏭️ Schedule DISABLED  |  workflow={workflow_id}  |  "
                f"name='{workflow_name}'  |  Unregistering stale cron job"
            )
            # Self-heal: unregister the stale APScheduler job on this replica
            scheduler_manager.unregister_workflow(workflow_id)
            return

        doc.pop("_id", None)
        from .models import WorkflowDefinition
        workflow = WorkflowDefinition(**doc)

        logger.info(
            f"{_LOG_PREFIX} 🚀 Dispatching execution  |  workflow={workflow_id}  |  "
            f"name='{workflow_name}'"
        )

        # Mint a short-lived org-scoped system JWT so AI agent nodes can
        # exchange it for a dept-MCP service token (per-source visibility
        # filtering). The token carries the workflow's org_id/dept_ids
        # directly so the run is independent of the original author —
        # works even after the author leaves the org.
        try:
            from citra_auth import mint_workflow_org_token
            system_jwt = mint_workflow_org_token(
                workflow_id=workflow.workflow_id,
                org_id=workflow.org_id,
                dept_ids=workflow.dept_ids,
                author_email=getattr(workflow, "author_email", None) or workflow.user_id,
            )
        except Exception as exc:
            logger.warning(
                f"{_LOG_PREFIX} ⚠️ Failed to mint org JWT for scheduled run "
                f"(non-fatal, dept-MCP filtering degrades): {exc}"
            )
            system_jwt = None

        # Resolve connections against the workflow's AUTHORITATIVE run
        # environment — NOT the executor's "test" default. The scheduler only
        # fires for DEPLOYED workflows, so an unmigrated doc with no field is
        # prod by definition (prod-readiness BLOCKER #1).
        run_env = getattr(workflow, "run_environment", None) or "prod"

        # Decoupled-overlap guard: now that the run executes in the worker (not
        # inline here), the short-lived overlap LOCK above can't span it. So
        # also skip this tick if a prior run of THIS workflow is still in flight
        # (queued/running/resuming/paused) — don't pile a concurrent run on.
        inflight = await db["WorkflowExecutions"].find_one(
            {"workflow_id": workflow_id,
             "status": {"$in": ["queued", "running", "resuming", "paused"]}},
            {"execution_id": 1},
        )
        if inflight:
            logger.warning(
                f"{_LOG_PREFIX} ⏭️ Overlap SKIPPED  |  workflow={workflow_id}  |  "
                f"prior run {inflight.get('execution_id')} still in flight"
            )
            return

        # Kill switch: skip the enqueue if a halt covers this workflow's scope
        # (global / org / dept). The scheduled run simply doesn't fire until an
        # admin resumes it from the Workflow Automation Control console.
        try:
            from . import workflow_control
            _wf_org = getattr(workflow, "org_id", "") or ""
            _dept_tokens = [f"{_wf_org}:{str(d).lower()}" for d in (getattr(workflow, "dept_ids", None) or [])]
            _halt = await workflow_control.get_halt(
                db["workflow_control"], org_id=_wf_org, dept_tokens=_dept_tokens
            )
        except Exception:
            _halt = None
        if _halt:
            logger.warning(
                f"{_LOG_PREFIX} ⛔ HALTED  |  workflow={workflow_id}  |  "
                f"scope={_halt.get('scope_type')} — scheduled run skipped"
            )
            return

        # Route the scheduled run through the DURABLE QUEUE — the same path as
        # manual/webhook triggers — instead of executing inline in the
        # scheduler loop. This keeps a long run from blocking the scheduler, and
        # a worker crash mid-run is recovered by the Streams reclaim sweep + the
        # stuck-execution reaper (which crash-resumes from the last checkpoint).
        import uuid as _uuid
        execution_id = str(_uuid.uuid4())
        trigger_data = {
            "trigger": "scheduled",
            "fire_time": fire_ts,
            "instance_id": SCHEDULER_INSTANCE_ID,
        }
        await db["WorkflowExecutions"].insert_one({
            "execution_id": execution_id,
            "workflow_id": workflow_id,
            "user_id": user_id,
            "status": "queued",
            "trigger_data": trigger_data,
            "trigger_type": "scheduled",
            "environment": run_env,
            "started_at": datetime.utcnow(),
            "node_results": {},
            "current_node": None,
            "error": None,
        })
        from citra_queue import enqueue as _wq_enqueue
        job_id = _wq_enqueue("workflow.run", {
            "workflow_id": workflow_id,
            "user_id": user_id,
            "trigger_data": trigger_data,
            "execution_id": execution_id,
            "environment": run_env,
            "jwt_token": system_jwt,
        }, tenant_id=user_id, request_id=execution_id)

        logger.info(
            f"{_LOG_PREFIX} ✅ Execution ENQUEUED  |  workflow={workflow_id}  |  "
            f"name='{workflow_name}'  |  execution_id={execution_id}  |  job={job_id}"
        )
        # Observability: record last fire + that we dispatched. The run's actual
        # outcome lives on the WorkflowExecutions doc (status), not here.
        try:
            _ttl = 7 * 24 * 3600
            cache.set(f"scheduler:metrics:{workflow_id}:last_fire", fire_ts, ex=_ttl)
            cache.set(f"scheduler:metrics:{workflow_id}:last_status", "enqueued", ex=_ttl)
            _cnt_key = cache._pk(f"scheduler:metrics:{workflow_id}:fire_count")
            cache._execute('incr', _cnt_key)
        except Exception:
            logger.error(
                f"{_LOG_PREFIX} failed to write scheduler ENQUEUE metrics for "
                f"workflow={workflow_id}; dashboards/alerts will be stale",
                exc_info=True,
            )

    except Exception as e:
        logger.error(
            f"{_LOG_PREFIX} ❌ Execution FAILED  |  workflow={workflow_id}  |  "
            f"name='{workflow_name}'  |  error={e}",
            exc_info=True,
        )
        # Observability: record failure in Redis
        try:
            _ttl = 7 * 24 * 3600
            cache.set(f"scheduler:metrics:{workflow_id}:last_fire", fire_ts, ex=_ttl)
            cache.set(f"scheduler:metrics:{workflow_id}:last_status", "failed", ex=_ttl)
            _cnt_key = cache._pk(f"scheduler:metrics:{workflow_id}:fire_count")
            cache._execute('incr', _cnt_key)
        except Exception:
            logger.error(
                f"{_LOG_PREFIX} failed to write scheduler FAILURE metrics for "
                f"workflow={workflow_id}; dashboards/alerts will be stale",
                exc_info=True,
            )

        # Send failure notification for unhandled scheduler-level errors
        try:
            if db is not None:
                user_doc = await db["users"].find_one(
                    {"uid": user_id}, {"email": 1, "displayName": 1, "name": 1}
                )
                if user_doc and user_doc.get("email"):
                    from .notifications import send_execution_failure_notification
                    await send_execution_failure_notification(
                        to_email=user_doc["email"],
                        user_name=user_doc.get("displayName") or user_doc.get("name", ""),
                        workflow_name=doc.get("name", "Unknown") if doc else workflow_name,
                        execution_id="N/A",
                        failed_node_label="Scheduler",
                        error_message=str(e),
                    )
        except Exception as notify_err:
            logger.error(f"{_LOG_PREFIX} ❌ Notification send failed: {notify_err}")

    finally:
        # Stop the lock renewal heartbeat
        stop_renewal.set()
        renewal_task.cancel()
        try:
            await renewal_task
        except (asyncio.CancelledError, Exception):
            pass

        # Release the overlap lock on completion (success or failure)
        if overlap_acquired:
            try:
                cache.delete_if_owner(overlap_key, lock_value)
            except Exception:
                pass
        # NOTE: We do NOT release the cron fire lock (lock_key).
        # It expires naturally via TTL, preventing any other instance
        # from re-running the same cron window.


async def _check_stuck_executions():
    """Reap executions abandoned by a crashed worker.

    A workflow run is hard-bounded by EXECUTION_TIMEOUT_SECONDS via
    ``asyncio.wait_for``. So any execution still in ``running``/``queued``
    well past ``started_at + EXECUTION_TIMEOUT + grace`` can only be there
    because the worker process died before writing a terminal status.
    Without this sweep such executions sit ``running`` forever — the UI
    polls them indefinitely and the cron overlap lock stays held.

    Redis-locked so only one replica runs the sweep per interval.
    """
    cache = _get_cache()
    epoch_min = int(datetime.utcnow().timestamp()) // 60
    bucket = (epoch_min // APPROVAL_CHECK_INTERVAL_MIN) * APPROVAL_CHECK_INTERVAL_MIN
    lock_key = f"lock:stuck_execution_check:{bucket}"
    lock_value = f"{SCHEDULER_INSTANCE_ID}:{uuid.uuid4().hex[:8]}"
    try:
        acquired = cache.set_nx(lock_key, lock_value, ex=SCHEDULER_APPROVAL_LOCK_TIMEOUT)
    except Exception:
        # Fail loud (visible): if the lock cache is unreachable the stuck-exec
        # sweep is skipped this cycle. Log it so monitoring sees the gap rather
        # than a silent no-op that hides a Redis outage (stuck executions would
        # then never get reaped).
        logger.error(
            f"{_LOG_PREFIX} lock acquire failed for {lock_key!r}; "
            f"skipping stuck-execution sweep this cycle",
            exc_info=True,
        )
        return
    if not acquired:
        return

    try:
        from citra_mongo import get_async_mongo_client, MONGODB_DATABASE
        client = get_async_mongo_client()
        db = client[MONGODB_DATABASE]

        now = datetime.utcnow()
        grace_seconds = EXECUTION_TIMEOUT_SECONDS + STUCK_EXECUTION_GRACE_SECONDS
        cutoff = now - timedelta(seconds=grace_seconds)

        # running/queued are reaped by ``started_at``. ``resuming`` is reaped by
        # ``resumed_at`` — an execution can sit paused for hours awaiting an
        # approval, so its ``started_at`` is not a meaningful cutoff; before
        # this, a worker crash mid-resume left the run stuck in ``resuming``
        # FOREVER with no sweep and no failure email (prod-readiness HIGH #9).
        # ``paused`` is intentionally excluded — that's the approval-timeout
        # sweep's job.
        abandoned_error = (
            "Execution abandoned — the worker did not report completion within "
            "the timeout window (likely a worker crash or restart mid-run)."
        )
        stuck_query = {
            "$or": [
                {"status": {"$in": ["running", "queued"]}, "started_at": {"$lt": cutoff}},
                {"status": "resuming", "resumed_at": {"$lt": cutoff}},
            ]
        }

        # Reap one doc at a time with a compare-and-set on its current status so
        # we only fire a failure email for the docs THIS sweep actually
        # transitioned to failed — never a duplicate if the worker happened to
        # write a terminal status between our read and our write. The sweep is
        # already Redis-locked to a single replica per interval.
        reaped = 0
        resumed = 0
        cancelled = 0
        cursor = db["WorkflowExecutions"].find(
            stuck_query,
            {"execution_id": 1, "workflow_id": 1, "user_id": 1, "status": 1,
             "current_node": 1, "crash_resume_count": 1, "trigger_type": 1,
             "cancel_requested": 1, "cancel_requested_by": 1},
        )
        async for ex_doc in cursor:
            exec_id = ex_doc.get("execution_id")
            attempts = int(ex_doc.get("crash_resume_count") or 0)

            # ── Cancellation wins over resume: an operator pressed Cancel but the
            # worker died (or the Redis flag expired) before the run stopped at a
            # node boundary. Finalise it as CANCELLED rather than crash-resuming,
            # so a cancelled run can never come back to life. ──
            if ex_doc.get("cancel_requested"):
                cas = await db["WorkflowExecutions"].update_one(
                    {"execution_id": exec_id, "status": ex_doc.get("status")},
                    {"$set": {
                        "status": "cancelled",
                        "error": "Execution cancelled by user",
                        "completed_at": now,
                        "cancelled_by": ex_doc.get("cancel_requested_by"),
                        "updated_at": now.isoformat(),
                    }},
                )
                if getattr(cas, "modified_count", 0) == 1:
                    cancelled += 1
                    logger.warning(
                        f"{_LOG_PREFIX} ⛔ Finalised abandoned-but-cancelled "
                        f"execution {exec_id} as CANCELLED (not resuming)",
                    )
                continue

            # ── Assistant test runs are interactive throwaways: the author
            # already saw the outcome (or a timeout message) in the chat, and
            # the definition may exist ONLY in the job payload (unsaved
            # working copy) — crash-resuming would enqueue workflow.resume
            # for a workflow_id that may not be in Workflows at all, which
            # permanently fails on the worker. Finalise as failed; no resume,
            # no owner email. ──
            if ex_doc.get("trigger_type") == "assistant_test":
                cas = await db["WorkflowExecutions"].update_one(
                    {"execution_id": exec_id, "status": ex_doc.get("status")},
                    {"$set": {
                        "status": "failed",
                        "error": abandoned_error,
                        "completed_at": now,
                        "updated_at": now.isoformat(),
                    }},
                )
                if getattr(cas, "modified_count", 0) == 1:
                    reaped += 1
                    logger.warning(
                        f"{_LOG_PREFIX} 🧹 Finalised abandoned assistant test "
                        f"run {exec_id} as FAILED (no crash-resume for "
                        "assistant_test executions)",
                    )
                continue

            # ── Crash-RESUME first (durability): the execution checkpoints its
            # node_results + variables per node, so instead of just failing an
            # abandoned run we re-enqueue a resume that restarts from the last
            # completed node. Bounded by MAX_CRASH_RESUME_ATTEMPTS so a poison
            # run that kills the worker on the same node can't loop forever.
            if attempts < MAX_CRASH_RESUME_ATTEMPTS:
                cas = await db["WorkflowExecutions"].update_one(
                    {"execution_id": exec_id, "status": ex_doc.get("status")},
                    {"$set": {
                        "status": "resuming",
                        "resumed_at": now,
                        "resume_decision": "crash_resume",
                        "updated_at": now.isoformat(),
                    },
                     "$inc": {"crash_resume_count": 1}},
                )
                if getattr(cas, "modified_count", 0) != 1:
                    continue  # raced — another writer finalized it
                try:
                    from citra_queue import enqueue as _wq_enqueue  # type: ignore
                    _wq_enqueue("workflow.resume", {
                        "execution_id": exec_id,
                        "workflow_id": ex_doc.get("workflow_id"),
                        "user_id": ex_doc.get("user_id"),
                        "approved": True,
                    }, tenant_id=ex_doc.get("user_id") or "", request_id=exec_id)
                    resumed += 1
                    logger.warning(
                        f"{_LOG_PREFIX} ♻️  Crash-resuming abandoned execution "
                        f"{exec_id} (attempt {attempts + 1}/{MAX_CRASH_RESUME_ATTEMPTS}) "
                        f"from its last checkpoint",
                    )
                except Exception as qe:  # noqa: BLE001
                    # Queue unavailable — roll back so the next sweep retries.
                    logger.error(
                        f"{_LOG_PREFIX} crash-resume enqueue failed for {exec_id}: {qe}",
                        exc_info=True,
                    )
                    await db["WorkflowExecutions"].update_one(
                        {"execution_id": exec_id},
                        {"$set": {"status": ex_doc.get("status")},
                         "$inc": {"crash_resume_count": -1}},
                    )
                continue

            # ── Cap reached → give up and FAIL (fail-loud, notify the owner). ──
            cas = await db["WorkflowExecutions"].update_one(
                {"execution_id": exec_id, "status": ex_doc.get("status")},
                {"$set": {
                    "status": "failed",
                    "error": abandoned_error,
                    "completed_at": now,
                    "updated_at": now.isoformat(),
                }},
            )
            if getattr(cas, "modified_count", 0) != 1:
                continue  # raced — another writer finalized it; don't notify
            reaped += 1
            # Fail loud: the owning BA must HEAR that an unattended run was
            # abandoned, not just discover it in the Workflows tab later
            # (closes the silent-drop symptom of prod-readiness HIGH #4).
            try:
                await _notify_reaped_failure(db, ex_doc, abandoned_error)
            except Exception as ne:  # noqa: BLE001
                logger.error(
                    f"{_LOG_PREFIX} reaped execution {exec_id} "
                    f"but failure notification failed: {ne}",
                    exc_info=True,
                )

        if resumed or reaped or cancelled:
            logger.warning(
                f"{_LOG_PREFIX} 🧹 Stuck-execution sweep: {resumed} crash-resumed, "
                f"{cancelled} cancelled (operator-requested), "
                f"{reaped} failed after exhausting {MAX_CRASH_RESUME_ATTEMPTS} "
                f"resume attempt(s) — owners notified on failure"
            )
    except Exception as e:
        logger.warning(f"{_LOG_PREFIX} Stuck-execution check failed: {e}")


async def _notify_reaped_failure(db, ex_doc: dict, error_message: str) -> None:
    """Send the standard owner failure email for a reaper-abandoned execution.

    Reuses the executor's ``_notify_failure`` so recipient resolution and the
    per-workflow ``notify_on_failure`` switch behave exactly as for a normal
    in-run failure — no duplicated notification policy. Best-effort; the caller
    logs if this raises.
    """
    wf_doc = await db["Workflows"].find_one({"workflow_id": ex_doc.get("workflow_id")})
    if not wf_doc:
        return
    wf_doc.pop("_id", None)
    from .models import WorkflowDefinition, WorkflowExecution
    from .executor import WorkflowExecutor
    workflow = WorkflowDefinition(**wf_doc)
    execution = WorkflowExecution(
        execution_id=ex_doc.get("execution_id"),
        workflow_id=ex_doc.get("workflow_id") or "",
        user_id=ex_doc.get("user_id") or "",
    )
    await WorkflowExecutor()._notify_failure(
        execution,
        workflow,
        failed_node_label=ex_doc.get("current_node") or "(abandoned run)",
        error_message=error_message,
    )


async def _check_approval_timeouts():
    """Check for expired approvals and auto-reject them.

    Uses a Redis distributed lock so only one instance runs the sweep
    per check interval.
    """
    cache = _get_cache()

    # Bucket by check interval to prevent minute-boundary races.
    # All replicas firing within the same interval window get the same key.
    epoch_min = int(datetime.utcnow().timestamp()) // 60
    bucket = (epoch_min // APPROVAL_CHECK_INTERVAL_MIN) * APPROVAL_CHECK_INTERVAL_MIN
    lock_key = f"lock:approval_timeout_check:{bucket}"
    lock_value = f"{SCHEDULER_INSTANCE_ID}:{uuid.uuid4().hex[:8]}"

    try:
        acquired = cache.set_nx(lock_key, lock_value, ex=SCHEDULER_APPROVAL_LOCK_TIMEOUT)
    except Exception:
        # Fail loud (visible): a lock-cache outage means expired approvals are
        # not swept this cycle. Log it instead of silently skipping so the gap
        # is observable.
        logger.error(
            f"{_LOG_PREFIX} lock acquire failed for {lock_key!r}; "
            f"skipping approval-timeout sweep this cycle",
            exc_info=True,
        )
        return

    if not acquired:
        return

    logger.debug(f"{_LOG_PREFIX} Running approval timeout check (bucket={bucket})")

    try:
        from citra_mongo import get_async_mongo_client, MONGODB_DATABASE
        client = get_async_mongo_client()
        db = client[MONGODB_DATABASE]

        now = datetime.utcnow()

        # Find pending approvals that have expired
        cursor = db["WorkflowApprovals"].find({
            "resolution": None,
        })

        expired_count = 0
        async for approval in cursor:
            created = approval.get("created_at")
            timeout_hours = approval.get("timeout_hours", DEFAULT_APPROVAL_TIMEOUT_HOURS)
            if not created:
                continue

            if isinstance(created, str):
                created = datetime.fromisoformat(created)

            deadline = created + timedelta(hours=timeout_hours)
            if now < deadline:
                continue

            # This approval has expired — auto-reject
            execution_id = approval["execution_id"]
            workflow_id = approval["workflow_id"]
            user_id = approval["user_id"]

            # Update approval record
            await db["WorkflowApprovals"].update_one(
                {"approval_id": approval["approval_id"]},
                {"$set": {
                    "resolution": "timed_out",
                    "resolved_at": now,
                }},
            )

            # Fail the execution
            await db["WorkflowExecutions"].update_one(
                {"execution_id": execution_id, "status": "paused"},
                {"$set": {
                    "status": "failed",
                    "error": f"Approval timed out after {timeout_hours} hours",
                    "completed_at": now,
                }},
            )

            # Send timeout notification
            try:
                user_doc = await db["users"].find_one(
                    {"uid": user_id}, {"email": 1, "displayName": 1, "name": 1}
                )
                if user_doc and user_doc.get("email"):
                    from .notifications import send_approval_timeout_notification
                    await send_approval_timeout_notification(
                        to_email=user_doc["email"],
                        user_name=user_doc.get("displayName") or user_doc.get("name", ""),
                        workflow_name=approval.get("workflow_name", "Unknown"),
                        node_label=approval.get("node_label", "Unknown"),
                        execution_id=execution_id,
                    )
            except Exception as e:
                logger.error(f"{_LOG_PREFIX} ❌ Timeout notification failed: {e}")

            expired_count += 1

        if expired_count:
            logger.info(f"{_LOG_PREFIX} Auto-rejected {expired_count} timed-out approval(s)")

    except Exception as e:
        logger.error(f"{_LOG_PREFIX} ❌ Approval timeout check failed: {e}", exc_info=True)


# ── Module-level singleton ──────────────────────────────────────────

scheduler_manager = WorkflowSchedulerManager()
