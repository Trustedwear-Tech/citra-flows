"""Tests for the per-run LLM-call budget (prod-readiness cost cap).

Covers the budget primitive (no-op without a budget, enforcement on the
(N+1)th call, shared counter across parallel branches) and that the processors'
``_charged_offload`` wrapper actually charges before offloading.
"""
import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from citra_workflow.llm_budget import (
    start_run_budget, charge_llm_call, current_run_llm_calls, RunLlmBudgetExceeded,
)


def test_charge_is_noop_without_budget():
    # No budget set on this context → charging must not raise.
    charge_llm_call()
    assert current_run_llm_calls() == 0


@pytest.mark.asyncio
async def test_limit_enforced_on_next_call():
    start_run_budget(3)
    for _ in range(3):
        charge_llm_call()
    assert current_run_llm_calls() == 3
    with pytest.raises(RunLlmBudgetExceeded):
        charge_llm_call()


@pytest.mark.asyncio
async def test_budget_shared_across_parallel_branches():
    # Child tasks (parallel_split branches) inherit the same budget object, so
    # their charges count against the one run-level tally.
    start_run_budget(2)

    async def branch():
        charge_llm_call()

    await asyncio.gather(branch(), branch())
    assert current_run_llm_calls() == 2
    with pytest.raises(RunLlmBudgetExceeded):
        charge_llm_call()


@pytest.mark.asyncio
async def test_charged_offload_charges_before_running():
    from citra_workflow.nodes.processors import _charged_offload

    start_run_budget(1)

    def _fake_llm():
        return "ok"

    # First offload charges (count 1) and runs.
    assert await _charged_offload(_fake_llm) == "ok"
    assert current_run_llm_calls() == 1
    # Second offload trips the cap BEFORE running the call.
    with pytest.raises(RunLlmBudgetExceeded):
        await _charged_offload(_fake_llm)
