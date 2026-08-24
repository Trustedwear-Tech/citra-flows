# Copyright (c) 2026 Trustedwear Tech Private Limited (https://citra-ai.com)
# Author: Rohit Kumar Chandan
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may not
# use this file except in compliance with the License. You may obtain a copy of
# the License at http://www.apache.org/licenses/LICENSE-2.0

"""
Handler registry for Citra-Worker.

Handlers are async functions registered by name. The worker loop pulls
a job, looks up the handler by ``job.handler``, and calls it with
``(payload, context)``.

Adding a new handler:

    from registry import register
    from queue import JobPermanentFailure

    @register("workflow.run")
    async def run_workflow(payload, ctx):
        workflow_id = payload.get("workflow_id")
        if not workflow_id:
            raise JobPermanentFailure("workflow_id is required")
        # ... do the work ...
        return {"status": "ok", "rows_processed": 42}

The handler's return value is JSON-serialised into the job's `result`
field so callers (Citra-Service polling for results) can read it.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, Optional

logger = logging.getLogger(__name__)


@dataclass
class JobContext:
    """Per-job context passed to every handler."""
    job_id: str
    tenant_id: Optional[str]
    request_id: Optional[str]
    retries: int


HandlerFn = Callable[[Dict[str, Any], JobContext], Awaitable[Any]]

_REGISTRY: Dict[str, HandlerFn] = {}


def register(name: str) -> Callable[[HandlerFn], HandlerFn]:
    """Decorator: register an async handler under ``name``.

    Names are usually dotted (e.g. ``workflow.run``,
    ``report.compose``) for namespacing. Uniqueness is enforced —
    re-registering the same name raises.
    """
    def _wrap(fn: HandlerFn) -> HandlerFn:
        if name in _REGISTRY:
            raise ValueError(f"handler '{name}' already registered")
        _REGISTRY[name] = fn
        logger.info(f"📖 registered handler: {name}")
        return fn
    return _wrap


def get(name: str) -> Optional[HandlerFn]:
    return _REGISTRY.get(name)


def names() -> list[str]:
    return sorted(_REGISTRY.keys())
