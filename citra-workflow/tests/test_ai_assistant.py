"""
Unit tests for the agentic workflow AI Assistant (citra_workflow.ai_assistant).

The LLM client is mocked: each test scripts a sequence of chat-completion
responses (tool-call rounds then a final answer) and asserts the events the
agent loop yields. No real LLM, DB, or network.
"""

import asyncio
import contextlib
import json
import sys
import os
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from citra_workflow import ai_assistant


# ── Mock helpers ────────────────────────────────────────────────────────

def _tool_call(call_id, name, args):
    tc = MagicMock()
    tc.id = call_id
    tc.function = MagicMock()
    tc.function.name = name
    tc.function.arguments = json.dumps(args)
    return tc


def _response(content=None, tool_calls=None):
    msg = MagicMock()
    msg.content = content
    msg.tool_calls = tool_calls or None
    choice = MagicMock()
    choice.message = msg
    choice.finish_reason = "tool_calls" if tool_calls else "stop"
    resp = MagicMock()
    resp.choices = [choice]
    usage = MagicMock()
    usage.prompt_tokens = 100
    usage.completion_tokens = 20
    usage.prompt_tokens_details = None
    resp.usage = usage
    return resp


def _client(responses):
    """Mock LLM client whose chat.completions.create returns `responses` in order."""
    client = MagicMock()
    client.chat.completions.create = MagicMock(side_effect=list(responses))
    return client


def _run(client, *, prompt="hi", workflow=None):
    """Drive run_workflow_assistant with all I/O patched, return the event list."""
    workflow = workflow if workflow is not None else {"nodes": [], "edges": [], "variables": {}}
    context = {"connections": [], "catalogue": [], "mcp_sources": []}

    async def _collect():
        out = []
        with patch.object(ai_assistant, "get_llm_client", return_value=client), \
             patch.object(ai_assistant, "get_default_model", return_value="test-model"), \
             patch.object(ai_assistant, "get_llm_extra_body", return_value={}):
            async for ev in ai_assistant.run_workflow_assistant(
                prompt=prompt, workflow=workflow, conversation_block="",
                focused_node_id=None, context=context,
                user_id="u1", user_email="u1@test",
            ):
                out.append(ev)
        return out

    # Sync test driver: run the async generator to completion in its own loop.
    return asyncio.run(_collect())


# ── Tests ───────────────────────────────────────────────────────────────

def test_answer_path_no_operation():
    """A question → inspect_node then a prose answer. No operation emitted."""
    wf = {"nodes": [{"id": "trigger", "type": "manual_trigger", "config": {}}], "edges": []}
    client = _client([
        _response(tool_calls=[_tool_call("c1", "inspect_node", {"node_id": "trigger"})]),
        _response(content="This workflow starts manually and does nothing else yet."),
    ])
    events = _run(client, prompt="what does this do?", workflow=wf)

    assert not any(e["type"] == "operation" for e in events)
    done = [e for e in events if e["type"] == "done"]
    assert len(done) == 1
    assert "starts manually" in done[0]["message"]
    assert done[0]["operations"] == []


def test_update_node_delta_emits_diff_operation():
    """A single-field edit → update_node delta yields ONE workflow op whose diff
    marks n1 updated. The model emitted only the changed field, not the node."""
    wf = {"nodes": [{"id": "n1", "type": "set_variable", "label": "old",
                     "config": {"assignments": [{"name": "x", "value": ""}]}}], "edges": []}
    client = _client([
        _response(tool_calls=[_tool_call("c1", "update_node", {"node_id": "n1", "changes": {"label": "Set X"}})]),
        _response(content="Renamed the node — Apply when ready."),
    ])
    events = _run(client, prompt="rename node n1 to Set X", workflow=wf)

    ops = [e for e in events if e["type"] == "operation"]
    assert len(ops) == 1
    op = ops[0]["operation"]
    assert op["type"] == "workflow"
    updated_ids = [n.get("id") for n in (op["diff"] or {}).get("nodes_updated", [])]
    assert "n1" in updated_ids
    # Deep-merge preserved the existing config key while changing the label.
    new_n1 = next(n for n in op["workflow"]["nodes"] if n["id"] == "n1")
    assert new_n1["label"] == "Set X"
    assert new_n1["config"]["assignments"] == [{"name": "x", "value": ""}]


def test_add_node_and_edge_deltas_emit_diff():
    """Add-a-step via add_node + add_edge deltas → ONE diff with the addition."""
    current = {"nodes": [{"id": "trigger", "type": "manual_trigger", "config": {}}], "edges": []}
    client = _client([
        _response(tool_calls=[_tool_call("c1", "add_node",
                  {"node": {"id": "set1", "type": "set_variable",
                            "config": {"assignments": [{"name": "x", "value": "1"}]}}})]),
        _response(tool_calls=[_tool_call("c2", "add_edge", {"source": "trigger", "target": "set1"})]),
        _response(content="Added a set-variable step. Apply to canvas?"),
    ])
    events = _run(client, prompt="add a set variable step after the trigger", workflow=current)

    ops = [e for e in events if e["type"] == "operation"]
    assert len(ops) == 1
    op = ops[0]["operation"]
    assert op["type"] == "workflow" and op["diff"] is not None
    added_ids = [n.get("id") for n in op["diff"].get("nodes_added", [])]
    assert "set1" in added_ids
    assert len(op["diff"].get("edges_added", [])) == 1


def test_loop_stops_at_max_rounds():
    """A model that never stops calling tools terminates at MAX_ROUNDS with a done event."""
    wf = {"nodes": [{"id": "trigger", "type": "manual_trigger", "config": {}}], "edges": []}
    # Always return a tool call → never a final answer.
    client = MagicMock()
    client.chat.completions.create = MagicMock(
        side_effect=lambda **kw: _response(
            tool_calls=[_tool_call("c", "inspect_node", {"node_id": "trigger"})]
        )
    )
    with patch.object(ai_assistant, "MAX_ROUNDS", 3):
        events = _run(client, prompt="loop forever", workflow=wf)

    done = [e for e in events if e["type"] == "done"]
    assert len(done) == 1
    assert "ran out of steps" in done[0]["message"]
    # Exactly MAX_ROUNDS LLM calls were made.
    assert client.chat.completions.create.call_count == 3


# ── Full-detail input ───────────────────────────────────────────────────

def test_opening_message_includes_full_node_config():
    """Every tiny detail — full node config, variables, meta — is inlined."""
    wf = {
        "workflow_id": "wf1", "name": "W", "description": "desc",
        "variables": {"region": "apac"},
        "nodes": [{"id": "n1", "type": "llm_processor", "label": "L",
                   "config": {"prompt": "SECRET_PROMPT_XYZ", "tier": "large", "temperature": 0.2}}],
        "edges": [{"id": "e", "source": "a", "target": "n1", "source_handle": "true"}],
    }
    msg = ai_assistant.build_opening_message(
        prompt="explain", workflow=wf, conversation_block="", focused_node_id=None)
    assert "COMPLETE JSON" in msg
    assert "SECRET_PROMPT_XYZ" in msg     # full config value present
    assert "temperature" in msg
    assert "apac" in msg                  # workflow variables present
    assert "source_handle" in msg         # edge detail present


def test_oversized_workflow_lists_every_node_and_defers_config(monkeypatch):
    """If the workflow can't be inlined, EVERY node + its config keys are still
    listed, with a pointer to inspect_node — nothing is silently dropped."""
    monkeypatch.setattr(ai_assistant, "_MAX_WORKFLOW_CHARS", 50)
    wf = {"name": "W",
          "nodes": [{"id": "big", "type": "code_block", "label": "B",
                     "config": {"code": "x" * 500}}],
          "edges": []}
    msg = ai_assistant.build_opening_message(
        prompt="fix", workflow=wf, conversation_block="", focused_node_id=None)
    assert "inspect_node" in msg
    assert "id='big'" in msg
    assert "config_keys=['code']" in msg
    assert "x" * 500 not in msg  # the giant value is deferred, not inlined


# ── Empty / malformed tool-call recovery ────────────────────────────────

def test_empty_create_workflow_rejected_then_retried():
    """A create_workflow with no payload (model described it in prose instead of
    the args) is rejected, so the model re-calls with the real nodes — we never
    emit a 0-node 'proposal' with no Apply button."""
    good = {
        "nodes": [{"id": "t", "type": "manual_trigger", "config": {}},
                  {"id": "n1", "type": "set_variable", "config": {"assignments": [{"name": "x", "value": "1"}]}}],
        "edges": [{"id": "e", "source": "t", "target": "n1"}],
    }
    client = _client([
        _response(tool_calls=[_tool_call("c1", "create_workflow", {})]),                # empty → error
        _response(tool_calls=[_tool_call("c2", "create_workflow", {"workflow": good})]),  # real payload
        _response(content="Built it — Apply when ready."),
    ])
    events = _run(client, prompt="build it", workflow={"nodes": [], "edges": []})

    ops = [e for e in events if e["type"] == "operation"]
    assert len(ops) == 1
    assert len(ops[0]["operation"]["workflow"]["nodes"]) == 2  # not a 0-node phantom


def test_malformed_tool_args_fed_back_not_crashed():
    """Truncated/invalid tool-call JSON is reported back to the model (which then
    re-issues a valid call) rather than silently running with empty args."""
    bad = MagicMock()
    bad.id = "c1"
    bad.function = MagicMock()
    bad.function.name = "create_workflow"
    bad.function.arguments = '{"workflow": {"nodes": [{"id": "t",'  # truncated JSON
    good = {"nodes": [{"id": "t", "type": "manual_trigger", "config": {}}], "edges": []}
    client = _client([
        _response(tool_calls=[bad]),
        _response(tool_calls=[_tool_call("c2", "create_workflow", {"workflow": good})]),
        _response(content="Done."),
    ])
    events = _run(client, prompt="build it", workflow={"nodes": [], "edges": []})

    assert not any(e["type"] == "error" for e in events)   # no crash
    ops = [e for e in events if e["type"] == "operation"]
    assert len(ops) == 1
    assert len(ops[0]["operation"]["workflow"]["nodes"]) == 1


# ── Graph / connection validation ───────────────────────────────────────

def test_validation_flags_disconnected_graph():
    """A dangling edge and an unconnected node are both reported."""
    ctx = {"connections": [], "catalogue": [], "mcp_sources": []}
    wf = {
        "nodes": [
            {"id": "t", "type": "manual_trigger", "config": {}},
            {"id": "a", "type": "llm_processor", "config": {}},      # orphan
            {"id": "orphan", "type": "set_variable", "config": {}},  # orphan
        ],
        "edges": [{"id": "e1", "source": "t", "target": "MISSING"}],  # dangling
    }
    res = ai_assistant._validate_candidate(wf, ctx, is_fresh=True)
    assert res["is_clean"] is False
    codes = [e.get("code") for e in res["errors"]]
    assert "E_GRAPH" in codes    # edge → unknown target
    assert "E_ORPHAN" in codes   # 'a' / 'orphan' wired to nothing


def test_gate_flags_stringified_json_field():
    """BUG-1: a `json`-type field (Data Transform `params`, Validator `rules`)
    authored as a STRINGIFIED JSON is flagged E_CONFIG_TYPE pre-Apply; a real
    object is accepted."""
    # stringified params on a data_transform → flagged
    errs = ai_assistant._validate_node_config_schemas([
        {"id": "dt", "type": "data_transform",
         "config": {"operation": "sort", "params": '{"column": "score", "ascending": false}'}},
    ])
    dt_codes = [e["code"] for e in errs if e.get("node_id") == "dt"]
    assert "E_CONFIG_TYPE" in dt_codes

    # stringified rules on a validator → flagged
    errs_v = ai_assistant._validate_node_config_schemas([
        {"id": "v", "type": "validator",
         "config": {"rules": '{"name": {"required": true}}'}},
    ])
    assert any(e["code"] == "E_CONFIG_TYPE" and e.get("field") == "rules" for e in errs_v)

    # real object params → NOT flagged as E_CONFIG_TYPE
    errs_ok = ai_assistant._validate_node_config_schemas([
        {"id": "dt2", "type": "data_transform",
         "config": {"operation": "sort", "params": {"column": "score", "ascending": False}}},
    ])
    assert not any(e["code"] == "E_CONFIG_TYPE" for e in errs_ok)


def test_broken_graph_triggers_one_repair_round():
    """A finished-but-disconnected workflow gets ONE forced self-correction
    round; the final proposal is clean and connected."""
    created = {
        "nodes": [
            {"id": "t", "type": "manual_trigger", "config": {}},
            {"id": "n1", "type": "set_variable", "config": {"assignments": [{"name": "x", "value": "1"}]}},
        ],
        "edges": [{"id": "bad", "source": "t", "target": "WRONG"}],  # dangling
    }
    client = _client([
        _response(tool_calls=[_tool_call("c1", "create_workflow", {"workflow": created})]),
        _response(content="Here's your workflow."),                  # graph broken → repair
        _response(tool_calls=[_tool_call("c2", "remove_edge", {"edge_id": "bad"})]),
        _response(tool_calls=[_tool_call("c3", "add_edge", {"source": "t", "target": "n1"})]),
        _response(content="Fixed and connected — Apply when ready."),
    ])
    events = _run(client, prompt="build it", workflow={"nodes": [], "edges": []})

    assert any(e["type"] == "status" and "connection" in e["text"].lower() for e in events)
    ops = [e for e in events if e["type"] == "operation"]
    assert len(ops) == 1
    assert ops[0]["operation"]["validation"]["is_clean"] is True


# ── Debug / test-run tools ──────────────────────────────────────────────

def _run_full(client, *, prompt, workflow, executor_result=None,
              find_one_result=None, workflow_id=None):
    """Driver that also patches the worker queue (run_workflow_test enqueues
    to Citra-Worker) and Mongo (past-run tools + execution-doc readback).
    Returns (events, captured)."""
    context = {"connections": [], "catalogue": [], "mcp_sources": []}
    captured = {}

    async def _collect():
        out = []
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(ai_assistant, "get_llm_client", return_value=client))
            stack.enter_context(patch.object(ai_assistant, "get_default_model", return_value="test-model"))
            stack.enter_context(patch.object(ai_assistant, "get_llm_extra_body", return_value={}))
            if executor_result is not None or find_one_result is not None:
                # One Mongo mock serves both paths: _run_test_on_worker
                # pre-creates + reads back the execution doc, the past-run
                # tools read prior executions.
                exec_doc = (executor_result.model_dump()
                            if executor_result is not None else find_one_result)
                col = MagicMock()
                col.insert_one = AsyncMock()
                col.find_one = AsyncMock(return_value=exec_doc)
                db = MagicMock(); db.__getitem__ = MagicMock(return_value=col)
                cli = MagicMock(); cli.__getitem__ = MagicMock(return_value=db)
                stack.enter_context(patch("citra_mongo.get_async_mongo_client", return_value=cli))
            if executor_result is not None:
                captured["enqueue"] = stack.enter_context(
                    patch("citra_queue.enqueue", return_value="job1"))
                stack.enter_context(
                    patch("citra_queue.get_status", return_value={"status": "done"}))
                stack.enter_context(patch.object(ai_assistant, "_TEST_RUN_POLL_SECONDS", 0))
                stack.enter_context(patch("citra_auth.mint_workflow_org_token", return_value="tok"))
            async for ev in ai_assistant.run_workflow_assistant(
                prompt=prompt, workflow=workflow, conversation_block="",
                focused_node_id=None, context=context, user_id="u1",
                workflow_id=workflow_id, org_id="org1",
            ):
                out.append(ev)
        return out

    events = asyncio.run(_collect())
    return events, captured


def _fake_execution(status="failed"):
    """A WorkflowExecution-like object exposing model_dump()."""
    obj = MagicMock()
    obj.model_dump.return_value = {
        "execution_id": "ex1",
        "status": status,
        "error": "Node n1 (Writer) failed: connection refused",
        "environment": "test",
        "node_results": {
            "trigger": {"status": "completed", "error": None, "output_data": {"ok": True}},
            "n1": {"status": "failed", "error": "connection refused", "output_data": None},
        },
    }
    return obj


def test_run_test_emits_run_result_and_uses_test_env():
    """A debug request → run_workflow_test (real test-env run) yields a
    run_result event, then a node_edit fix proposal."""
    wf = {"nodes": [{"id": "trigger", "type": "manual_trigger", "config": {}},
                    {"id": "n1", "type": "sql_writer", "config": {"table": "results"}}],
          "edges": [{"id": "e", "source": "trigger", "target": "n1"}]}
    client = _client([
        _response(tool_calls=[_tool_call("c1", "run_workflow_test", {})]),
        _response(tool_calls=[_tool_call("c2", "update_node",
                  {"node_id": "n1", "changes": {"config": {"connection_id": "c1"}}})]),
        _response(content="Node n1 failed because it had no connection. I set one — Apply to fix."),
    ])
    events, captured = _run_full(client, prompt="test this and fix bugs",
                                 workflow=wf, executor_result=_fake_execution())

    run_evts = [e for e in events if e["type"] == "run_result"]
    assert len(run_evts) == 1
    nr = {n["node_id"]: n["status"] for n in run_evts[0]["run"]["node_results"]}
    assert nr["n1"] == "failed"
    # The run was ENQUEUED to Citra-Worker (same substrate as manual/cron
    # runs — the API container has no Docker sandbox), with the working copy
    # inline and in the TEST environment, never prod.
    assert captured["enqueue"].call_args.args[0] == "workflow.run"
    payload = captured["enqueue"].call_args.args[1]
    assert payload["environment"] == "test"
    assert payload["workflow_definition"]["nodes"]
    # And a fix was proposed as a diff.
    assert any(e["type"] == "operation" and e["operation"]["type"] == "workflow" for e in events)


def test_run_test_stale_doc_reports_unpersisted_outcome():
    """Job finishes 'done' but the executor never updated the pre-created
    execution doc (Mongo save failure is swallowed in _save_execution) →
    the model gets the job's true status, NOT a stale 'queued' summary."""
    async def _invoke():
        wf = MagicMock()
        wf.workflow_id = "wf1"
        wf.user_id = "u1"
        wf.model_dump.return_value = {"workflow_id": "wf1"}
        col = MagicMock()
        col.insert_one = AsyncMock()
        col.find_one = AsyncMock(return_value={
            "execution_id": "e1", "status": "queued", "node_results": {},
        })
        db = MagicMock(); db.__getitem__ = MagicMock(return_value=col)
        cli = MagicMock(); cli.__getitem__ = MagicMock(return_value=db)
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch("citra_mongo.get_async_mongo_client", return_value=cli))
            stack.enter_context(patch("citra_queue.enqueue", return_value="j1"))
            stack.enter_context(patch("citra_queue.get_status", return_value={
                "status": "done", "result": {"status": "completed"},
            }))
            stack.enter_context(patch.object(ai_assistant, "_TEST_RUN_POLL_SECONDS", 0))
            return await ai_assistant._run_test_on_worker(wf, {}, "tok")

    out = asyncio.run(_invoke())
    assert isinstance(out, str)
    assert "completed" in out
    assert "not persisted" in out


def test_get_execution_results_reads_past_run():
    """'why did my last run fail?' → get_execution_results reads a past run
    without re-executing, and the turn completes."""
    wf = {"nodes": [{"id": "n1", "type": "sql_writer", "config": {}}], "edges": []}
    past = {
        "execution_id": "exPAST", "workflow_id": "wf-123", "status": "failed",
        "error": "boom", "environment": "test",
        "node_results": {"n1": {"status": "failed", "error": "boom", "output_data": None}},
    }
    client = _client([
        _response(tool_calls=[_tool_call("c1", "get_execution_results", {"execution_id": "exPAST"})]),
        _response(content="Your last run failed at n1: boom."),
    ])
    events, _ = _run_full(client, prompt="why did my last run fail?",
                          workflow=wf, find_one_result=past, workflow_id="wf-123")

    assert not any(e["type"] == "error" for e in events)
    done = [e for e in events if e["type"] == "done"]
    assert len(done) == 1
    assert "n1" in done[0]["message"]
