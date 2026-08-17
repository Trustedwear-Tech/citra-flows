# Copyright (c) 2026 Trustedwear Tech Private Limited (https://citra-ai.com)
# Author: Rohit Kumar Chandan
# SPDX-License-Identifier: BUSL-1.1
#
# Licensed under the Business Source License 1.1. Non-production use is granted;
# production use requires a commercial licence until the Change Date, after
# which this file converts to Apache-2.0. See LICENSE at the repository root.

"""
Unit tests for the POST /api/workflows/generate-code endpoint.
"""

import sys
import os
import pytest
from unittest.mock import patch, AsyncMock, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))


class TestGenerateCodeEndpoint:

    @pytest.fixture(autouse=True)
    def _bypass_workflow_auth(self):
        """generate_code is gated by _require_workflow_access; these tests call
        the handler directly with a MagicMock request (no auth middleware), so
        stub the gate to a workflow-capable identity. The handler ignores the
        returned claims (it only uses get_secure_user_id, patched per-test)."""
        with patch(
            "citra_workflow.router._require_workflow_access",
            return_value={"user_id": "u", "org_id": "test_org",
                          "roles": ["super_admin"], "dept_ids": []},
        ):
            yield

    @pytest.mark.asyncio
    async def test_returns_generated_code(self):
        from citra_workflow.router import generate_code, GenerateCodeRequest

        request = MagicMock()
        with patch("citra_workflow.router.get_secure_user_id", return_value="user123"):
            with patch("citra_llm.oss.llm_call", new_callable=AsyncMock, return_value="result = sum(data.get('values', []))") as mock_llm:
                body = GenerateCodeRequest(prompt="Sum the values list")
                resp = await generate_code(request, body)
                assert resp["code"] == "result = sum(data.get('values', []))"
                mock_llm.assert_called_once()
                call_kwargs = mock_llm.call_args
                assert call_kwargs.kwargs["user_id"] == "user123"
                assert "data" in call_kwargs.kwargs["system"]

    @pytest.mark.asyncio
    async def test_strips_markdown_fences(self):
        from citra_workflow.router import generate_code, GenerateCodeRequest

        request = MagicMock()
        fenced_code = "```python\nresult = 42\n```"

        with patch("citra_workflow.router.get_secure_user_id", return_value="user1"):
            with patch("citra_llm.oss.llm_call", new_callable=AsyncMock, return_value=fenced_code):
                body = GenerateCodeRequest(prompt="Return 42")
                resp = await generate_code(request, body)
                assert resp["code"] == "result = 42"

    @pytest.mark.asyncio
    async def test_strips_plain_fences(self):
        from citra_workflow.router import generate_code, GenerateCodeRequest

        request = MagicMock()
        fenced_code = "```\nresult = 'hello'\n```"

        with patch("citra_workflow.router.get_secure_user_id", return_value="user1"):
            with patch("citra_llm.oss.llm_call", new_callable=AsyncMock, return_value=fenced_code):
                body = GenerateCodeRequest(prompt="Return hello")
                resp = await generate_code(request, body)
                assert resp["code"] == "result = 'hello'"

    @pytest.mark.asyncio
    async def test_passes_input_schema_to_prompt(self):
        from citra_workflow.router import generate_code, GenerateCodeRequest

        request = MagicMock()
        schema = {"records": "list of dicts with 'name' and 'score'"}

        with patch("citra_workflow.router.get_secure_user_id", return_value="user1"):
            with patch("citra_llm.oss.llm_call", new_callable=AsyncMock, return_value="result = data") as mock_llm:
                body = GenerateCodeRequest(prompt="Pass through", input_schema=schema)
                await generate_code(request, body)
                system_prompt = mock_llm.call_args.kwargs["system"]
                assert "input schema" in system_prompt.lower()

    @pytest.mark.asyncio
    async def test_no_fence_passthrough(self):
        from citra_workflow.router import generate_code, GenerateCodeRequest

        request = MagicMock()
        clean_code = "x = [r for r in data['items'] if r > 0]\nresult = x"

        with patch("citra_workflow.router.get_secure_user_id", return_value="user1"):
            with patch("citra_llm.oss.llm_call", new_callable=AsyncMock, return_value=clean_code):
                body = GenerateCodeRequest(prompt="Filter items")
                resp = await generate_code(request, body)
                assert resp["code"] == clean_code
