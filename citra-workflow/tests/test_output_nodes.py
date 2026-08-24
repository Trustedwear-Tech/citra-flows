# Copyright (c) 2026 Trustedwear Tech Private Limited (https://citra-ai.com)
# Author: Rohit Kumar Chandan
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may not
# use this file except in compliance with the License. You may obtain a copy of
# the License at http://www.apache.org/licenses/LICENSE-2.0

"""
Unit tests for output / sink nodes — S3, MongoDB, webhook, email, CSV, report.
All external calls (S3, MongoDB, SMTP, httpx) are mocked.
"""

import sys
import os
import json
import pytest
from unittest.mock import patch, AsyncMock, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from citra_workflow.nodes import NodeContext, get_node
from citra_workflow.models import NodeType


# ============================================================================
# WebhookOutputNode
# ============================================================================

class TestWebhookOutput:

    @pytest.mark.asyncio
    async def test_posts_data(self):
        node = get_node(NodeType.WEBHOOK_OUTPUT)
        ctx = NodeContext(
            node_id="wh1",
            node_config={"url": "https://hooks.example.com/results", "headers": {"X-Key": "abc"}},
            input_data={"result": "ok"},
        )
        mock_resp = MagicMock(status_code=200)
        mock_client = AsyncMock()
        mock_client.post.return_value = mock_resp
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        # assert_url_is_public does real DNS — patch it out for this
        # hermetic happy-path test (SSRF blocking is covered by test_ssrf).
        with patch("httpx.AsyncClient", return_value=mock_client), \
             patch("citra_workflow.utils.ssrf.assert_url_is_public"):
            result = await node.execute(ctx)

        assert result["meta"]["status_code"] == 200
        assert result["meta"]["url"] == "https://hooks.example.com/results"

    @pytest.mark.asyncio
    async def test_blocks_localhost(self):
        node = get_node(NodeType.WEBHOOK_OUTPUT)
        ctx = NodeContext(
            node_id="wh2",
            node_config={"url": "http://localhost:3000/admin"},
            input_data={},
        )
        with pytest.raises(ValueError, match="internal|private"):
            await node.execute(ctx)

    @pytest.mark.asyncio
    async def test_blocks_private_ip(self):
        node = get_node(NodeType.WEBHOOK_OUTPUT)
        ctx = NodeContext(
            node_id="wh3",
            node_config={"url": "http://192.168.1.1/admin"},
            input_data={},
        )
        with pytest.raises(ValueError, match="internal|private"):
            await node.execute(ctx)


# ============================================================================
# BucketWriterNode
# ============================================================================

class TestBucketWriter:

    @pytest.mark.asyncio
    async def test_writes_json_to_bucket(self):
        import bucket as bucket_mod

        node = get_node(NodeType.BUCKET_WRITER)
        ctx = NodeContext(
            node_id="s3w1",
            node_config={"filename": "output.json"},
            input_data={"items": [1, 2, 3]},
            user_id="u1",
            execution_id="exec1",
        )
        mock_s3 = MagicMock()
        # The outputs.py does `from bucket import get_client` inside a
        # nested function.  Patch the module attribute so the import succeeds.
        bucket_mod.get_client = MagicMock(return_value=mock_s3)
        try:
            with patch.dict(os.environ, {"BUCKET_NAME": "test-bucket", "ENVIRONMENT": "test"}):
                result = await node.execute(ctx)
        finally:
            del bucket_mod.get_client

        assert "object_key" in result["meta"]
        assert result["meta"]["object_key"].endswith("output.json")
        assert result["meta"]["size_bytes"] > 0


# ============================================================================
# MongoWriterNode
# ============================================================================

class TestEmailSender:

    @pytest.mark.asyncio
    async def test_sends_email(self):
        node = get_node(NodeType.EMAIL_SENDER)
        ctx = NodeContext(
            node_id="em1",
            node_config={
                "to": "user@example.com",
                "subject": "Results",
                "body_template": "Data: {{data}}",
            },
            input_data={"score": 95},
        )
        # EmailSender now posts to the User Service /api/send-workflow-email
        # endpoint (AWS SES under the hood). Mock the httpx call to return 200.
        from unittest.mock import AsyncMock as _AsyncMock
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json = MagicMock(return_value={"ok": True})

        mock_client = MagicMock()
        mock_client.__aenter__ = _AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = _AsyncMock(return_value=False)
        mock_client.post = _AsyncMock(return_value=mock_resp)

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await node.execute(ctx)

        assert result["meta"]["sent"] is True
        assert result["meta"]["to"] == "user@example.com"

    @pytest.mark.asyncio
    async def test_no_smtp_configured(self):
        node = get_node(NodeType.EMAIL_SENDER)
        ctx = NodeContext(
            node_id="em2",
            node_config={"to": "x@x.com", "subject": "test"},
            input_data={},
        )
        # User Service unreachable / returns non-200 → sent=False
        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.post = AsyncMock(side_effect=Exception("connection refused"))
            mock_client_cls.return_value = mock_client
            result = await node.execute(ctx)

        assert result["meta"]["sent"] is False


