# Copyright (c) 2026 Trustedwear Tech Private Limited (https://citra-ai.com)
# Author: Rohit Kumar Chandan
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may not
# use this file except in compliance with the License. You may obtain a copy of
# the License at http://www.apache.org/licenses/LICENSE-2.0

"""Notification node — post a message to Slack / Teams / a generic incoming
webhook. Complements ``email_sender`` for chat-channel alerts (a workflow
finished, an approval is waiting, an anomaly was found).

Side-effecting + SSRF-guarded. Fails loud on a bad URL or a non-2xx response.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List

from ..models import NodeType, NodeCategory, NodeFieldSchema
from . import BaseNode, NodeContext, register_node, interpolate_variables

logger = logging.getLogger(__name__)


@register_node
class NotifyNode(BaseNode):
    """Send a chat-channel notification via an incoming-webhook URL."""

    node_type = NodeType.NOTIFY
    category = NodeCategory.OUTPUT
    label = "Send Notification"
    description = (
        "Post a message to Slack, Microsoft Teams, or any incoming webhook. "
        "Use for run-finished / approval-waiting / alert notifications."
    )
    icon = "🔔"
    color = "#ec4899"
    side_effecting = True
    ai_authoring_hint = (
        "Use to alert a chat channel (Slack/Teams). Provide the channel's "
        "incoming-webhook URL and a message; {{variable}} placeholders are "
        "supported. For emailing a report use email_sender instead."
    )

    @classmethod
    def get_fields(cls) -> List[NodeFieldSchema]:
        return [
            NodeFieldSchema(
                name="channel_type", label="Channel", type="select", default="slack",
                options=[
                    {"label": "Slack", "value": "slack"},
                    {"label": "Microsoft Teams", "value": "teams"},
                    {"label": "Generic webhook (JSON)", "value": "generic"},
                ],
            ),
            NodeFieldSchema(
                name="webhook_url", label="Incoming Webhook URL", type="text", required=True,
                placeholder="https://hooks.slack.com/services/...",
                help_text="The channel's incoming-webhook URL. Supports {{variable}}.",
            ),
            NodeFieldSchema(
                name="message", label="Message", type="textarea", required=True,
                placeholder="Workflow {{workflow_name}} finished: {{count}} records processed.",
                help_text="Message body. Supports {{variable}} placeholders.",
            ),
            NodeFieldSchema(
                name="timeout_seconds", label="Timeout (seconds)", type="number", default=15,
            ),
        ]

    async def execute(self, ctx: NodeContext) -> Any:
        import httpx

        channel = (ctx.config.get("channel_type") or "slack").strip()
        url = interpolate_variables((ctx.config.get("webhook_url") or "").strip(), ctx.variables)
        message = interpolate_variables((ctx.config.get("message") or "").strip(), ctx.variables)
        if not url:
            raise ValueError("'Incoming Webhook URL' is required.")
        if not message:
            raise ValueError("'Message' is required.")

        from ..utils.ssrf import assert_url_is_public
        assert_url_is_public(url)

        # Slack and Teams incoming webhooks both accept {"text": "..."}; a
        # generic webhook gets a structured envelope.
        if channel in ("slack", "teams"):
            payload: Dict[str, Any] = {"text": message}
        else:
            payload = {"message": message, "source": "citra_workflow",
                       "workflow_id": ctx.workflow_id or None}

        try:
            timeout = float(ctx.config.get("timeout_seconds", 15))
        except (TypeError, ValueError):
            timeout = 15.0

        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()

        logger.info("NotifyNode: %s notification sent (status=%s)", channel, resp.status_code)
        return self._make_output(
            items=[], sent=True, channel=channel, status_code=resp.status_code,
            source="notify",
        )
