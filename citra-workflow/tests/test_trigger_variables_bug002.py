"""Bug 002/#5 — Manual & Scheduled triggers now emit their variables as a row.

Previously Manual/Scheduled triggers returned items=[] (variables only in meta),
so a downstream Data Transform silently processed 0 rows. Start/Webhook already
emitted items=[validated]. This aligns Manual/Scheduled with that behavior.
"""
from __future__ import annotations

import pytest

from citra_workflow.nodes import NodeContext, get_node
from citra_workflow.models import NodeType


@pytest.mark.asyncio
async def test_manual_trigger_emits_variables_as_item():
    node = get_node(NodeType.MANUAL_TRIGGER)
    ctx = NodeContext(node_id="t1", node_config={}, variables={"score": 72, "name": "alice"})
    result = await node.execute(ctx)
    assert result["items"] == [{"score": 72, "name": "alice"}]      # row available downstream
    assert result["meta"]["variables"]["score"] == 72                # still in meta for {{vars}}
    assert result["meta"]["trigger_type"] == "manual"


@pytest.mark.asyncio
async def test_manual_trigger_no_variables_stays_empty():
    node = get_node(NodeType.MANUAL_TRIGGER)
    ctx = NodeContext(node_id="t1", node_config={}, variables={})
    result = await node.execute(ctx)
    assert result["items"] == []   # nothing to emit → empty (no spurious row)


@pytest.mark.asyncio
async def test_scheduled_trigger_emits_variables_as_item():
    node = get_node(NodeType.SCHEDULED_TRIGGER)
    ctx = NodeContext(
        node_id="t1",
        node_config={"cron_expression": "*/5 * * * *"},
        variables={"tick": 1},
    )
    result = await node.execute(ctx)
    assert result["items"] == [{"tick": 1}]
    assert result["meta"]["trigger_type"] == "scheduled"
