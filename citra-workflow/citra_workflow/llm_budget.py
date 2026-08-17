# Copyright (c) 2026 Trustedwear Tech Private Limited (https://citra-ai.com)
# Author: Rohit Kumar Chandan
# SPDX-License-Identifier: BUSL-1.1
#
# Licensed under the Business Source License 1.1. Non-production use is granted;
# production use requires a commercial licence until the Change Date, after
# which this file converts to Apache-2.0. See LICENSE at the repository root.

"""Per-run LLM-call budget for workflow executions.

A single workflow RUN may make at most N LLM calls across all of its agent /
LLM nodes. The executor sets the limit at run start (a global default, optionally
overridden per workflow within an admin hard-max); every LLM call site calls
``charge_llm_call()`` just before issuing a request, and the run aborts
fail-loud (``RunLlmBudgetExceeded``) the moment the limit is breached.

The counter lives on a ``ContextVar`` so all nodes in a run — including parallel
branches — share one tally without threading a parameter through every node
signature. IMPORTANT: charge in the ASYNC node code, before any
``run_in_executor`` offload — contextvars are NOT copied into the default
executor's threads, so a charge made inside the offloaded function would read an
empty budget and silently under-count.
"""
from __future__ import annotations

import contextvars
import logging

logger = logging.getLogger(__name__)


class RunLlmBudgetExceeded(RuntimeError):
    """Raised when a workflow run exceeds its per-run LLM-call limit."""


class _RunLlmBudget:
    __slots__ = ("limit", "count")

    def __init__(self, limit: int):
        self.limit = int(limit)
        self.count = 0

    def charge(self) -> None:
        self.count += 1
        if self.limit and self.count > self.limit:
            raise RunLlmBudgetExceeded(
                f"Workflow exceeded its per-run LLM-call limit of {self.limit} "
                f"(attempted call #{self.count}). Reduce per-item fan-out, agent "
                f"tool iterations, or the number of LLM nodes — or raise the "
                f"workflow's max_run_llm_calls (subject to the admin hard-max)."
            )


# default=None → calls made outside a workflow run (no budget set) are a no-op.
_budget_var: contextvars.ContextVar = contextvars.ContextVar(
    "wf_run_llm_budget", default=None
)


def start_run_budget(limit: int) -> None:
    """Begin a fresh per-run LLM-call budget for the current execution context.

    Called once by the executor at run start, BEFORE any node runs, so every
    node (and every parallel branch spawned from this context) shares the
    counter.
    """
    _budget_var.set(_RunLlmBudget(limit))


def charge_llm_call() -> None:
    """Count one LLM call against the current run's budget, raising
    ``RunLlmBudgetExceeded`` if the limit is now exceeded. No-op when no budget
    is set on the current context (e.g. an LLM call outside a workflow run)."""
    budget = _budget_var.get()
    if budget is not None:
        budget.charge()


def current_run_llm_calls() -> int:
    """LLM calls charged so far in this run (0 when no budget is set)."""
    budget = _budget_var.get()
    return budget.count if budget is not None else 0
