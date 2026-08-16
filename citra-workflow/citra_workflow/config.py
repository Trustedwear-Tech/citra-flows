"""
Workflow Engine — Centralized Configuration
============================================
All tunable limits, timeouts, and thresholds for the workflow engine.
Every value is read from an environment variable with a sensible enterprise default.

In a Kubernetes multi-pod deployment each pod reads the SAME env vars,
so these values define the enterprise-wide behaviour.

Override any value via environment variable or .env file.
See ../.env.workflow (citra-workflow/.env.workflow) for the full reference
with documentation.
"""

import os
import socket

_e = os.environ.get


def _int(key: str, default: int) -> int:
    return int(_e(key, str(default)))


def _float(key: str, default: float) -> float:
    return float(_e(key, str(default)))


def _bool(key: str, default: bool) -> bool:
    return _e(key, str(default)).strip().lower() in ("1", "true", "yes", "on")


# ============================================================================
# code_block node — Docker sandbox
# ============================================================================

# The `code_block` node runs author-supplied Python in an isolated Docker
# container — the worker launches it against the local Docker daemon (the
# sandbox image is co-located on the worker box). Code is NEVER executed
# in-process. If the daemon/image is unavailable the node fails closed.
CODE_SANDBOX_IMAGE = _e("WF_CODE_SANDBOX_IMAGE", "quick-chat-sandbox")
CODE_SANDBOX_TIMEOUT = _int("WF_CODE_SANDBOX_TIMEOUT", 60)


# ============================================================================
# Execution Engine
# ============================================================================

# Maximum wall-clock seconds for an entire workflow execution (all nodes combined).
# If a workflow exceeds this, it is forcibly terminated and marked FAILED.
EXECUTION_TIMEOUT_SECONDS = _int("WF_EXECUTION_TIMEOUT", 3600)

# Grace margin added on top of EXECUTION_TIMEOUT before the scheduler's
# stuck-execution reaper marks a still-"running"/"queued" execution as
# FAILED. An execution past started_at + EXECUTION_TIMEOUT + this grace
# can only be there because the worker process died mid-run.
STUCK_EXECUTION_GRACE_SECONDS = _int("WF_STUCK_EXECUTION_GRACE", 600)

# Minimum seconds between mid-run execution checkpoints. After each node
# completes the executor persists progress (node_results + variables) to Mongo,
# but no more often than this, so a fast graph doesn't hammer the DB. A crash
# resumes from the last checkpoint instead of re-running the whole graph.
CHECKPOINT_INTERVAL_SECONDS = _int("WF_CHECKPOINT_INTERVAL", 5)

# How many times the stuck-execution reaper will try to crash-resume a run
# before giving up and marking it FAILED. Bounds a poison run that crashes the
# worker on the same node every time.
MAX_CRASH_RESUME_ATTEMPTS = _int("WF_MAX_CRASH_RESUME_ATTEMPTS", 3)

# Maximum seconds any single node may run before it is killed.
NODE_TIMEOUT_SECONDS = _int("WF_NODE_TIMEOUT", 300)

# Maximum number of node execution steps in a single workflow run.
# Guards against infinite graphs or pathological fan-outs.
MAX_NODE_STEPS = _int("WF_MAX_NODE_STEPS", 500)

# Per-run LLM-call ceiling. A single workflow RUN may make at most this many
# LLM calls across ALL its agent/LLM nodes (counted in the executor); the run
# aborts fail-loud when exceeded. This is the hard cost/time stop for a run —
# even within the step/fan-out/agent-iteration bounds, a workflow can't run up
# unbounded model spend. A workflow may override this via its
# ``max_run_llm_calls`` field, but never above MAX_RUN_LLM_CALLS_HARD.
MAX_RUN_LLM_CALLS = _int("WF_MAX_RUN_LLM_CALLS", 200)
# Admin hard ceiling on any per-workflow override of the above.
MAX_RUN_LLM_CALLS_HARD = _int("WF_MAX_RUN_LLM_CALLS_HARD", 1000)

# Safety cap on how often a scheduled (cron) workflow may fire. Deploy is
# rejected if the cron would fire more frequently than this — an unattended
# every-minute schedule against a production source system is exactly the
# runaway pattern we want to prevent. 300s (5 min) by default; lower it per
# deployment if you have a genuine high-frequency polling need.
MIN_CRON_INTERVAL_SECONDS = _int("WF_MIN_CRON_INTERVAL_SECONDS", 300)

# Maximum simultaneous workflow executions across all pods sharing the same Redis.
# Set based on your cluster capacity. Each running execution holds one slot.
MAX_CONCURRENT_EXECUTIONS = _int("WF_MAX_CONCURRENT", 20)

# Safety TTL on the Redis concurrency counter (seconds).
# Should be >= EXECUTION_TIMEOUT to avoid premature eviction.
CONCURRENCY_TTL = _int("WF_CONCURRENCY_TTL", EXECUTION_TIMEOUT_SECONDS + 60)

# TTL for execution progress cache in Redis (seconds).
# Frontend polls this for live updates. Must be >= EXECUTION_TIMEOUT.
PROGRESS_CACHE_TTL = _int("WF_PROGRESS_CACHE_TTL", max(EXECUTION_TIMEOUT_SECONDS, 600))


# ============================================================================
# Rate Limiting
# ============================================================================

# Webhook trigger rate limit: max requests per token within the window.
RATE_LIMIT_WEBHOOK = _int("WF_RATE_LIMIT_WEBHOOK", 30)

# Rate limit sliding window in seconds.
RATE_LIMIT_WINDOW = _int("WF_RATE_LIMIT_WINDOW", 60)

# AI generation/editing endpoints (generate-workflow / refine / edit-node /
# generate-code). These drive expensive, high-token LLM calls, so they are
# throttled per-user and per-org to stop a runaway loop (a buggy client or a
# tight retry) from running up unbounded inference cost. Enforced via the
# SHARED Redis counter (citra-cache), so the limit holds across all replicas —
# unlike the in-process webhook limiter. Requests per RATE_LIMIT_WINDOW seconds.
RATE_LIMIT_AI_PER_USER = _int("WF_RATE_LIMIT_AI_PER_USER", 15)
RATE_LIMIT_AI_PER_ORG = _int("WF_RATE_LIMIT_AI_PER_ORG", 60)


# ============================================================================
# Pagination
# ============================================================================

# Default and maximum page sizes for list endpoints.
PAGE_DEFAULT_WORKFLOWS = _int("WF_PAGE_DEFAULT_WORKFLOWS", 50)
PAGE_DEFAULT_EXECUTIONS = _int("WF_PAGE_DEFAULT_EXECUTIONS", 20)
PAGE_DEFAULT_APPROVALS = _int("WF_PAGE_DEFAULT_APPROVALS", 50)
PAGE_MAX = _int("WF_PAGE_MAX", 100)


# ============================================================================
# Timeouts — Network & Database
# ============================================================================

# MongoDB serverSelectionTimeoutMS for node connections (read/write).
MONGO_CONNECT_TIMEOUT_MS = _int("WF_MONGO_CONNECT_TIMEOUT_MS", 10000)

# MongoDB serverSelectionTimeoutMS for router connection-test endpoint.
MONGO_TEST_TIMEOUT_MS = _int("WF_MONGO_TEST_TIMEOUT_MS", 5000)

# HTTP timeout (seconds) for the web search tool inside AI agents.
HTTP_TIMEOUT_WEB_SEARCH = _float("WF_HTTP_TIMEOUT_WEB_SEARCH", 15.0)

# HTTP timeout (seconds) for the HTTP request tool inside AI agents.
HTTP_TIMEOUT_TOOL = _float("WF_HTTP_TIMEOUT_TOOL", 30.0)

# HTTP timeout (seconds) for API Source nodes.
HTTP_TIMEOUT_API_SOURCE = _int("WF_HTTP_TIMEOUT_API_SOURCE", 30)

# HTTP timeout (seconds) for sending data to the Playwright render service.
HTTP_TIMEOUT_PDF_RENDER = _int("WF_HTTP_TIMEOUT_PDF_RENDER", 60)

# HTTP timeout (seconds) for Webhook Output nodes.
HTTP_TIMEOUT_WEBHOOK_OUTPUT = _int("WF_HTTP_TIMEOUT_WEBHOOK_OUTPUT", 30)

# HTTP timeout (seconds) for router connection-test on API connections.
HTTP_TIMEOUT_CONN_TEST = _int("WF_HTTP_TIMEOUT_CONN_TEST", 10)

# FTP/SFTP connection timeout (seconds).
FTP_CONNECT_TIMEOUT = _int("WF_FTP_CONNECT_TIMEOUT", 30)


# ============================================================================
# Data Size Limits — Nodes (Never Truncate, Error Instead)
# ============================================================================

# Maximum items in a Loop node before erroring.
MAX_LOOP_ITERATIONS = _int("WF_MAX_LOOP_ITERATIONS", 10000)

# Runaway-cost guard for per-item LLM processing. A processor in `each`
# mode makes one LLM call per input item; `batch` mode one per batch.
# A mis-wired large source must not silently fire thousands of calls.
MAX_PER_ITEM_FANOUT = _int("WF_MAX_PER_ITEM_FANOUT", 500)

# How many per-item / per-batch LLM calls a processor runs CONCURRENTLY in
# `each` / `batch` mode. Sequential per-record processing of a slow model
# (e.g. evaluating several candidate resumes one-by-one) easily exceeds the
# per-node timeout; bounded concurrency keeps total time ~= the slowest call
# instead of the sum, while capping simultaneous load on the LLM backend.
PER_ITEM_CONCURRENCY = _int("WF_PER_ITEM_CONCURRENCY", 5)

# Maximum field path nesting depth for Condition nodes.
MAX_FIELD_DEPTH = _int("WF_MAX_FIELD_DEPTH", 10)

# Maximum tool-calling iterations for AI Agent nodes.
MAX_AGENT_ITERATIONS = _int("WF_MAX_AGENT_ITERATIONS", 20)

# Maximum input data size (chars) sent to an AI Agent.
MAX_AGENT_INPUT_SIZE = _int("WF_MAX_AGENT_INPUT_SIZE", 30000)

# Maximum vector search content per hit (chars).
MAX_VECTOR_CONTENT_SIZE = _int("WF_MAX_VECTOR_CONTENT_SIZE", 500)

# Maximum chars for LLM Processor "all data" prompt.
MAX_LLM_PROMPT_SIZE = _int("WF_MAX_LLM_PROMPT_SIZE", 30000)

# Maximum chars for LLM Processor single-item prompt.
MAX_LLM_ITEM_SIZE = _int("WF_MAX_LLM_ITEM_SIZE", 15000)

# Maximum chars for Rules Engine / Classifier / Extractor data.
MAX_LLM_RULES_SIZE = _int("WF_MAX_LLM_RULES_SIZE", 20000)

# Maximum chars for Summarizer data.
MAX_LLM_SUMMARY_SIZE = _int("WF_MAX_LLM_SUMMARY_SIZE", 25000)

# Maximum chars for email body data in Email Sender node.
MAX_EMAIL_BODY_SIZE = _int("WF_MAX_EMAIL_BODY_SIZE", 10000)

# Maximum rows for PDF / HTML table rendering.
MAX_TABLE_RECORDS = _int("WF_MAX_TABLE_RECORDS", 500)

# Maximum email preview length in notification emails before adding "View in Citra" link.
MAX_EMAIL_PREVIEW_SIZE = _int("WF_MAX_EMAIL_PREVIEW_SIZE", 2000)


# ============================================================================
# Database Query Defaults (user-configurable per node, these are fallbacks)
# ============================================================================

# Default max rows returned by SQL Source node.
DEFAULT_SQL_MAX_ROWS = _int("WF_DEFAULT_SQL_MAX_ROWS", 1000)

# Default max documents returned by NoSQL Source node.
DEFAULT_NOSQL_LIMIT = _int("WF_DEFAULT_NOSQL_LIMIT", 500)

# Default max rows returned by the sql_query tool inside AI Agents.
DEFAULT_AGENT_SQL_ROWS = _int("WF_DEFAULT_AGENT_SQL_ROWS", 50)

# Default max documents returned by the nosql_query tool inside AI Agents.
DEFAULT_AGENT_NOSQL_LIMIT = _int("WF_DEFAULT_AGENT_NOSQL_LIMIT", 20)

# Per-tool-call timeout (seconds) inside the AI Agent LLM loop.
# If a single tool call (SQL query, SFTP download, etc.) exceeds this, it is
# cancelled and the error is reported back to the LLM for recovery.
AGENT_TOOL_CALL_TIMEOUT = _float("WF_AGENT_TOOL_CALL_TIMEOUT", 30.0)

# Max file size (bytes) an agent bucket/SFTP read tool may download.
MAX_AGENT_FILE_READ_SIZE = _int("WF_MAX_AGENT_FILE_READ_SIZE", 5 * 1024 * 1024)  # 5 MB

# Max objects returned by the bucket_read tool's list operation.
DEFAULT_AGENT_BUCKET_LIST_LIMIT = _int("WF_DEFAULT_AGENT_BUCKET_LIST_LIMIT", 100)

# Max entries returned by the sftp_read tool's list operation.
DEFAULT_AGENT_SFTP_LIST_LIMIT = _int("WF_DEFAULT_AGENT_SFTP_LIST_LIMIT", 100)


# ============================================================================
# Scheduler
# ============================================================================

# Unique instance identifier for distributed locking and log identification.
# Auto-generated from hostname + PID. Override for custom naming.
try:
    _hostname = socket.gethostname()[:12]
except Exception:
    _hostname = "unknown"
SCHEDULER_INSTANCE_ID = _e(
    "SCHEDULER_INSTANCE_ID",
    f"{_hostname}-{os.getpid()}",
)

# Whether the scheduler is enabled on this instance.
# Set to "false" to disable the scheduler (e.g. for API-only replicas).
SCHEDULER_ENABLED = _e("SCHEDULER_ENABLED", "true").lower() in ("true", "1", "yes")

# Timezone for the APScheduler internal clock and the default for new cron
# schedules.  Must be a valid pytz/IANA timezone name.
SCHEDULER_TIMEZONE = _e("SCHEDULER_TIMEZONE", "America/New_York")

# How often the approval timeout checker runs (minutes).
APPROVAL_CHECK_INTERVAL_MIN = _int("WF_APPROVAL_CHECK_INTERVAL_MIN", 10)

# Redis lock timeout for the cron execution lock (seconds).
# Must be > EXECUTION_TIMEOUT to prevent premature expiry during long workflows.
# The lock renewal heartbeat will extend this periodically.
SCHEDULER_CRON_LOCK_TIMEOUT = _int("WF_SCHEDULER_CRON_LOCK_TIMEOUT", EXECUTION_TIMEOUT_SECONDS + 120)

# Redis lock timeout for the overlap (running-workflow) lock (seconds).
# Prevents a second cron fire from starting the same workflow while it's running.
# Must be >= EXECUTION_TIMEOUT.
SCHEDULER_OVERLAP_LOCK_TIMEOUT = _int("WF_SCHEDULER_OVERLAP_LOCK_TIMEOUT", EXECUTION_TIMEOUT_SECONDS + 120)

# Redis lock timeout for the approval timeout sweep (seconds).
SCHEDULER_APPROVAL_LOCK_TIMEOUT = _int("WF_SCHEDULER_APPROVAL_LOCK_TIMEOUT", 600)

# Default approval timeout in hours when not specified by the node config.
DEFAULT_APPROVAL_TIMEOUT_HOURS = _float("WF_DEFAULT_APPROVAL_TIMEOUT_HOURS", 24)

# Misfire grace time (seconds). If the scheduler fires late (e.g. due to
# container startup delay), it will still run the job if within this window.
SCHEDULER_MISFIRE_GRACE_TIME = _int("WF_SCHEDULER_MISFIRE_GRACE_TIME", 30)

# ── Leader election ──────────────────────────────────────────────────────────
# All instances compete for the Redis leader key. The winner runs APScheduler
# and registers cron jobs. If the leader dies the key expires after
# SCHEDULER_LEADER_TTL seconds and a follower takes over automatically.

# How long (seconds) the Redis leader lock lives without renewal.
# Failover latency ≈ SCHEDULER_LEADER_TTL + SCHEDULER_LEADER_POLL_INTERVAL.
SCHEDULER_LEADER_TTL = _int("WF_SCHEDULER_LEADER_TTL", 30)

# How often (seconds) the leader renews its lock — target ~1/3 of TTL.
# Must satisfy: SCHEDULER_LEADER_RENEW_INTERVAL < SCHEDULER_LEADER_TTL.
SCHEDULER_LEADER_RENEW_INTERVAL = max(SCHEDULER_LEADER_TTL // 3, 5)

# How often (seconds) followers poll the leader lock to attempt takeover.
# Keep above SCHEDULER_LEADER_TTL // 2 to avoid thundering-herd on expiry.
SCHEDULER_LEADER_POLL_INTERVAL = _int("WF_SCHEDULER_LEADER_POLL_INTERVAL", 15)

# Redis key for the leader lock (prefixed by REDIS_KEY_PREFIX when stored).
SCHEDULER_LEADER_KEY = "scheduler:leader"

# Redis pub/sub channel for hot-reload of schedule changes.
# Scoped by APP_ENV so PROD and TEST containers on a shared Redis server
# do NOT cross-deliver each other's events.
# Redis pub/sub is server-global (not DB-scoped), so key-prefix isolation is
# insufficient — the channel name itself must differ between environments.
# Produces:  prod:workflow:schedule:refresh
#            test:workflow:schedule:refresh
#            dev:workflow:schedule:refresh  (fallback for local)
_scheduler_env = _e("APP_ENV", "prod").lower().strip() or "prod"
SCHEDULER_REFRESH_CHANNEL = f"{_scheduler_env}:workflow:schedule:refresh"


# ============================================================================
# Webhook Security
# ============================================================================

# Grace period (seconds) for accepting a recently-rotated webhook token.
WEBHOOK_TOKEN_GRACE_PERIOD = _int("WF_WEBHOOK_TOKEN_GRACE_PERIOD", 300)

# Maximum age (seconds) of a webhook timestamp before replay-protection rejects it.
WEBHOOK_REPLAY_WINDOW = _int("WF_WEBHOOK_REPLAY_WINDOW", 300)

# Number of leading characters to show when masking secrets (e.g. connection strings).
SECRET_MASK_LENGTH = _int("WF_SECRET_MASK_LENGTH", 8)


# ============================================================================
# Model Constraints
# ============================================================================

# Maximum nodes allowed in a workflow definition.
MAX_WORKFLOW_NODES = _int("WF_MAX_WORKFLOW_NODES", 500)

# Maximum edges allowed in a workflow definition.
MAX_WORKFLOW_EDGES = _int("WF_MAX_WORKFLOW_EDGES", 1000)
