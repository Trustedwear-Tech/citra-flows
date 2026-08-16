"""Tests for the AI Agent node's read-only enforcement guard.

The agent LLM node is contractually READ-ONLY. A user attaches tools so the
LLM can *read* enterprise data, never mutate it. These tests cover the guard
added to ``citra_workflow/nodes/agents.py``:

  • ``_assert_tool_is_read_only`` raises ``WriteBlockedError`` for write
    attempts (POST/PUT/DELETE via http_request, write-verb dept_* tools,
    write-named unknown tools) and passes read tools through.
  • A blocked write is audited to the dedicated write-block log file.
  • Inside the agent loop, a blocked write PROPAGATES OUT (fails the node) —
    it is NOT caught and fed back to the LLM as a tool result. (RULE #1.)
  • The http_request schema only advertises GET/HEAD/OPTIONS to the model.
"""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


# ─── _assert_tool_is_read_only: read tools pass ────────────────────────────


@pytest.mark.parametrize(
    "fn_name, fn_args",
    [
        ("web_search", {"query": "x"}),
        ("sql_query", {"sql": "SELECT 1"}),
        ("nosql_query", {"collection": "c", "filter": {}}),
        ("bucket_read", {"action": "list"}),
        ("sftp_read", {"action": "list"}),
        ("code_execute", {"script": "print(1)"}),
        ("http_request", {"url": "https://api.x/data", "method": "GET"}),
        ("http_request", {"url": "https://api.x/data", "method": "head"}),
        ("http_request", {"url": "https://api.x/data"}),  # defaults to GET
    ],
)
def test_read_tools_pass(fn_name, fn_args):
    from citra_workflow.nodes.agents import _assert_tool_is_read_only

    # Must not raise.
    _assert_tool_is_read_only(fn_name, fn_args)


def test_read_classified_dept_tool_passes():
    from citra_workflow.nodes.agents import _assert_tool_is_read_only

    tool_def = {"source_id": "finance_ledger", "query_endpoint": "http://x/query", "verb": "query"}
    _assert_tool_is_read_only("dept_finance_ledger", {"query": "q"}, tool_def=tool_def)


def test_dept_tool_with_data_named_updates_is_not_a_false_positive():
    """A source named 'customer_updates' must NOT trip the write-verb matcher
    (word-boundary anchored — 'updates' != 'update')."""
    from citra_workflow.nodes.agents import _assert_tool_is_read_only

    _assert_tool_is_read_only(
        "dept_customer_updates",
        {"query": "q"},
        tool_def={"source_id": "customer_updates", "query_endpoint": "http://x/query"},
    )


# ─── _assert_tool_is_read_only: write attempts blocked ─────────────────────


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE", "post", "Put"])
def test_http_request_write_methods_blocked(method):
    from citra_workflow.nodes.agents import _assert_tool_is_read_only, WriteBlockedError

    with pytest.raises(WriteBlockedError) as ei:
        _assert_tool_is_read_only("http_request", {"url": "https://x/", "method": method})
    assert ei.value.fn_name == "http_request"
    assert method.upper() in str(ei.value)


def test_dept_tool_with_write_verb_name_blocked():
    from citra_workflow.nodes.agents import _assert_tool_is_read_only, WriteBlockedError

    tool_def = {"source_id": "create_ticket", "query_endpoint": "http://x/query"}
    with pytest.raises(WriteBlockedError):
        _assert_tool_is_read_only("dept_create_ticket", {"query": "q"}, tool_def=tool_def)


def test_unknown_write_named_tool_blocked():
    from citra_workflow.nodes.agents import _assert_tool_is_read_only, WriteBlockedError

    with pytest.raises(WriteBlockedError):
        _assert_tool_is_read_only("delete_record", {"id": "1"})


@pytest.mark.parametrize("source", ["asset_data", "customer_updates", "dataset_sales", "result_set_log"])
def test_dept_read_source_names_not_false_positive(source):
    """Legit read source names that merely contain a verb-like substring must
    NOT be blocked — the dept heuristic is a START-of-name match."""
    from citra_workflow.nodes.agents import _assert_tool_is_read_only

    _assert_tool_is_read_only(
        f"dept_{source}",
        {"query": "q"},
        tool_def={"source_id": source, "query_endpoint": "http://x/query"},
    )


# ─── WriteBlockedError is non-retryable ────────────────────────────────────


def test_write_blocked_error_is_non_retryable():
    from citra_workflow.nodes.agents import WriteBlockedError

    assert getattr(WriteBlockedError("t", "r"), "non_retryable", False) is True


@pytest.mark.asyncio
async def test_node_run_does_not_retry_write_blocked(monkeypatch):
    """A node whose execute() raises WriteBlockedError must fail on the FIRST
    attempt even when max_retries is configured — no re-runs."""
    from citra_workflow.nodes import BaseNode, NodeContext, NodeExecutionStatus
    from citra_workflow.nodes.agents import WriteBlockedError
    from citra_workflow.models import NodeType, NodeCategory

    calls = {"n": 0}

    class _BlockingNode(BaseNode):
        node_type = NodeType.AI_AGENT
        category = NodeCategory.AGENT
        label = "Blocking"

        async def execute(self, ctx):
            calls["n"] += 1
            raise WriteBlockedError("http_request", "POST blocked")

    ctx = NodeContext(
        node_id="n1",
        node_config={"max_retries": 3, "retry_delay_seconds": 0},
        user_id="u1",
    )
    result = await _BlockingNode().run(ctx)

    assert result.status == NodeExecutionStatus.FAILED
    assert calls["n"] == 1  # executed once, not 1 + max_retries


# ─── http_request schema only advertises read methods ──────────────────────


def test_http_request_schema_only_offers_read_methods():
    from citra_workflow.nodes.agents import _build_tool_definitions

    defs = _build_tool_definitions(["http_request"])
    fn = defs[0]["function"]
    assert fn["name"] == "http_request"
    method_enum = fn["parameters"]["properties"]["method"]["enum"]
    assert set(method_enum) == {"GET", "HEAD", "OPTIONS"}
    # The write-only `body` param is gone from the schema.
    assert "body" not in fn["parameters"]["properties"]


# ─── audit logging ─────────────────────────────────────────────────────────


def test_audit_write_block_writes_to_file(tmp_path, monkeypatch):
    from citra_workflow.nodes import agents as agents_mod

    log_file = tmp_path / "blocks.log"
    monkeypatch.setenv("WF_WRITE_BLOCK_LOG_FILE", str(log_file))
    # Force the lazily-built logger to re-init against the temp path.
    monkeypatch.setattr(agents_mod, "_write_block_logger", None)

    err = agents_mod.WriteBlockedError("http_request", "POST blocked", {"url": "https://x/"})
    agents_mod._audit_write_block(
        err,
        user_id="u1",
        org_id="acme",
        workflow_id="wf_1",
        node_id="n_1",
        execution_id="ex_1",
        iteration=2,
    )
    # Flush handlers so the file is written.
    for h in agents_mod._get_write_block_logger().handlers:
        h.flush()

    content = log_file.read_text(encoding="utf-8")
    assert "agent_write_blocked" in content
    record_json = content[content.index("{"):content.rindex("}") + 1]  # strip "asctime LEVEL " prefix
    rec = json.loads(record_json)
    assert rec["tool"] == "http_request"
    assert rec["workflow_id"] == "wf_1"
    assert rec["node_id"] == "n_1"
    assert rec["user_id"] == "u1"


# ─── integration: write attempt fails the node, never fed back to LLM ──────


@pytest.mark.asyncio
async def test_agent_loop_blocks_post_http_request_and_raises(tmp_path, monkeypatch):
    """When the LLM emits a POST http_request call, the agent loop must raise
    WriteBlockedError out (fail the node) — NOT swallow it into a tool result
    and continue. The attempt is audited to the log file."""
    from citra_workflow.nodes import agents as agents_mod

    log_file = tmp_path / "blocks.log"
    monkeypatch.setenv("WF_WRITE_BLOCK_LOG_FILE", str(log_file))
    monkeypatch.setattr(agents_mod, "_write_block_logger", None)

    tool_call = SimpleNamespace(
        id="tc1",
        function=SimpleNamespace(
            name="http_request",
            arguments='{"url":"https://api.x/orders","method":"POST","body":"{}"}',
        ),
    )
    msg = SimpleNamespace(content=None, tool_calls=[tool_call])
    msg.model_dump = lambda: {"role": "assistant", "content": None, "tool_calls": []}
    resp = SimpleNamespace(choices=[SimpleNamespace(message=msg, finish_reason="tool_calls")])

    fake_client = MagicMock()
    fake_client.chat.completions.create = MagicMock(return_value=resp)

    with patch("citra_llm.get_llm_client", return_value=fake_client), \
         patch("citra_llm.get_llm_model", return_value="test-model"), \
         patch("citra_llm.get_llm_extra_body", return_value={}):
        with pytest.raises(agents_mod.WriteBlockedError) as ei:
            await agents_mod._run_agent_with_tools(
                system_prompt="agent",
                user_message="create an order",
                tier="large",
                tool_names=["http_request"],
                user_id="u1",
                workflow_id="wf_1",
                node_id="n_1",
                execution_id="ex_1",
            )

    assert ei.value.fn_name == "http_request"
    # The LLM was called exactly once — the loop did not continue after the
    # block (the write was not fed back as a tool result for another round).
    assert fake_client.chat.completions.create.call_count == 1

    # Audit record was written to the dedicated file.
    for h in agents_mod._get_write_block_logger().handlers:
        h.flush()
    assert "agent_write_blocked" in log_file.read_text(encoding="utf-8")
