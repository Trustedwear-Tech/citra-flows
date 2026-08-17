# Copyright (c) 2026 Trustedwear Tech Private Limited (https://citra-ai.com)
# Author: Rohit Kumar Chandan
# SPDX-License-Identifier: BUSL-1.1
#
# Licensed under the Business Source License 1.1. Non-production use is granted;
# production use requires a commercial licence until the Change Date, after
# which this file converts to Apache-2.0. See LICENSE at the repository root.

"""
Unit tests for processor nodes — LLM, DataTransform, CodeBlock, processing modes.
All LLM calls mocked via _get_llm_response.
"""

import sys
import os
import pytest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from citra_workflow.nodes import NodeContext, get_node
from citra_workflow.models import NodeType


# ============================================================================
# Processing Mode Tests (shared by LLM-backed processors)
# ============================================================================

class TestProcessingModes:

    @pytest.mark.asyncio
    async def test_mode_all(self):
        """All-at-once mode: entire input sent to LLM in one call."""
        node = get_node(NodeType.LLM_PROCESSOR)
        ctx = NodeContext(
            node_id="pm1",
            node_config={
                "user_prompt": "Summarize: {{data}}",
                "processing_mode": "all",
            },
            input_data={"items": [{"x": 1}, {"x": 2}]},
        )
        with patch(
            "citra_workflow.nodes.processors._get_llm_response",
            return_value="Summary of all items",
        ):
            result = await node.execute(ctx)

        assert result["items"][0]["result"] == "Summary of all items"

    @pytest.mark.asyncio
    async def test_mode_each(self):
        """Each-item mode: items processed one-by-one, results aggregated."""
        call_count = {"n": 0}

        def mock_llm(prompt, model, system=""):
            call_count["n"] += 1
            return f"Result {call_count['n']}"

        node = get_node(NodeType.LLM_PROCESSOR)
        ctx = NodeContext(
            node_id="pm2",
            node_config={
                "user_prompt": "Process: {{item}}",
                "processing_mode": "each",
            },
            input_data={"items": [{"a": 1}, {"a": 2}, {"a": 3}]},
        )
        with patch(
            "citra_workflow.nodes.processors._get_llm_response",
            side_effect=mock_llm,
        ):
            result = await node.execute(ctx)

        assert result["meta"]["processing_mode"] == "each"
        assert result["meta"]["total"] == 3
        assert len(result["items"]) == 3

    @pytest.mark.asyncio
    async def test_mode_batch(self):
        """Batch mode: 5 items with batch_size=2 → 3 batches."""
        call_count = {"n": 0}

        def mock_llm(prompt, model, system=""):
            call_count["n"] += 1
            return f"Batch {call_count['n']}"

        node = get_node(NodeType.LLM_PROCESSOR)
        ctx = NodeContext(
            node_id="pm3",
            node_config={
                "user_prompt": "Process batch: {{data}}",
                "processing_mode": "batch",
                "batch_size": 2,
            },
            input_data={"items": [1, 2, 3, 4, 5]},
        )
        with patch(
            "citra_workflow.nodes.processors._get_llm_response",
            side_effect=mock_llm,
        ):
            result = await node.execute(ctx)

        assert result["meta"]["processing_mode"] == "batch"
        assert result["meta"]["total_batches"] == 3
        assert result["meta"]["batch_size"] == 2

    @pytest.mark.asyncio
    async def test_batch_size_larger_than_input(self):
        """batch_size=100 with 3 items → 1 batch."""
        node = get_node(NodeType.LLM_PROCESSOR)
        ctx = NodeContext(
            node_id="pm4",
            node_config={
                "user_prompt": "Process: {{data}}",
                "processing_mode": "batch",
                "batch_size": 100,
            },
            input_data={"items": [1, 2, 3]},
        )
        with patch(
            "citra_workflow.nodes.processors._get_llm_response",
            return_value="All done",
        ):
            result = await node.execute(ctx)

        assert result["meta"]["processing_mode"] == "batch"
        assert result["meta"]["total_batches"] == 1


# ============================================================================
# LLM Processor Tests
# ============================================================================

class TestLLMProcessor:

    @pytest.mark.asyncio
    async def test_text_output(self):
        node = get_node(NodeType.LLM_PROCESSOR)
        ctx = NodeContext(
            node_id="llm1",
            node_config={
                "user_prompt": "Hello {{data}}",
                "processing_mode": "all",
            },
            input_data={"items": [{"name": "test"}]},
        )
        with patch(
            "citra_workflow.nodes.processors._get_llm_response",
            return_value="Hello from LLM",
        ):
            result = await node.execute(ctx)

        assert result["items"][0]["result"] == "Hello from LLM"

    @pytest.mark.asyncio
    async def test_json_output(self):
        node = get_node(NodeType.LLM_PROCESSOR)
        ctx = NodeContext(
            node_id="llm2",
            node_config={
                "user_prompt": "Return JSON: {{data}}",
                "processing_mode": "all",
            },
            input_data={"items": []},
        )
        with patch(
            "citra_workflow.nodes.processors._get_llm_response",
            return_value='{"key": "value"}',
        ):
            result = await node.execute(ctx)

        assert result["items"][0] == {"key": "value"}

    @pytest.mark.asyncio
    async def test_json_output_with_markdown_fence(self):
        """LLM returns JSON wrapped in ```json``` fences → should be parsed."""
        node = get_node(NodeType.LLM_PROCESSOR)
        ctx = NodeContext(
            node_id="llm3",
            node_config={
                "user_prompt": "Return JSON",
                "processing_mode": "all",
            },
            input_data={"items": []},
        )
        with patch(
            "citra_workflow.nodes.processors._get_llm_response",
            return_value='```json\n{"result": 42}\n```',
        ):
            result = await node.execute(ctx)

        assert result["items"][0] == {"result": 42}

    @pytest.mark.asyncio
    async def test_template_variable_substitution(self):
        """{{data}} and variable placeholders should be replaced."""
        captured_prompt = {}

        def mock_llm(prompt, model, system=""):
            captured_prompt["prompt"] = prompt
            return "ok"

        node = get_node(NodeType.LLM_PROCESSOR)
        ctx = NodeContext(
            node_id="llm4",
            node_config={
                "user_prompt": "Company: {{company_name}}, Data: {{data}}",
                "processing_mode": "all",
            },
            input_data={"items": [{"x": 1}]},
            variables={"company_name": "Acme Corp"},
        )
        with patch(
            "citra_workflow.nodes.processors._get_llm_response",
            side_effect=mock_llm,
        ):
            await node.execute(ctx)

        assert "Acme Corp" in captured_prompt["prompt"]


# ============================================================================
# DataTransform Tests
# ============================================================================

class TestDataTransform:

    @pytest.mark.asyncio
    async def test_filter(self):
        node = get_node(NodeType.DATA_TRANSFORM)
        ctx = NodeContext(
            node_id="dt1",
            node_config={
                "operation": "filter",
                "params": {"column": "status", "operator": "==", "value": "active"},
            },
            input_data={"items": [
                {"name": "A", "status": "active"},
                {"name": "B", "status": "inactive"},
                {"name": "C", "status": "active"},
            ]},
        )
        result = await node.execute(ctx)
        assert result["meta"]["count"] == 2
        assert all(r["status"] == "active" for r in result["items"])

    @pytest.mark.asyncio
    async def test_select_columns(self):
        node = get_node(NodeType.DATA_TRANSFORM)
        ctx = NodeContext(
            node_id="dt2",
            node_config={
                "operation": "select",
                "params": {"columns": ["name"]},
            },
            input_data={"items": [
                {"name": "A", "age": 30, "email": "a@b.com"},
            ]},
        )
        result = await node.execute(ctx)
        assert list(result["items"][0].keys()) == ["name"]

    @pytest.mark.asyncio
    async def test_rename_columns(self):
        node = get_node(NodeType.DATA_TRANSFORM)
        ctx = NodeContext(
            node_id="dt3",
            node_config={
                "operation": "rename",
                "params": {"old_name": "new_name"},
            },
            input_data={"items": [{"old_name": "value1"}]},
        )
        result = await node.execute(ctx)
        assert "new_name" in result["items"][0]
        assert "old_name" not in result["items"][0]

    @pytest.mark.asyncio
    async def test_params_stringified_json_is_parsed(self):
        """BUG-1: a `json`-type `params` emitted as a STRINGIFIED JSON must be
        parsed (not crash with 'string indices must be integers')."""
        node = get_node(NodeType.DATA_TRANSFORM)
        ctx = NodeContext(
            node_id="dt_str",
            node_config={
                "operation": "sort",
                "params": '{"column": "score", "ascending": false}',  # a STRING
            },
            input_data={"items": [
                {"name": "A", "score": 50},
                {"name": "B", "score": 90},
            ]},
        )
        result = await node.execute(ctx)
        assert [r["score"] for r in result["items"]] == [90, 50]

    @pytest.mark.asyncio
    async def test_params_invalid_string_raises_clear_error(self):
        """BUG-1: an unparseable string `params` fails early with a clear
        config-type ValueError, not the cryptic TypeError."""
        node = get_node(NodeType.DATA_TRANSFORM)
        ctx = NodeContext(
            node_id="dt_bad",
            node_config={"operation": "sort", "params": "not json"},
            input_data={"items": [{"score": 1}]},
        )
        with pytest.raises(ValueError, match="must be a JSON object"):
            await node.execute(ctx)

    @pytest.mark.asyncio
    async def test_rename_direct_map(self):
        """BUG-3: the canonical direct map {existing: new} renames correctly."""
        node = get_node(NodeType.DATA_TRANSFORM)
        ctx = NodeContext(
            node_id="dt_rn1",
            node_config={
                "operation": "rename",
                "params": {"current_role": "role", "_classification_label": "classification"},
            },
            input_data={"items": [{"current_role": "FE", "_classification_label": "Strong"}]},
        )
        result = await node.execute(ctx)
        keys = result["items"][0].keys()
        assert "role" in keys and "classification" in keys
        assert "current_role" not in keys and "_classification_label" not in keys

    @pytest.mark.asyncio
    async def test_rename_old_new_compat(self):
        """BUG-3: the AI's {old_name, new_name} shape (single object AND a list)
        is accepted and renames instead of being a silent no-op."""
        node = get_node(NodeType.DATA_TRANSFORM)
        # single {old_name, new_name} object
        ctx1 = NodeContext(
            node_id="dt_rn2",
            node_config={
                "operation": "rename",
                "params": {"old_name": "current_role", "new_name": "role"},
            },
            input_data={"items": [{"current_role": "FE", "score": 80}]},
        )
        r1 = await node.execute(ctx1)
        assert "role" in r1["items"][0] and "current_role" not in r1["items"][0]
        # a LIST of {old_name, new_name} objects
        ctx2 = NodeContext(
            node_id="dt_rn3",
            node_config={
                "operation": "rename",
                "params": [
                    {"old_name": "current_role", "new_name": "role"},
                    {"old_name": "_classification_label", "new_name": "classification"},
                ],
            },
            input_data={"items": [{"current_role": "FE", "_classification_label": "Strong"}]},
        )
        r2 = await node.execute(ctx2)
        keys = r2["items"][0].keys()
        assert "role" in keys and "classification" in keys
        assert "current_role" not in keys and "_classification_label" not in keys

    @pytest.mark.asyncio
    async def test_sort(self):
        node = get_node(NodeType.DATA_TRANSFORM)
        ctx = NodeContext(
            node_id="dt4",
            node_config={
                "operation": "sort",
                "params": {"column": "score", "ascending": False},
            },
            input_data={"items": [
                {"name": "A", "score": 50},
                {"name": "B", "score": 90},
                {"name": "C", "score": 70},
            ]},
        )
        result = await node.execute(ctx)
        scores = [r["score"] for r in result["items"]]
        assert scores == [90, 70, 50]

    @pytest.mark.asyncio
    async def test_aggregate(self):
        node = get_node(NodeType.DATA_TRANSFORM)
        ctx = NodeContext(
            node_id="dt5",
            node_config={
                "operation": "aggregate",
                "params": {"group_by": "dept", "agg": {"salary": "mean"}},
            },
            input_data={"items": [
                {"dept": "eng", "salary": 100},
                {"dept": "eng", "salary": 200},
                {"dept": "sales", "salary": 150},
            ]},
        )
        result = await node.execute(ctx)
        assert result["meta"]["count"] == 2
        eng = next(r for r in result["items"] if r["dept"] == "eng")
        assert eng["salary"] == 150.0

    @pytest.mark.asyncio
    async def test_add_column_safe_expression(self):
        node = get_node(NodeType.DATA_TRANSFORM)
        ctx = NodeContext(
            node_id="dt6",
            node_config={
                "operation": "add_column",
                "params": {"name": "total", "expression": "price * quantity"},
            },
            input_data={"items": [
                {"price": 10, "quantity": 5},
                {"price": 20, "quantity": 3},
            ]},
        )
        result = await node.execute(ctx)
        assert result["items"][0]["total"] == 50
        assert result["items"][1]["total"] == 60

    @pytest.mark.asyncio
    async def test_add_column_blocks_imports(self):
        """__import__('os') in expression → ValueError."""
        node = get_node(NodeType.DATA_TRANSFORM)
        ctx = NodeContext(
            node_id="dt7",
            node_config={
                "operation": "add_column",
                "params": {"name": "hack", "expression": "__import__('os').system('rm -rf /')"},
            },
            input_data={"items": [{"a": 1}]},
        )
        with pytest.raises(ValueError):
            await node.execute(ctx)

    @pytest.mark.asyncio
    async def test_empty_records(self):
        node = get_node(NodeType.DATA_TRANSFORM)
        ctx = NodeContext(
            node_id="dt8",
            node_config={"operation": "filter", "params": {"column": "x", "value": "y"}},
            input_data={"items": []},
        )
        result = await node.execute(ctx)
        assert result["items"] == []
        assert result["meta"]["count"] == 0

    @pytest.mark.asyncio
    async def test_missing_required_param_names_what_is_missing(self):
        """A missing param must say which one, not surface a bare KeyError.

        The shipped "Data Processing Pipeline" template has params={} and used
        to fail with an error whose entire text was `'column'`.
        """
        node = get_node(NodeType.DATA_TRANSFORM)
        ctx = NodeContext(
            node_id="dt9",
            node_config={"operation": "filter", "params": {}},
            input_data={"items": [{"status": "active"}]},
        )
        with pytest.raises(ValueError) as exc:
            await node.execute(ctx)
        msg = str(exc.value)
        assert "filter" in msg
        assert "column" in msg and "value" in msg
        # and it should show the caller what a good config looks like
        assert "operator" in msg

    @pytest.mark.asyncio
    async def test_partially_missing_param_reported(self):
        node = get_node(NodeType.DATA_TRANSFORM)
        ctx = NodeContext(
            node_id="dt10",
            node_config={"operation": "aggregate", "params": {"group_by": "region"}},
            input_data={"items": [{"region": "north", "amount": 1}]},
        )
        with pytest.raises(ValueError) as exc:
            await node.execute(ctx)
        msg = str(exc.value)
        assert "agg" in msg
        # the param that WAS supplied must not be reported as missing
        assert "group_by" not in msg.split("missing required param(s):")[1].split(".")[0]

    @pytest.mark.asyncio
    async def test_unknown_operation_fails_instead_of_passing_data_through(self):
        """An unrecognised operation matched no branch and returned rows
        untouched — a typo silently became a no-op on a data step."""
        node = get_node(NodeType.DATA_TRANSFORM)
        ctx = NodeContext(
            node_id="dt11",
            node_config={"operation": "fliter", "params": {"column": "a", "value": 1}},
            input_data={"items": [{"a": 1}, {"a": 2}]},
        )
        with pytest.raises(ValueError) as exc:
            await node.execute(ctx)
        assert "fliter" in str(exc.value)
        assert "filter" in str(exc.value)   # lists the supported operations


# ============================================================================
# CodeBlock Tests
# ============================================================================

class TestCodeBlock:
    """code_block runs author code in a Docker sandbox (C1).

    These tests stub the sandbox with an in-process runner of the *wrapped*
    script so the node's wrapper construction + result parsing are verified
    deterministically without a Docker daemon. A real-Docker integration
    test additionally needs the sandbox image present.
    """

    @pytest.fixture(autouse=True)
    def _fake_sandbox(self, monkeypatch, tmp_path):
        import io
        import contextlib
        import json as _json
        from citra_workflow.utils import code_sandbox as _cs

        async def _fake(*, script, input_payload, image, timeout):
            # Mirror the real sandbox: stage input_payload as the data file
            # the wrapper reads, then run the wrapped script and capture
            # its stdout. In-process exec of trusted test scaffolding only.
            data_file = tmp_path / "data.json"
            data_file.write_text(_json.dumps(input_payload, default=str))
            runnable = script.replace(
                "/workspace/input/data.json",
                str(data_file).replace("\\", "/"),
            )
            buf = io.StringIO()
            try:
                with contextlib.redirect_stdout(buf):
                    exec(compile(runnable, "<sandbox-fake>", "exec"), {})
                return {"success": True, "exit_code": 0,
                        "stdout": buf.getvalue(), "stderr": ""}
            except Exception as e:  # noqa: BLE001
                return {"success": False, "exit_code": 1,
                        "stdout": buf.getvalue(), "stderr": str(e)}

        monkeypatch.setattr(_cs, "run_in_sandbox", _fake)

    @pytest.mark.asyncio
    async def test_safe_execution(self):
        node = get_node(NodeType.CODE_BLOCK)
        ctx = NodeContext(
            node_id="cb1",
            node_config={"code": "result = sum([1, 2, 3, 4])"},
            input_data={},
        )
        result = await node.execute(ctx)
        assert result["items"][0]["result"] == 10

    @pytest.mark.asyncio
    async def test_accesses_input_data(self):
        node = get_node(NodeType.CODE_BLOCK)
        ctx = NodeContext(
            node_id="cb2",
            node_config={"code": "result = [r['score'] for r in data.get('items', [])]"},
            input_data={"items": [{"score": 80}, {"score": 95}]},
        )
        result = await node.execute(ctx)
        assert result["items"] == [80, 95]

    @pytest.mark.asyncio
    async def test_uses_variables(self):
        node = get_node(NodeType.CODE_BLOCK)
        ctx = NodeContext(
            node_id="cb8",
            node_config={"code": "result = variables.get('multiplier', 1) * 10"},
            input_data={},
            variables={"multiplier": 5},
        )
        result = await node.execute(ctx)
        assert result["items"][0]["result"] == 50

    @pytest.mark.asyncio
    async def test_imports_allowed(self):
        # The old in-process node blocked imports; the sandbox allows them.
        node = get_node(NodeType.CODE_BLOCK)
        ctx = NodeContext(
            node_id="cb_imp",
            node_config={"code": "import statistics\nresult = statistics.mean([2, 4, 6])"},
            input_data={},
        )
        result = await node.execute(ctx)
        assert result["items"][0]["result"] == 4

    @pytest.mark.asyncio
    async def test_dict_result_wrapped(self):
        node = get_node(NodeType.CODE_BLOCK)
        ctx = NodeContext(
            node_id="cb_dict",
            node_config={"code": "result = {'a': 1}"},
            input_data={},
        )
        result = await node.execute(ctx)
        assert result["items"] == [{"a": 1}]

    @pytest.mark.asyncio
    async def test_syntax_error(self):
        # Caught by the pre-flight ast.parse, before any sandbox call.
        node = get_node(NodeType.CODE_BLOCK)
        ctx = NodeContext(
            node_id="cb7",
            node_config={"code": "def broken("},
            input_data={},
        )
        with pytest.raises(ValueError, match="Syntax error"):
            await node.execute(ctx)

    @pytest.mark.asyncio
    async def test_execution_failure_raises(self):
        node = get_node(NodeType.CODE_BLOCK)
        ctx = NodeContext(
            node_id="cb_fail",
            node_config={"code": "result = 1 / 0"},
            input_data={},
        )
        with pytest.raises(ValueError, match="Code execution failed"):
            await node.execute(ctx)
