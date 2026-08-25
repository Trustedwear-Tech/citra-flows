# Copyright (c) 2026 Trustedwear Tech Private Limited (https://citra-ai.com)
# Author: Rohit Kumar Chandan
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may not
# use this file except in compliance with the License. You may obtain a copy of
# the License at http://www.apache.org/licenses/LICENSE-2.0

# Dockerfile for citra-workflow (standalone)
#
# Build from the MONOREPO ROOT so the editable shared libs
# (citra-queue, citra-auth, citra-mongo) are reachable at COPY time:
#
#   docker build -f citra-workflow/Dockerfile -t citra/workflow:latest .
#
# Runs as its own ECS service. Default port 9200. Gunicorn + uvicorn
# workers; tune -w via env at deploy time (start with 2 for I/O-bound
# CRUD; scale up if /generate-code routes get hot).
FROM python:3.11-slim

ARG APP_ENV=prod

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DEBIAN_FRONTEND=noninteractive \
    APP_ENV=${APP_ENV} \
    TZ=Etc/UTC

# Minimal system deps — citra-workflow is HTTP + Mongo + Redis only;
# no ffmpeg / no snappy / no native LLM clients.
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl tzdata \
  && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy shared editable libs first (cache-friendly)
# Must match `-e ../citra-X` lines in citra-workflow/requirements.txt:
#   citra-queue, citra-auth, citra-mongo, citra-service-utils, citra-cache
COPY citra-common/citra-queue         /app/citra-queue
COPY citra-common/citra-auth          /app/citra-auth
COPY citra-common/citra-mongo         /app/citra-mongo
COPY citra-common/citra-service-utils /app/citra-service-utils
COPY citra-common/citra-cache         /app/citra-cache
COPY citra-common/citra-llm           /app/citra-llm

# Copy this service's requirements and install
COPY citra-workflow/requirements.txt /app/citra-workflow/requirements.txt
WORKDIR /app/citra-workflow
RUN pip install --retries 10 --timeout 60 --no-cache-dir -r requirements.txt

# Copy the citra-workflow source LAST so deps stay cached on code-only edits
COPY citra-workflow /app/citra-workflow

# `services/` holds the code-execution sandbox cluster (code_executor,
# sandbox_pool, sandbox_file_cache), lazy-imported by nodes/agents.py as
# `services.code_executor`. It needs /app on PYTHONPATH, not the package dir.
#
# This used to be `COPY Citra-Service /app/citra-service`, for the dept_*
# enterprise tools the /agent-tools endpoint once loaded. Citra-Service is not
# part of this repo — that coupling was removed with the rest of the platform
# wiring — so the COPY referenced a path that does not exist and the image
# could not build at all. Only comments referenced it by the time this was
# fixed; nothing imports it.
COPY services /app/services
# services/code_executor.py imports the root-level `bucket` module (S3/MinIO
# helpers) at import time. Without it the sandbox tool raised
# ModuleNotFoundError the first time a workflow actually ran code — the API
# still booted, so the gap only showed at execute time.
COPY bucket.py /app/bucket.py
ENV PYTHONPATH=/app:/app/citra-workflow

EXPOSE 9200

# Healthcheck — uses the public /health endpoint (no JWT)
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -fsS http://127.0.0.1:9200/health >/dev/null || exit 1

# Gunicorn + Uvicorn workers — graceful 30s, generous request timeout so
# /generate-code (LLM round-trips) has headroom. Worker count env-tunable via
# WEB_CONCURRENCY (default 2). The API scales freely; the cron scheduler is
# leader-elected so only one process fires jobs regardless of worker count.
# `exec` so gunicorn is PID 1 and gets SIGTERM directly.
CMD ["sh", "-c", "exec gunicorn citra_workflow.main:app -k uvicorn.workers.UvicornWorker -w ${WEB_CONCURRENCY:-2} -b 0.0.0.0:9200 --timeout 300 --graceful-timeout 30 --access-logfile - --error-logfile -"]
