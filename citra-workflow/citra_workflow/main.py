"""
citra-workflow — standalone FastAPI service.

Run with:
    uvicorn citra_workflow.main:app --host 0.0.0.0 --port 9200

Or in production:
    gunicorn citra_workflow.main:app -k uvicorn.workers.UvicornWorker \
        --workers 2 --bind 0.0.0.0:9200 --timeout 120

What this serves:
  /api/workflows/*        — workflow CRUD + lifecycle endpoints
  /api/admin/workflows/*  — admin reassign / inheritance overrides
  /health                 — liveness probe (always 200)
  /health/ready           — readiness probe (200 only when Mongo + Redis reachable)

What this does NOT serve:
  - Workflow execution (handled by Citra-Worker via the Redis queue)
  - Scheduler / cron firing (handled by Citra-Worker)
"""
from __future__ import annotations

# Vault bootstrap MUST be first — populates os.environ with secrets so the
# Mongo/Redis/JWT vars are visible before any settings/config import below.
# Fails fast (SystemExit) if Vault is unreachable in prod. In local dev the
# load_dotenv() call below provides a fallback (existing env vars are not
# overwritten by Vault, so the local .env wins).
import os
if os.getenv("VAULT_ADDR"):
    from citra_service_utils import load_from_vault
    load_from_vault()

# ── Local-dev sys.path shim for Citra-Service ────────────────────────────
# router.py's /agent-tools endpoint lazy-imports services.enterprise_mcp_
# client + services.enterprise_tools (and transitively agentic_rag.
# circuit_breaker) to enrich the catalog with dept_* MCP tools. In prod
# the Dockerfile bakes Citra-Service into the image and sets PYTHONPATH=
# /app/citra-service:/app/citra-workflow, so those resolve. In local dev
# the service runs from its own venv with Citra-Service sitting as a
# sibling in the monorepo — add it to sys.path here so both environments
# behave the same. No-op if PYTHONPATH already has it or the sibling
# doesn't exist (e.g. citra-workflow checked out standalone).
import sys as _sys
from pathlib import Path as _Path
_citra_service_dir = _Path(__file__).resolve().parent.parent.parent / "Citra-Service"
if _citra_service_dir.is_dir() and str(_citra_service_dir) not in _sys.path:
    _sys.path.insert(0, str(_citra_service_dir))

import logging
from contextlib import asynccontextmanager
from typing import Any, Dict

# Local-dev fallback: load .env if present. In prod, vault_bootstrap above
# already populated os.environ, so this is a no-op.
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from citra_auth import JWTAuthMiddleware
from citra_mongo import get_async_mongo_client, get_database_name

from .router import router as workflow_router

logger = logging.getLogger(__name__)
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)

# Error tracker (GlitchTip / Sentry) — no-op unless SENTRY_DSN is set
try:
    import sentry_sdk
    from sentry_sdk.integrations.fastapi import FastApiIntegration
    from sentry_sdk.integrations.starlette import StarletteIntegration
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
                FastApiIntegration(transaction_style="endpoint"),
                StarletteIntegration(transaction_style="endpoint"),
                AsyncioIntegration(),
                LoggingIntegration(level=logging.INFO, event_level=logging.ERROR),
            ],
        )
        sentry_sdk.set_tag("service", "citra-workflow")
except Exception as _sentry_exc:
    logger.warning("Sentry init skipped: %s", _sentry_exc)


# ── Lifespan: warm Mongo on startup, close on shutdown ─────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("⚡ citra-workflow starting up")
    # Warm the Mongo client. If this fails, the readiness probe will
    # return 503 and ECS will not route traffic to us — preferred over
    # silently serving 500s.
    try:
        client = get_async_mongo_client()
        await client.admin.command("ping")
        logger.info(f"✅ Mongo reachable (db={get_database_name()})")
    except Exception as exc:  # noqa: BLE001
        logger.error(f"❌ Mongo ping on startup failed: {exc}")
    # Ensure the per-workflow run-history index exists. Self-guards against
    # errors and is idempotent; the run-history query works without it (slower).
    try:
        from .router import ensure_execution_indexes
        await ensure_execution_indexes()
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"WorkflowExecutions index init skipped: {exc}")
    # No user store to initialise: accounts live in Citra-User-Service and this
    # service only verifies the tokens it issues. See auth_routes.py.
    if not os.getenv("USER_SERVICE_URL"):
        logger.warning(
            "USER_SERVICE_URL is not set — sign-in will fail. Identity is "
            "delegated to Citra-User-Service; set it to that service's base URL."
        )
    yield
    logger.info("🛑 citra-workflow shutting down")
    try:
        from citra_mongo import close_all_connections
        close_all_connections()
    except Exception:  # noqa: BLE001
        # Announce loudly; don't raise during shutdown (would abort clean
        # teardown). A failure here means Mongo connections may leak.
        logger.error(
            "Failed to close Mongo connections during shutdown; possible leak",
            exc_info=True,
        )


app = FastAPI(
    title="citra-workflow",
    description=(
        "Workflow domain service — DAG CRUD, lifecycle transitions, "
        "and dept-data-flow templates. Execution lives in Citra-Worker."
    ),
    version=os.getenv("APP_VERSION", "1.1.0"),
    lifespan=lifespan,
)

# Prometheus /metrics endpoint
try:
    from prometheus_fastapi_instrumentator import Instrumentator
    Instrumentator().instrument(app).expose(app, endpoint="/metrics", include_in_schema=False)
except ImportError:
    import logging
    logging.getLogger(__name__).warning("prometheus-fastapi-instrumentator not installed; /metrics disabled")


# Distributed tracing — OTel spans → Tempo
try:
    from citra_service_utils import setup_tracing as _setup_tracing, request_id_middleware as _request_id_middleware
    app.middleware("http")(_request_id_middleware)
    _setup_tracing(app, service_name="citra-workflow")
except ImportError:
    logger.warning("citra-service-utils not installed; distributed tracing disabled")


# ── Auth ───────────────────────────────────────────────────────────────
# Webhook triggers are auth-via-URL-token, NOT JWT — keep them public.
# Health probes are public so ECS can hit them without a token.
app.add_middleware(
    JWTAuthMiddleware,
    extra_public_endpoints={
        # Login is how you GET a token — it cannot itself require one.
        "/api/auth/login",
    },
    extra_public_patterns=[
        "/api/workflows/webhook/",
    ],
)


# ── CORS (added LAST so it is the OUTERMOST middleware) ────────────────
# Starlette runs middleware in reverse-add order, so whatever is added
# last wraps everything. CORS must be outermost — otherwise a JWT 401 (or
# any pre-route exception) returns without Access-Control-Allow-Origin and
# the browser reports a misleading "blocked by CORS policy" instead of the
# real 401. This was the bug behind the UI's "Failed to fetch" on
# /api/workflows/generate-workflow.
_cors_origins = [
    o.strip() for o in os.getenv("CORS_ALLOWED_ORIGINS", "*").split(",") if o.strip()
] or ["*"]

# `allow_origins=["*"]` together with `allow_credentials=True` is rejected by
# every browser — the CORS spec forbids a wildcard on a credentialed request,
# so the preflight comes back with no Access-Control-Allow-Origin at all and
# the fetch fails as "Failed to fetch" rather than as anything diagnosable.
# Shipping that pair as the DEFAULT meant the API could not be called from a
# browser out of the box. Caught by actually loading the UI against it.
#
# We authenticate with a bearer header, not cookies, so credentials are only
# needed when someone deliberately configures explicit origins.
_allow_credentials = _cors_origins != ["*"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=_allow_credentials,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Routes ─────────────────────────────────────────────────────────────
app.include_router(workflow_router, tags=["Workflow Engine"])

# User authentication (login / me). Small on purpose — see auth_routes.py.
from .auth_routes import router as auth_router  # noqa: E402
app.include_router(auth_router)



# ── Health endpoints ───────────────────────────────────────────────────
# Two distinct concerns:
#   /health        — liveness, always 200 unless the process is hung
#   /health/ready  — readiness, only 200 when downstream deps work
# ECS / k8s probes use the latter to gate traffic.
@app.get("/health", include_in_schema=False)
async def health() -> Dict[str, Any]:
    return {"status": "ok", "service": "citra-workflow"}


@app.get("/health/live", include_in_schema=False)
async def health_live() -> Dict[str, Any]:
    return {"status": "ok"}


@app.get("/health/ready", include_in_schema=False)
async def health_ready():
    """Readiness — verifies Mongo + Redis (via citra-queue) are reachable.

    Returns 503 when a dependency is down so ECS withdraws traffic until
    things stabilise. Cheap: a ping per dependency, no real load.
    """
    issues = []

    # Mongo
    try:
        client = get_async_mongo_client()
        await client.admin.command("ping")
    except Exception as exc:  # noqa: BLE001
        issues.append(f"mongo: {exc}")

    # Redis (via citra-queue's sync client to avoid import-loop pain)
    try:
        from citra_queue.queue import _sync_redis
        _sync_redis().ping()
    except Exception as exc:  # noqa: BLE001
        issues.append(f"redis: {exc}")

    if issues:
        from fastapi.responses import JSONResponse
        return JSONResponse(
            status_code=503,
            content={"status": "degraded", "issues": issues},
        )
    return {"status": "ok"}
