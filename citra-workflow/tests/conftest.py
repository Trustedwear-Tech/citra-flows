# Copyright (c) 2026 Trustedwear Tech Private Limited (https://citra-ai.com)
# Author: Rohit Kumar Chandan
# SPDX-License-Identifier: BUSL-1.1
#
# Licensed under the Business Source License 1.1. Non-production use is granted;
# production use requires a commercial licence until the Change Date, after
# which this file converts to Apache-2.0. See LICENSE at the repository root.

"""
Shared fixtures for workflow engine tests.
All I/O (MongoDB, Redis, S3, LLM) is mocked for unit tests.
"""

import sys
import os
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

# Register E2E fixtures so pytest discovers them
pytest_plugins = ["tests.conftest_e2e"]
from datetime import datetime

# Ensure project root is on the path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

os.environ["DISABLE_AUTH"] = "true"
# Provide minimum LLM tier config so unit tests that exercise
# `_run_agent_with_tools` can call `get_llm_client(...)` without hitting the
# tier-config validator. The actual OpenAI client is mocked per-test.
os.environ.setdefault("LLM_BASE_URL", "http://test-llm.local/v1")
os.environ.setdefault("LLM_API_KEY", "test-key")
os.environ.setdefault("LLM_MODEL", "test-model")


# ─── Phase J cross-service stubs ───────────────────────────────────────
# Before the split, citra-workflow was IN-process inside Citra-Service, so
# `from services.code_executor import execute_code` (and a few similar
# `services.*` imports inside agent / router code paths) resolved
# naturally via PYTHONPATH. After the split, citra-workflow runs in its
# own process and those imports raise ModuleNotFoundError.
#
# Production: those code paths call Citra-Service over HTTP (the agent
# returns a graceful "Sandbox unavailable" tool result; tests for that
# path live behind @pytest.mark.integration).
#
# Tests: stub the modules with permissive defaults so each test can
# override the behaviour via the normal `patch("services.X.Y", ...)`
# pattern.
import types as _types
from unittest.mock import AsyncMock as _AsyncMock, MagicMock as _MagicMock


def _install_services_stubs():
    """Register fake `services.*` modules into sys.modules so the
    citra_workflow.nodes.agents inline imports resolve. Each function
    returns a sane default that tests can monkey-patch."""
    # services (package)
    services_pkg = _types.ModuleType("services")
    services_pkg.__path__ = []
    sys.modules.setdefault("services", services_pkg)

    # services.code_executor.execute_code
    exec_mod = _types.ModuleType("services.code_executor")

    async def _fake_execute_code(script, session_id=None, files=None, output_filename="output.json"):
        return {
            "success": True,
            "stdout": "",
            "stderr": "",
            "output_files": [],
        }

    exec_mod.execute_code = _fake_execute_code
    sys.modules.setdefault("services.code_executor", exec_mod)
    services_pkg.code_executor = exec_mod

    # services.enterprise_tools (used by agent tool-call dispatch)
    et_mod = _types.ModuleType("services.enterprise_tools")
    et_mod.build_enterprise_tool_schemas = _MagicMock(return_value=[])
    et_mod.dispatch_enterprise_tool = _AsyncMock(return_value={"ok": True})
    et_mod._slugify_tool_name = lambda name: str(name).lower().replace(" ", "_")
    sys.modules.setdefault("services.enterprise_tools", et_mod)
    services_pkg.enterprise_tools = et_mod

    # services.enterprise_mcp_client (used by router /agent-tools and the
    # AI-builder MCP-source context fetch — list-all + query-ranked search)
    mcp_mod = _types.ModuleType("services.enterprise_mcp_client")
    mcp_mod.discover_tools = _AsyncMock(return_value=[])
    mcp_mod.search_tools = _AsyncMock(return_value=[])
    sys.modules.setdefault("services.enterprise_mcp_client", mcp_mod)
    services_pkg.enterprise_mcp_client = mcp_mod

    # bucket (Citra-Service's S3 client helper) — used by output nodes
    # (BucketWriter etc.). Same rationale as services.* — production
    # calls Citra-Service over HTTP; tests stub it.
    bucket_mod = _types.ModuleType("bucket")
    bucket_mod.get_client = _MagicMock()
    bucket_mod._get_client = _MagicMock()
    bucket_mod.get_config = _MagicMock(return_value=("test-bucket", "us-east-1"))
    bucket_mod.get_environment_prefix = _MagicMock(return_value="test")
    bucket_mod.sanitize_container_name = lambda s: str(s).replace("@", "_at_").lower()
    bucket_mod.upload_file = _MagicMock(return_value={"ok": True})
    bucket_mod.download_file = _MagicMock(return_value=b"")
    sys.modules.setdefault("bucket", bucket_mod)


_install_services_stubs()


@pytest.fixture(autouse=True)
def _clear_llm_client_cache():
    """Clear cached LLM clients between tests so per-test ``patch('openai.OpenAI')``
    is honored on every test (clients are normally memoized in llm_client).

    Also rewires ``llm_client._make_client`` to resolve ``openai.OpenAI`` /
    ``openai.AsyncOpenAI`` at call time, so tests that patch ``openai.OpenAI``
    take effect (the default ``_make_client`` captures the symbol at module
    import time, before tests can patch it)."""
    try:
        # citra-llm's private state lives in citra_llm.client, not the
        # top-level citra_llm namespace. Reach into the submodule.
        from citra_llm import client as llm_client
        import openai as _openai_mod
        llm_client._sync_clients.clear()
        llm_client._async_clients.clear()

        def _make_client_dynamic(base_url, api_key, async_=False, timeout=None):
            kwargs = {"base_url": base_url, "api_key": api_key or "test-key"}
            if timeout is not None:
                kwargs["timeout"] = timeout
            cls = _openai_mod.AsyncOpenAI if async_ else _openai_mod.OpenAI
            return cls(**kwargs)

        original = llm_client._make_client
        llm_client._make_client = _make_client_dynamic
        try:
            yield
        finally:
            llm_client._make_client = original
            llm_client._sync_clients.clear()
            llm_client._async_clients.clear()
    except Exception:
        yield


# ============================================================================
# Mock Fixtures
# ============================================================================

@pytest.fixture
def mock_cache():
    """Mock Redis cache manager."""
    cache = MagicMock()
    cache.set = MagicMock()
    cache.get = MagicMock(return_value=None)
    cache.delete = MagicMock()
    cache.eval = MagicMock(return_value=1)  # default: concurrency slot acquired
    return cache


@pytest.fixture
def mock_mongo_col():
    """Mock MongoDB async collection."""
    col = AsyncMock()
    col.insert_one = AsyncMock(return_value=MagicMock(inserted_id="mock_id"))
    col.update_one = AsyncMock()
    col.find_one = AsyncMock(return_value=None)
    col.find = MagicMock()
    col.find.return_value.to_list = AsyncMock(return_value=[])
    return col


@pytest.fixture
def mock_mongo_db(mock_mongo_col):
    """Mock MongoDB async database with collection accessor."""
    db = MagicMock()
    db.__getitem__ = MagicMock(return_value=mock_mongo_col)
    return db


@pytest.fixture
def mock_mongo_client(mock_mongo_db):
    """Mock async mongo client."""
    client = MagicMock()
    client.__getitem__ = MagicMock(return_value=mock_mongo_db)
    return client


@pytest.fixture
def mock_llm_response():
    """Factory for mock OpenAI chat completion responses."""
    def _make(content="Mock LLM response", tool_calls=None):
        choice = MagicMock()
        choice.message.content = content
        choice.message.tool_calls = tool_calls
        resp = MagicMock()
        resp.choices = [choice]
        return resp
    return _make


@pytest.fixture
def mock_httpx_response():
    """Factory for mock httpx responses."""
    def _make(status_code=200, json_data=None, text=""):
        resp = MagicMock()
        resp.status_code = status_code
        resp.text = text
        resp.json.return_value = json_data or {}
        resp.raise_for_status = MagicMock()
        if status_code >= 400:
            import httpx
            resp.raise_for_status.side_effect = httpx.HTTPStatusError(
                "error", request=MagicMock(), response=resp
            )
        return resp
    return _make


# ============================================================================
# Workflow Factory Fixtures
# ============================================================================

@pytest.fixture
def make_node():
    """Factory for creating NodeDefinition dicts."""
    from citra_workflow.models import NodeDefinition, NodeType

    def _make(node_id: str, node_type: NodeType, label: str = "", config: dict = None):
        return NodeDefinition(
            id=node_id,
            type=node_type,
            label=label or node_id,
            config=config or {},
        )
    return _make


@pytest.fixture
def make_edge():
    """Factory for creating EdgeDefinition dicts."""
    from citra_workflow.models import EdgeDefinition

    def _make(source: str, target: str, source_handle: str = None):
        return EdgeDefinition(
            id=f"{source}->{target}",
            source=source,
            target=target,
            source_handle=source_handle,
        )
    return _make


@pytest.fixture
def sample_linear_workflow(make_node, make_edge):
    """Trigger -> Processor -> Output (3 nodes, linear)."""
    from citra_workflow.models import WorkflowDefinition, NodeType

    return WorkflowDefinition(
        workflow_id="wf-linear-1",
        user_id="test-user",
        name="Linear Test Workflow",
        nodes=[
            make_node("trigger", NodeType.MANUAL_TRIGGER),
            make_node("proc", NodeType.LLM_PROCESSOR, config={
                "prompt": "Summarize: {{input}}",
                "processing_mode": "all",
            }),
            make_node("out", NodeType.WEBHOOK_OUTPUT, config={
                "url": "https://example.com/hook",
                "method": "POST",
            }),
        ],
        edges=[
            make_edge("trigger", "proc"),
            make_edge("proc", "out"),
        ],
    )


@pytest.fixture
def sample_condition_workflow(make_node, make_edge):
    """Trigger -> Condition -> True/False branches."""
    from citra_workflow.models import WorkflowDefinition, NodeType

    return WorkflowDefinition(
        workflow_id="wf-cond-1",
        user_id="test-user",
        name="Condition Test Workflow",
        nodes=[
            make_node("trigger", NodeType.MANUAL_TRIGGER),
            make_node("cond", NodeType.CONDITION, config={
                "field": "count",
                "operator": ">",
                "value": "10",
            }),
            make_node("true_branch", NodeType.LLM_PROCESSOR, config={
                "prompt": "High count",
                "processing_mode": "all",
            }),
            make_node("false_branch", NodeType.LLM_PROCESSOR, config={
                "prompt": "Low count",
                "processing_mode": "all",
            }),
        ],
        edges=[
            make_edge("trigger", "cond"),
            make_edge("cond", "true_branch", source_handle="true"),
            make_edge("cond", "false_branch", source_handle="false"),
        ],
    )


@pytest.fixture
def sample_switch_workflow(make_node, make_edge):
    """Trigger -> Switch -> 3 output routes."""
    from citra_workflow.models import WorkflowDefinition, NodeType

    return WorkflowDefinition(
        workflow_id="wf-switch-1",
        user_id="test-user",
        name="Switch Test Workflow",
        nodes=[
            make_node("trigger", NodeType.MANUAL_TRIGGER),
            make_node("switch", NodeType.SWITCH_ROUTER, config={
                "field": "category",
                "routes": [
                    {"label": "Route A", "value": "a"},
                    {"label": "Route B", "value": "b"},
                    {"label": "Default", "value": "__default__"},
                ],
            }),
            make_node("route_a", NodeType.LLM_PROCESSOR, config={
                "prompt": "Route A",
                "processing_mode": "all",
            }),
            make_node("route_b", NodeType.LLM_PROCESSOR, config={
                "prompt": "Route B",
                "processing_mode": "all",
            }),
            make_node("route_default", NodeType.LLM_PROCESSOR, config={
                "prompt": "Default",
                "processing_mode": "all",
            }),
        ],
        edges=[
            make_edge("trigger", "switch"),
            make_edge("switch", "route_a", source_handle="out-0"),
            make_edge("switch", "route_b", source_handle="out-1"),
            make_edge("switch", "route_default", source_handle="out-2"),
        ],
    )


@pytest.fixture
def sample_approval_workflow(make_node, make_edge):
    """Trigger -> Approval -> Output."""
    from citra_workflow.models import WorkflowDefinition, NodeType

    return WorkflowDefinition(
        workflow_id="wf-approval-1",
        user_id="test-user",
        name="Approval Test Workflow",
        nodes=[
            make_node("trigger", NodeType.MANUAL_TRIGGER),
            make_node("approval", NodeType.HUMAN_APPROVAL, config={
                "message": "Please approve",
                "timeout_hours": 24,
            }),
            make_node("out", NodeType.WEBHOOK_OUTPUT, config={
                "url": "https://example.com/hook",
                "method": "POST",
            }),
        ],
        edges=[
            make_edge("trigger", "approval"),
            make_edge("approval", "out"),
        ],
    )


# ============================================================================
# Executor Fixture (with all I/O mocked)
# ============================================================================

@pytest.fixture
def mock_executor(mock_cache, mock_mongo_client):
    """WorkflowExecutor with mocked cache and MongoDB."""
    with patch("citra_cache.get_cache_manager", return_value=mock_cache):
        from citra_workflow.executor import WorkflowExecutor
        executor = WorkflowExecutor()
        executor.cache = mock_cache

    # Patch MongoDB calls inside executor methods
    executor._save_execution = AsyncMock()
    executor._create_approval_request = AsyncMock(return_value="approval-123")
    return executor


@pytest.fixture
def patched_executor(mock_cache, mock_mongo_client):
    """WorkflowExecutor with cache mocked but _save_execution / _create_approval intact
    for testing those methods directly."""
    with patch("citra_cache.get_cache_manager", return_value=mock_cache):
        from citra_workflow.executor import WorkflowExecutor
        executor = WorkflowExecutor()
        executor.cache = mock_cache
    return executor
