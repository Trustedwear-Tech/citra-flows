"""Tests for the NotifyNode (Track 10) — Slack/Teams/webhook notifications."""
import sys
import os
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from citra_workflow.nodes import get_node, NodeContext
from citra_workflow.models import NodeType


def _client_returning(status=200):
    resp = MagicMock()
    resp.status_code = status
    resp.raise_for_status = MagicMock()
    client = MagicMock()
    client.post = AsyncMock(return_value=resp)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    return client, resp


class TestNotifyNode:
    @pytest.mark.asyncio
    async def test_slack_message_sends_text_payload(self):
        node = get_node(NodeType.NOTIFY)
        ctx = NodeContext(
            node_id="n1",
            node_config={"channel_type": "slack",
                         "webhook_url": "https://hooks.slack.com/services/x",
                         "message": "Run {{name}} done"},
            input_data={}, variables={"name": "Theft Report"},
        )
        client, _ = _client_returning(200)
        with patch("httpx.AsyncClient", return_value=client), \
             patch("citra_workflow.utils.ssrf.assert_url_is_public"):
            result = await node.execute(ctx)

        assert result["meta"]["sent"] is True
        assert result["meta"]["channel"] == "slack"
        _, kwargs = client.post.call_args
        assert kwargs["json"] == {"text": "Run Theft Report done"}  # interpolated

    @pytest.mark.asyncio
    async def test_generic_webhook_envelope(self):
        node = get_node(NodeType.NOTIFY)
        ctx = NodeContext(
            node_id="n2",
            node_config={"channel_type": "generic",
                         "webhook_url": "https://example.com/hook",
                         "message": "hi"},
            input_data={}, workflow_id="wf9",
        )
        client, _ = _client_returning(200)
        with patch("httpx.AsyncClient", return_value=client), \
             patch("citra_workflow.utils.ssrf.assert_url_is_public"):
            await node.execute(ctx)
        _, kwargs = client.post.call_args
        assert kwargs["json"]["message"] == "hi"
        assert kwargs["json"]["workflow_id"] == "wf9"

    @pytest.mark.asyncio
    async def test_requires_url_and_message(self):
        node = get_node(NodeType.NOTIFY)
        ctx = NodeContext(node_id="n3",
                          node_config={"webhook_url": "", "message": "x"},
                          input_data={})
        with pytest.raises(ValueError, match="URL"):
            await node.execute(ctx)
