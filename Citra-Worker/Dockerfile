# Copyright (c) 2026 Trustedwear Tech Private Limited (https://citra-ai.com)
# Author: Rohit Kumar Chandan
# SPDX-License-Identifier: BUSL-1.1
#
# Licensed under the Business Source License 1.1. Non-production use is granted;
# production use requires a commercial licence until the Change Date, after
# which this file converts to Apache-2.0. See LICENSE at the repository root.

FROM python:3.12-slim

# Citra-Worker shares a codebase with Citra-Service. The workflow
# executor's node implementations defer-import core Citra-Service
# modules (`services.code_executor`, `services.enterprise_tools`,
# `services.enterprise_mcp_client`). Rather than duplicate those into
# the worker image, we bundle Citra-Service code into the worker image
# and put it on PYTHONPATH. Same pattern Celery uses — workers share
# the application codebase with the web tier.
# (Mongo is no longer in this list — it moved to the `citra-mongo`
# shared package and is consumed via `-e ../citra-mongo`.)

WORKDIR /app

# Build context is the monorepo root (.github/workflows/deploy.yml sets
# context="." for citra-worker because the image bundles sibling repos).
# Bundle all editable-deps from requirements.txt (`-e ../citra-X`):
#   citra-queue, citra-auth, citra-mongo, citra-cache, citra-llm,
#   citra-service-utils, citra-workflow.
# requirements.txt resolves these as `../citra-X` from WORKDIR /app/Citra-Worker,
# so each must live at /app/citra-X.
COPY citra-common/citra-queue         /app/citra-queue
COPY citra-common/citra-auth          /app/citra-auth
COPY citra-common/citra-mongo         /app/citra-mongo
COPY citra-common/citra-cache         /app/citra-cache
COPY citra-common/citra-llm           /app/citra-llm
COPY citra-common/citra-service-utils /app/citra-service-utils
COPY citra-workflow      /app/citra-workflow

# `services/` = the code-execution sandbox cluster, lazy-imported by
# citra_workflow.nodes.agents as `services.code_executor`. The worker executes
# those nodes, so it needs the same modules the API has. /app (not the package
# dir) goes on PYTHONPATH so `services.*` resolves.
#
# Was `COPY Citra-Service /app/citra-service`; Citra-Service is not part of
# this repo, so the build failed on a missing path. Nothing imports it.
COPY services /app/services
# services/code_executor.py imports the root-level `bucket` module at import
# time. The worker is where nodes actually execute, so a missing `bucket` here
# fails the run, not the boot.
COPY bucket.py /app/bucket.py

ENV PYTHONPATH=/app:/app/citra-workflow:/app/Citra-Worker

# Worker's own files (queue.py, registry.py, worker.py, handlers/).
COPY Citra-Worker/requirements.txt /app/Citra-Worker/requirements.txt
WORKDIR /app/Citra-Worker
RUN pip install --no-cache-dir -r requirements.txt

COPY Citra-Worker /app/Citra-Worker

# Default knobs — override per environment.
ENV CITRA_WORKER_CONCURRENCY=4 \
    CITRA_WORKER_QUEUES=default \
    CITRA_WORKER_RUN_SCHEDULER=true \
    LOG_LEVEL=INFO

CMD ["python", "-m", "worker"]
