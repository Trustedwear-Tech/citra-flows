"""Tests for the workflow AI agent's tool integration.

Covers the upgrades made to ``workflow_engine/nodes/agents.py``:
  • code_execute now wraps ``services.code_executor.execute_code`` (the
    chat Docker sandbox) instead of the in-process AST executor
  • code_execute is enabled by default on new agent nodes
  • MCP data-source tools are pinned explicitly by IT in the node's
    ``mcp_tools`` config (NO discovery) and dispatched straight to the
    configured /query endpoint via ``dept_mcp_client.call_dept_mcp_query``
  • web_search provider is switchable via WORKFLOW_AGENT_SEARCH_PROVIDER
  • NodeContext now carries jwt_token end-to-end
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ─── code_execute schema upgrade ───────────────────────────────────────────


def test_code_execute_schema_uses_script_param_not_code():
    """The new schema must expose `script` + `output_filename` to the LLM
    (matching chat). The legacy `code` param is gone."""
    from citra_workflow.nodes.agents import _build_tool_definitions

    defs = _build_tool_definitions(["code_execute"])
    assert len(defs) == 1
    fn = defs[0]["function"]
    assert fn["name"] == "code_execute"
    props = fn["parameters"]["properties"]
    assert "script" in props
    assert "output_filename" in props
    # Legacy `code` param was removed
    assert "code" not in props
    # Required: only script (output_filename has a default)
    assert "script" in fn["parameters"]["required"]
    # Description must mention pandas + the auto-mounted data.json
    desc = fn["function"]["description"] if "function" in fn else fn["description"]
    assert "pandas" in desc.lower()
    assert "/workspace/input/data.json" in desc


def test_code_execute_default_enabled_in_available_tools_catalog():
    """The catalog entry should have default_enabled=True so the UI
    pre-checks Code Execution on new agent nodes."""
    from citra_workflow.nodes.agents import AVAILABLE_TOOLS

    assert "code_execute" in AVAILABLE_TOOLS
    assert AVAILABLE_TOOLS["code_execute"].get("default_enabled") is True


def test_ai_agent_node_default_tools_includes_code_execute():
    """When a user drags a fresh AI Agent node onto the canvas, code_execute
    must be in the tools field's default value."""
    from citra_workflow.nodes.agents import AIAgentNode

    fields = {f.name: f for f in AIAgentNode.get_fields()}
    assert "tools" in fields
    assert fields["tools"].default == ["code_execute"]


# ─── code_execute Docker-sandbox routing ───────────────────────────────────


@pytest.mark.asyncio
async def test_tool_code_execute_routes_to_chat_sandbox():
    """Calling the tool must call services.code_executor.execute_code with
    the LLM's script, and pipe input_data through as a JSON file."""
    from citra_workflow.nodes import agents as agents_mod

    captured = {}

    async def fake_execute_code(*, script, session_id, files, output_filename):
        captured["script"] = script
        captured["session_id"] = session_id
        captured["files"] = files
        captured["output_filename"] = output_filename
        return {
            "success": True,
            "stdout": "computed: 1234",
            "stderr": "",
            "output_files": [],
        }

    # Stub out S3 upload too (avoids needing real bucket creds)
    fake_upload = MagicMock(return_value="s3://fake/data.json")

    with patch(
        "services.code_executor.execute_code", new=fake_execute_code
    ), patch(
        "bucket.upload_file", new=fake_upload
    ):
        result_str = await agents_mod._tool_code_execute(
            script="import pandas as pd\nprint('computed: 1234')",
            output_filename="result.json",
            session_id="test_session",
            input_data=[{"row": 1}, {"row": 2}],
        )

    import json as _json
    parsed = _json.loads(result_str)
    assert parsed["success"] is True
    assert parsed["stdout"] == "computed: 1234"
    # Sandbox was called with the right script
    assert "import pandas" in captured["script"]
    assert captured["output_filename"] == "result.json"
    # input_data was staged and mounted as a file
    assert any(f["filename"] == "data.json" for f in captured["files"])


@pytest.mark.asyncio
async def test_tool_code_execute_handles_no_input_data():
    """Empty input_data should still succeed — the LLM might just compute
    something self-contained (e.g., generate a fresh PDF)."""
    from citra_workflow.nodes import agents as agents_mod

    async def fake_execute_code(*, script, session_id, files, output_filename):
        return {
            "success": True,
            "stdout": "ok",
            "stderr": "",
            "output_files": [{"filename": "out.json", "download_url": "https://…"}],
        }

    with patch("services.code_executor.execute_code", new=fake_execute_code):
        result_str = await agents_mod._tool_code_execute(
            script="print('ok')",
            input_data=None,
        )
    import json as _json
    parsed = _json.loads(result_str)
    assert parsed["success"] is True
    assert parsed["output_files"][0]["filename"] == "out.json"


@pytest.mark.asyncio
async def test_tool_code_execute_returns_error_dict_on_sandbox_failure():
    from citra_workflow.nodes import agents as agents_mod

    async def fake_fail(*, script, session_id, files, output_filename):
        raise RuntimeError("docker daemon unreachable")

    with patch("services.code_executor.execute_code", new=fake_fail):
        result_str = await agents_mod._tool_code_execute(
            script="print(1)", input_data=None,
        )
    import json as _json
    parsed = _json.loads(result_str)
    assert parsed["success"] is False
    assert "docker daemon" in parsed["stderr"]


# ─── NodeContext jwt_token plumbing ────────────────────────────────────────


def test_node_context_carries_jwt_token():
    from citra_workflow.nodes import NodeContext

    ctx = NodeContext(
        node_id="n1",
        node_config={},
        user_id="u1",
        jwt_token="my-jwt-12345",
    )
    assert ctx.jwt_token == "my-jwt-12345"


def test_node_context_default_jwt_is_none():
    from citra_workflow.nodes import NodeContext

    ctx = NodeContext(node_id="n1", node_config={}, user_id="u1")
    assert ctx.jwt_token is None


# ─── web_search provider switch ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_web_search_uses_chat_provider_when_env_set(monkeypatch):
    """When WORKFLOW_AGENT_SEARCH_PROVIDER=chat, route through chat's
    execute_internet_search instead of DuckDuckGo scraping.

    We inject a fake `citra_internet_service` module into ``sys.modules`` so
    the agent's deferred import picks it up — this avoids loading the real
    module which requires SEARCH_MODEL to be set at import time.
    """
    import sys
    from types import SimpleNamespace
    from citra_workflow.nodes import agents as agents_mod

    monkeypatch.setenv("WORKFLOW_AGENT_SEARCH_PROVIDER", "chat")

    called_with = {}

    def fake_chat_search(query, context="", **_kw):
        called_with["query"] = query
        return "Chat-provider search result"

    fake_module = SimpleNamespace(execute_internet_search=fake_chat_search)
    monkeypatch.setitem(sys.modules, "citra_internet_service", fake_module)

    result = await agents_mod._tool_web_search("what is fifo?")
    assert result == "Chat-provider search result"
    assert called_with["query"] == "what is fifo?"


@pytest.mark.asyncio
async def test_web_search_default_uses_duckduckgo(monkeypatch):
    """Without the env var, the agent falls back to DDG scraping."""
    from citra_workflow.nodes import agents as agents_mod

    monkeypatch.delenv("WORKFLOW_AGENT_SEARCH_PROVIDER", raising=False)

    # Stub httpx so we don't make a real network call
    fake_resp = MagicMock()
    fake_resp.text = '<a class="result__snippet">FIFO is first-in-first-out.</a>'
    fake_resp.raise_for_status = MagicMock()

    fake_client = MagicMock()
    fake_client.get = AsyncMock(return_value=fake_resp)
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=None)

    with patch("httpx.AsyncClient", return_value=fake_client):
        result = await agents_mod._tool_web_search("what is fifo?")
    assert "FIFO" in result


# ─── Cron workflow system-token minter ─────────────────────────────────────


def test_mint_workflow_system_token_returns_valid_hs256(monkeypatch):
    """The minter must produce a JWT that survives a round-trip decode under
    the same JWT_SECRET — proving it can be forwarded as X-User-JWT to
    dept-MCPs on a scheduled (cron) run."""
    import jwt as _jwt
    from citra_auth import mint_workflow_system_token

    monkeypatch.setenv("JWT_SECRET", "x" * 32)
    monkeypatch.setenv("JWT_ISSUER", "Citra-AI-Test")

    token = mint_workflow_system_token("user_owner_123", ttl_seconds=600)
    assert token and isinstance(token, str)

    decoded = _jwt.decode(token, "x" * 32, algorithms=["HS256"])
    assert decoded["user_id"] == "user_owner_123"
    assert decoded["iss"] == "Citra-AI-Test"
    assert decoded["workflow_system"] is True
    # Has exp claim and it's in the future
    import time
    assert decoded["exp"] > int(time.time()) + 500


def test_mint_workflow_system_token_returns_none_without_secret(monkeypatch):
    """If JWT_SECRET is missing, the minter degrades gracefully so scheduled
    runs still execute (without per-source visibility filtering)."""
    from citra_auth import mint_workflow_system_token

    monkeypatch.delenv("JWT_SECRET", raising=False)
    token = mint_workflow_system_token("user_x")
    assert token is None


def test_mint_workflow_system_token_rejects_empty_user_id():
    from citra_auth import mint_workflow_system_token
    assert mint_workflow_system_token("") is None
    assert mint_workflow_system_token(None) is None


def test_mint_workflow_system_token_uses_provided_email(monkeypatch):
    import jwt as _jwt
    from citra_auth import mint_workflow_system_token

    monkeypatch.setenv("JWT_SECRET", "y" * 32)
    token = mint_workflow_system_token("u1", user_email="alice@example.com")
    decoded = _jwt.decode(token, "y" * 32, algorithms=["HS256"])
    assert decoded["email"] == "alice@example.com"


# ─── Org-scoped workflow token (mint_workflow_org_token) ──────────────────
#
# Replaces mint_workflow_system_token above for cron / webhook runs. Tokens
# carry org_id / dept_ids directly so downstream dept-MCPs can filter
# without looking up the (possibly departed) author user record.


def test_mint_workflow_org_token_carries_org_and_dept_claims(monkeypatch):
    """The minted token must round-trip with the workflow's org_id,
    dept_ids, and the workflow_system marker — these are the claims
    dept-MCP per-source visibility filtering reads."""
    import jwt as _jwt
    from citra_auth import mint_workflow_org_token

    monkeypatch.setenv("JWT_SECRET", "x" * 32)
    monkeypatch.setenv("JWT_ISSUER", "Citra-AI-Test")

    token = mint_workflow_org_token(
        workflow_id="wf_abc",
        org_id="acme",
        dept_ids=["claims-ops", "fraud"],
        author_email="alice@acme.com",
        ttl_seconds=600,
    )
    assert token and isinstance(token, str)

    decoded = _jwt.decode(token, "x" * 32, algorithms=["HS256"])
    assert decoded["user_id"] == "workflow-system:wf_abc"
    assert decoded["org_id"] == "acme"
    assert sorted(decoded["dept_ids"]) == ["claims-ops", "fraud"]
    assert decoded["workflow_id"] == "wf_abc"
    assert decoded["author_email"] == "alice@acme.com"
    assert decoded["workflow_system"] is True
    # "user" is included for back-compat with dept-MCPs that haven't yet
    # learned the workflow_system bypass — see source-mcp-template/auth.py.
    assert "user" in decoded["roles"]
    assert "IT-workflow" in decoded["roles"]
    assert "workflow_system" in decoded["roles"]


def test_mint_workflow_org_token_returns_none_without_required_fields(monkeypatch):
    """workflow_id AND org_id are both required — missing either yields None
    so the caller falls back to running without per-source filtering
    rather than minting a malformed token."""
    from citra_auth import mint_workflow_org_token

    monkeypatch.setenv("JWT_SECRET", "x" * 32)
    assert mint_workflow_org_token(workflow_id="", org_id="acme") is None
    assert mint_workflow_org_token(workflow_id="wf_x", org_id="") is None


def test_mint_workflow_org_token_returns_none_without_secret(monkeypatch):
    """If JWT_SECRET is missing the minter degrades gracefully."""
    from citra_auth import mint_workflow_org_token

    monkeypatch.delenv("JWT_SECRET", raising=False)
    token = mint_workflow_org_token(workflow_id="wf_x", org_id="acme")
    assert token is None


def test_mint_workflow_org_token_empty_dept_ids(monkeypatch):
    """A workflow with no dept_ids should still mint cleanly with an
    empty list (not crash, not omit the claim)."""
    import jwt as _jwt
    from citra_auth import mint_workflow_org_token

    monkeypatch.setenv("JWT_SECRET", "x" * 32)
    token = mint_workflow_org_token(
        workflow_id="wf_x", org_id="acme", dept_ids=None,
    )
    decoded = _jwt.decode(token, "x" * 32, algorithms=["HS256"])
    assert decoded["dept_ids"] == []


# ─── System token for Smart-App publishing (mint_system_workflow_token) ───
#
# Used by smart-app-service when publishing a Smart App containing a
# smart_app_action workflow. The publishing BA may not hold IT-workflow
# role, but the platform service does (scoped to the BA's org) with
# on_behalf_of claims so audit + author_user_id reflect the actual BA.


def test_mint_system_workflow_token_carries_on_behalf_of(monkeypatch):
    """The token must record on_behalf_of_user_id / on_behalf_of_email so
    the workflow router can stamp the BA as the workflow's author rather
    than the synthetic system identity."""
    import jwt as _jwt
    from citra_auth import mint_system_workflow_token

    monkeypatch.setenv("JWT_SECRET", "x" * 32)

    token = mint_system_workflow_token(
        org_id="acme",
        on_behalf_of_user_id="alice@acme.com",
        on_behalf_of_email="alice@acme.com",
        dept_ids=["bizops"],
        purpose="smart_app_publish",
    )
    assert token

    decoded = _jwt.decode(token, "x" * 32, algorithms=["HS256"])
    assert decoded["user_id"] == "workflow-publisher:alice@acme.com"
    assert decoded["on_behalf_of_user_id"] == "alice@acme.com"
    assert decoded["on_behalf_of_email"] == "alice@acme.com"
    assert decoded["org_id"] == "acme"
    assert decoded["dept_ids"] == ["bizops"]
    assert decoded["purpose"] == "smart_app_publish"
    assert decoded["workflow_system"] is True
    # Same roles shape as mint_workflow_org_token — token MUST satisfy
    # workflow router's _has_workflow_access check.
    assert "IT-workflow" in decoded["roles"]
    assert "workflow_system" in decoded["roles"]


def test_mint_system_workflow_token_requires_org_and_user(monkeypatch):
    from citra_auth import mint_system_workflow_token

    monkeypatch.setenv("JWT_SECRET", "x" * 32)
    assert mint_system_workflow_token(org_id="", on_behalf_of_user_id="u") is None
    assert mint_system_workflow_token(org_id="acme", on_behalf_of_user_id="") is None


# ─── DISABLE_AUTH bypass safety ───────────────────────────────────────────


def test_disable_auth_only_honoured_in_non_prod(monkeypatch):
    """The bypass must NOT activate when ENVIRONMENT=prod, even if the env
    var is set (defence-in-depth against config leaks)."""
    from citra_auth import _is_auth_disabled

    monkeypatch.setenv("DISABLE_AUTH", "true")

    monkeypatch.setenv("ENVIRONMENT", "production")
    assert _is_auth_disabled() is False

    monkeypatch.setenv("ENVIRONMENT", "prod")
    assert _is_auth_disabled() is False

    monkeypatch.setenv("ENVIRONMENT", "dev")
    assert _is_auth_disabled() is True

    monkeypatch.setenv("ENVIRONMENT", "test")
    assert _is_auth_disabled() is True


def test_disable_auth_off_by_default(monkeypatch):
    """Without DISABLE_AUTH env var, bypass must be inactive."""
    from citra_auth import _is_auth_disabled

    monkeypatch.delenv("DISABLE_AUTH", raising=False)
    monkeypatch.setenv("ENVIRONMENT", "dev")
    assert _is_auth_disabled() is False


def test_disable_auth_accepts_truthy_values(monkeypatch):
    from citra_auth import _is_auth_disabled
    monkeypatch.setenv("ENVIRONMENT", "dev")

    for v in ("true", "1", "yes", "on", "TRUE", "Yes"):
        monkeypatch.setenv("DISABLE_AUTH", v)
        assert _is_auth_disabled() is True, f"failed for {v}"

    for v in ("false", "0", "no", "off", ""):
        monkeypatch.setenv("DISABLE_AUTH", v)
        assert _is_auth_disabled() is False, f"failed for {v}"
