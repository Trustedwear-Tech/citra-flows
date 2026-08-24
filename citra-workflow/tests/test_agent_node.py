# Copyright (c) 2026 Trustedwear Tech Private Limited (https://citra-ai.com)
# Author: Rohit Kumar Chandan
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may not
# use this file except in compliance with the License. You may obtain a copy of
# the License at http://www.apache.org/licenses/LICENSE-2.0

"""
Unit tests for AI Agent node and agent tool functions.
All external calls (LLM, HTTP, DB, vector) are mocked.
"""

import sys
import os
import json
import pytest
from unittest.mock import patch, AsyncMock, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from citra_workflow.nodes import NodeContext, get_node
from citra_workflow.nodes.agents import (
    _tool_code_execute,
    _tool_http_request,
    _validate_readonly_sql,
    _build_tool_definitions,
    _tool_bucket_read,
    _tool_sftp_read,
    _BUCKET_ALLOWED_ACTIONS,
    _SFTP_ALLOWED_ACTIONS,
    AIAgentNode,
)
from citra_workflow.models import NodeType


# ============================================================================
# _tool_code_execute Security Tests
# ============================================================================
# These assert the chat-sandbox's policy (blocks import/eval/dunder/open,
# returns "Security ..." messages for blocked patterns, returns
# "Syntax error" / "Execution error" wrappers). That policy + the
# Docker-based sandbox itself lives in `Citra-Service/services/code_executor.py`.
#
# After the Phase J split citra-workflow no longer ships the sandbox; the
# agent's _tool_code_execute now POSTs to Citra-Service over HTTP (or, in
# unit-test mode, hits the conftest stub that returns the empty success
# blob). These tests therefore belong as INTEGRATION tests against a
# running Citra-Service. Mark them so the unit-test run skips them.

@pytest.mark.integration
class TestToolCodeExecute:

    @pytest.mark.asyncio
    async def test_safe_code(self):
        result = await _tool_code_execute("print(1 + 2)")
        assert "3" in result

    @pytest.mark.asyncio
    async def test_blocks_imports(self):
        result = await _tool_code_execute("import os")
        assert "Security" in result and "imports" in result.lower()

    @pytest.mark.asyncio
    async def test_blocks_open(self):
        result = await _tool_code_execute("open('/etc/passwd')")
        assert "Security" in result and "open" in result

    @pytest.mark.asyncio
    async def test_blocks_eval(self):
        result = await _tool_code_execute("eval('1+1')")
        assert "Security" in result and "eval" in result

    @pytest.mark.asyncio
    async def test_blocks_dunder(self):
        result = await _tool_code_execute("x = ''.__class__")
        assert "Security" in result and "__class__" in result

    @pytest.mark.asyncio
    async def test_syntax_error(self):
        result = await _tool_code_execute("def bad(")
        assert "Syntax error" in result

    @pytest.mark.asyncio
    async def test_runtime_error_handled(self):
        result = await _tool_code_execute("x = 1 / 0")
        assert "Execution error" in result


# ============================================================================
# _tool_http_request SSRF Tests
# ============================================================================

class TestToolHttpRequestSSRF:

    @pytest.mark.asyncio
    async def test_blocks_localhost(self):
        result = await _tool_http_request("http://localhost/admin")
        assert "not allowed" in result.lower()

    @pytest.mark.asyncio
    async def test_blocks_metadata_google(self):
        result = await _tool_http_request("http://metadata.google.internal/computeMetadata/v1/")
        assert "not allowed" in result.lower()

    @pytest.mark.asyncio
    async def test_blocks_private_ip(self):
        """Resolving to 127.0.0.1 should be rejected."""
        with patch("socket.getaddrinfo", return_value=[
            (2, 1, 6, "", ("127.0.0.1", 0)),
        ]):
            result = await _tool_http_request("http://evil.example.com/steal")
        assert "not allowed" in result.lower()


# ============================================================================
# _build_tool_definitions Tests
# ============================================================================

class TestBuildToolDefinitions:

    def test_returns_selected_tools_only(self):
        defs = _build_tool_definitions(["web_search", "code_execute"])
        names = [d["function"]["name"] for d in defs]
        assert "web_search" in names
        assert "code_execute" in names
        assert "sql_query" not in names
        assert "nosql_query" not in names

    def test_empty_list(self):
        defs = _build_tool_definitions([])
        assert defs == []

    def test_sql_and_nosql_tools(self):
        defs = _build_tool_definitions(["sql_query", "nosql_query"])
        names = [d["function"]["name"] for d in defs]
        assert "sql_query" in names
        assert "nosql_query" in names
        assert len(defs) == 2

    def test_s3_and_sftp_tools(self):
        defs = _build_tool_definitions(["bucket_read", "sftp_read"])
        names = [d["function"]["name"] for d in defs]
        assert "bucket_read" in names
        assert "sftp_read" in names
        assert len(defs) == 2
        # Verify action enum is restricted to read-only
        bucket_def = next(d for d in defs if d["function"]["name"] == "bucket_read")
        assert bucket_def["function"]["parameters"]["properties"]["action"]["enum"] == ["list", "download"]
        sftp_def = next(d for d in defs if d["function"]["name"] == "sftp_read")
        assert sftp_def["function"]["parameters"]["properties"]["action"]["enum"] == ["list", "download"]


# ============================================================================
# AIAgentNode.execute Tests (mock _run_agent_with_tools entirely)
# ============================================================================

class TestAIAgentNode:

    @pytest.mark.asyncio
    async def test_basic_execution(self):
        node = get_node(NodeType.AI_AGENT)
        ctx = NodeContext(
            node_id="agent1",
            node_config={
                "system_prompt": "You are helpful.",
                "user_prompt": "Analyze: {{data}}",
                "model": None,
                "tools": [],
                "max_iterations": 5,
            },
            input_data={"items": [1, 2, 3]},
            user_id="u123",
        )
        with patch(
            "citra_workflow.nodes.agents._run_agent_with_tools",
            return_value={"result": "Analysis complete", "tool_calls": [], "structured": False},
        ) as mock_run:
            result = await node.execute(ctx)

        assert result["items"][0]["result"] == "Analysis complete"
        mock_run.assert_awaited_once()
        call_kwargs = mock_run.call_args[1]
        assert call_kwargs["max_iterations"] == 5
        assert call_kwargs["user_id"] == "u123"

    @pytest.mark.asyncio
    async def test_max_iterations_capped_at_20(self):
        node = get_node(NodeType.AI_AGENT)
        ctx = NodeContext(
            node_id="agent2",
            node_config={
                "system_prompt": "test",
                "user_prompt": "test",
                "max_iterations": 999,
                "tools": [],
            },
            input_data={},
            user_id="u1",
        )
        with pytest.raises(ValueError, match="max_iterations.*exceeds limit"):
            await node.execute(ctx)

    @pytest.mark.asyncio
    async def test_variable_substitution(self):
        node = get_node(NodeType.AI_AGENT)
        ctx = NodeContext(
            node_id="agent3",
            node_config={
                "system_prompt": "test",
                "user_prompt": "Company: {{company}}, Data: {{data}}",
                "tools": [],
            },
            input_data={"x": 1},
            variables={"company": "Acme"},
            user_id="u2",
        )
        with patch(
            "citra_workflow.nodes.agents._run_agent_with_tools",
            return_value={"result": "done", "tool_calls": [], "structured": False},
        ) as mock_run:
            await node.execute(ctx)

        user_msg = mock_run.call_args[1]["user_message"]
        assert "Acme" in user_msg

    @pytest.mark.asyncio
    async def test_output_schema_passed(self):
        node = get_node(NodeType.AI_AGENT)
        schema = {"type": "object", "properties": {"score": {"type": "number"}}}
        ctx = NodeContext(
            node_id="agent4",
            node_config={
                "system_prompt": "rate",
                "user_prompt": "{{data}}",
                "output_schema": json.dumps(schema),
                "tools": [],
            },
            input_data={},
            user_id="u3",
        )
        with patch(
            "citra_workflow.nodes.agents._run_agent_with_tools",
            return_value={"result": {"score": 8.5}, "structured": True, "tool_calls": []},
        ) as mock_run:
            result = await node.execute(ctx)

        assert result["meta"]["structured"] is True
        assert mock_run.call_args[1]["output_schema"] == schema

    @pytest.mark.asyncio
    async def test_connection_ids_passed_through(self):
        node = get_node(NodeType.AI_AGENT)
        ctx = NodeContext(
            node_id="agent5",
            node_config={
                "system_prompt": "test",
                "user_prompt": "{{data}}",
                "tools": ["sql_query", "bucket_read", "sftp_read"],
                "sql_connection_id": "conn-sql-1",
                "nosql_connection_id": "conn-nosql-1",
                "bucket_connection_id": "conn-s3-1",
                "sftp_connection_id": "conn-sftp-1",
            },
            input_data={},
            user_id="u5",
            environment="prod",
        )
        with patch(
            "citra_workflow.nodes.agents._run_agent_with_tools",
            return_value={"result": "ok", "tool_calls": [], "structured": False},
        ) as mock_run:
            await node.execute(ctx)

        kw = mock_run.call_args[1]
        assert kw["sql_connection_id"] == "conn-sql-1"
        assert kw["nosql_connection_id"] == "conn-nosql-1"
        assert kw["bucket_connection_id"] == "conn-s3-1"
        assert kw["sftp_connection_id"] == "conn-sftp-1"
        assert kw["environment"] == "prod"


# ============================================================================
# SQL Read-Only Validation Tests
# ============================================================================

class TestValidateReadonlySQL:

    def test_select_allowed(self):
        _validate_readonly_sql("SELECT * FROM orders WHERE status = 'active'")

    def test_with_cte_allowed(self):
        _validate_readonly_sql("WITH cte AS (SELECT id FROM users) SELECT * FROM cte")

    def test_insert_blocked(self):
        with pytest.raises(ValueError, match="Only SELECT"):
            _validate_readonly_sql("INSERT INTO users (name) VALUES ('x')")

    def test_update_blocked(self):
        with pytest.raises(ValueError, match="Only SELECT"):
            _validate_readonly_sql("UPDATE users SET name = 'x'")

    def test_delete_blocked(self):
        with pytest.raises(ValueError, match="Only SELECT"):
            _validate_readonly_sql("DELETE FROM users WHERE id = 1")

    def test_drop_blocked(self):
        with pytest.raises(ValueError, match="Only SELECT"):
            _validate_readonly_sql("DROP TABLE users")

    def test_truncate_blocked(self):
        with pytest.raises(ValueError, match="Only SELECT"):
            _validate_readonly_sql("TRUNCATE TABLE users")

    def test_comment_hiding_blocked(self):
        """Destructive keyword hidden after a SELECT should be caught by semicolon check."""
        with pytest.raises(ValueError, match="Semicolons"):
            _validate_readonly_sql("SELECT 1; /* harmless */ DROP TABLE users")

    def test_inline_comment_hiding_blocked(self):
        with pytest.raises(ValueError, match="DELETE"):
            _validate_readonly_sql("SELECT 1 -- safe\nDELETE FROM users")

    def test_select_with_embedded_insert(self):
        """A SELECT that sneaks INSERT after a semicolon should be blocked."""
        with pytest.raises(ValueError, match="Semicolons"):
            _validate_readonly_sql("SELECT 1; INSERT INTO t VALUES (1)")

    def test_non_select_first_word(self):
        with pytest.raises(ValueError, match="Only SELECT"):
            _validate_readonly_sql("CALL some_procedure()")

    def test_grant_blocked(self):
        with pytest.raises(ValueError, match="Only SELECT"):
            _validate_readonly_sql("GRANT ALL ON users TO public")

    def test_merge_blocked(self):
        with pytest.raises(ValueError, match="Only SELECT"):
            _validate_readonly_sql("MERGE INTO t1 USING t2 ON t1.id=t2.id WHEN MATCHED THEN UPDATE SET x=1")


# ============================================================================
# S3 Read-Only Guard Tests
# ============================================================================

class TestS3ReadGuard:

    @pytest.mark.asyncio
    async def test_list_action_allowed(self):
        """list action should pass the guard and reach the S3 client.

        Connections are pre-resolved by the agent runner and passed in via
        ``resolved_connection`` — the tool never resolves them itself.
        """
        with patch("boto3.client") as mock_boto:
            mock_s3 = MagicMock()
            mock_boto.return_value = mock_s3
            mock_s3.list_objects_v2.return_value = {"Contents": []}
            result = await _tool_bucket_read(
                "list", "conn1", "u1", "prod",
                resolved_connection={"bucket": "my-bucket", "region": "us-east-1"},
            )
        assert result == "[]"

    @pytest.mark.asyncio
    async def test_download_action_allowed(self):
        """download action should pass the guard."""
        with patch("boto3.client") as mock_boto:
            mock_s3 = MagicMock()
            mock_boto.return_value = mock_s3
            mock_s3.head_object.return_value = {"ContentLength": 10}
            mock_s3.get_object.return_value = {"Body": MagicMock(read=lambda: b"hello")}
            result = await _tool_bucket_read(
                "download", "conn1", "u1", "prod", key="data.txt",
                resolved_connection={"bucket": "my-bucket"},
            )
        parsed = json.loads(result)
        assert parsed["content"] == "hello"

    @pytest.mark.asyncio
    async def test_delete_action_rejected(self):
        result = await _tool_bucket_read("delete", "conn1", "u1", "prod")
        assert "Only 'list' and 'download'" in result

    @pytest.mark.asyncio
    async def test_put_action_rejected(self):
        result = await _tool_bucket_read("put", "conn1", "u1", "prod")
        assert "Only 'list' and 'download'" in result

    @pytest.mark.asyncio
    async def test_missing_connection_bucket(self):
        """If resolved connection has no bucket, return an error."""
        # Connections are pre-resolved by the agent runner and passed in.
        result = await _tool_bucket_read(
            "list", "conn1", "u1", "prod",
            resolved_connection={"region": "us-east-1"},
        )
        assert "no bucket" in result.lower()

    @pytest.mark.asyncio
    async def test_key_traversal_blocked(self):
        """Path traversal in the S3 key should be rejected."""
        # boto3.client must be stubbed so client creation doesn't fail before
        # the key is sanitized (the traversal guard runs in the download branch).
        with patch("boto3.client") as mock_boto:
            mock_boto.return_value = MagicMock()
            with pytest.raises(ValueError, match="[Tt]raversal"):
                await _tool_bucket_read(
                    "download", "conn1", "u1", "prod", key="../../etc/passwd",
                    resolved_connection={"bucket": "my-bucket"},
                )

    def test_allowed_actions_set(self):
        assert _BUCKET_ALLOWED_ACTIONS == {"list", "download"}


# ============================================================================
# SFTP Read-Only Guard Tests
# ============================================================================

class TestSFTPReadGuard:

    @pytest.mark.asyncio
    async def test_delete_action_rejected(self):
        result = await _tool_sftp_read("delete", "conn1", "u1", "prod")
        assert "Only 'list' and 'download'" in result

    @pytest.mark.asyncio
    async def test_upload_action_rejected(self):
        result = await _tool_sftp_read("upload", "conn1", "u1", "prod")
        assert "Only 'list' and 'download'" in result

    @pytest.mark.asyncio
    async def test_rename_action_rejected(self):
        result = await _tool_sftp_read("rename", "conn1", "u1", "prod")
        assert "Only 'list' and 'download'" in result

    @pytest.mark.asyncio
    async def test_path_traversal_blocked(self):
        """Path traversal in remote_path should be rejected."""
        with pytest.raises(ValueError, match="[Tt]raversal"):
            await _tool_sftp_read("list", "conn1", "u1", "prod", remote_path="../../etc")

    @pytest.mark.asyncio
    async def test_missing_host(self):
        """If resolved connection has no host, return an error."""
        # Connections are pre-resolved by the agent runner and passed in.
        result = await _tool_sftp_read(
            "list", "conn1", "u1", "prod", remote_path="/data",
            resolved_connection={"port": 22, "username": "user"},
        )
        assert "no host" in result.lower()

    def test_allowed_actions_set(self):
        assert _SFTP_ALLOWED_ACTIONS == {"list", "download"}


# ============================================================================
# Semicolon Injection Tests (new — covers _validate_readonly_sql semicolon fix)
# ============================================================================

class TestSemicolonInjection:

    def test_semicolon_after_select(self):
        with pytest.raises(ValueError, match="Semicolons"):
            _validate_readonly_sql("SELECT 1; DROP TABLE users")

    def test_semicolon_multiline(self):
        with pytest.raises(ValueError, match="Semicolons"):
            _validate_readonly_sql("SELECT * FROM orders;\nDELETE FROM orders")

    def test_no_semicolon_in_clean_select(self):
        # Should NOT raise
        _validate_readonly_sql("SELECT * FROM users WHERE name = 'test'")

    def test_no_semicolon_in_cte(self):
        _validate_readonly_sql(
            "WITH ranked AS (SELECT *, ROW_NUMBER() OVER (ORDER BY id) rn FROM t) "
            "SELECT * FROM ranked WHERE rn <= 10"
        )


# ============================================================================
# Connection Pre-Resolution Tests (_run_agent_with_tools)
# ============================================================================

class TestConnectionPreResolution:
    """Tests that connections are resolved once before the LLM loop,
    validated immediately, and passed to tool calls."""

    @pytest.mark.asyncio
    async def test_sql_connection_resolved_once(self):
        """resolve_connection should be called exactly once for SQL,
        not once per tool call."""
        mock_resolve = AsyncMock(return_value={
            "connection_string": "postgresql://localhost/db"
        })
        mock_llm_resp_tool = MagicMock()
        mock_llm_resp_tool.choices = [MagicMock()]
        tool_call = MagicMock()
        tool_call.function.name = "sql_query"
        tool_call.function.arguments = json.dumps({"sql": "SELECT 1"})
        tool_call.id = "call_1"
        mock_llm_resp_tool.choices[0].message.content = None
        mock_llm_resp_tool.choices[0].message.tool_calls = [tool_call]

        mock_llm_resp_final = MagicMock()
        mock_llm_resp_final.choices = [MagicMock()]
        mock_llm_resp_final.choices[0].message.content = "Done"
        mock_llm_resp_final.choices[0].message.tool_calls = None

        from citra_workflow.nodes.agents import _run_agent_with_tools

        with patch("citra_workflow.connection_resolver.resolve_connection", mock_resolve), \
             patch("citra_workflow.schema_cache.get_or_discover_schema", new_callable=AsyncMock, return_value=None), \
             patch("citra_workflow.tool_skills.build_tool_skills_section", return_value=""), \
             patch("openai.OpenAI") as mock_openai:
            mock_client = MagicMock()
            mock_openai.return_value = mock_client
            mock_client.chat.completions.create.side_effect = [
                mock_llm_resp_tool, mock_llm_resp_final
            ]
            with patch("citra_workflow.nodes.agents._tool_sql_query", new_callable=AsyncMock, return_value='[{"x":1}]'):
                result = await _run_agent_with_tools(
                    system_prompt="test",
                    user_message="query the db",
                    tier="small",
                    tool_names=["sql_query"],
                    user_id="u1",
                    environment="test",
                    sql_connection_id="conn-sql-1",
                )

        # resolve_connection called once during pre-resolution, not per tool call
        assert mock_resolve.call_count == 1
        assert mock_resolve.call_args[0] == ("conn-sql-1",)
        assert mock_resolve.call_args.kwargs["environment"] == "test"

    @pytest.mark.asyncio
    async def test_missing_connection_string_fails_fast(self):
        """If the SQL connection has no connection_string, should raise
        immediately instead of entering the LLM loop."""
        mock_resolve = AsyncMock(return_value={
            "connection_string": ""  # empty = misconfigured
        })

        from citra_workflow.nodes.agents import _run_agent_with_tools

        with patch("citra_workflow.connection_resolver.resolve_connection", mock_resolve), \
             patch("citra_workflow.schema_cache.get_or_discover_schema", new_callable=AsyncMock, return_value=None), \
             patch("citra_workflow.tool_skills.build_tool_skills_section", return_value=""), \
             patch("openai.OpenAI"):
            with pytest.raises(ValueError, match="missing required field.*connection_string"):
                await _run_agent_with_tools(
                    system_prompt="test",
                    user_message="test",
                    tier="small",
                    tool_names=["sql_query"],
                    user_id="u1",
                    sql_connection_id="conn-bad",
                )

    @pytest.mark.asyncio
    async def test_missing_nosql_database_fails_fast(self):
        """NoSQL connection without database should fail before LLM loop."""
        mock_resolve = AsyncMock(return_value={
            "connection_string": "mongodb://localhost",
            "database": "",  # empty
        })

        from citra_workflow.nodes.agents import _run_agent_with_tools

        with patch("citra_workflow.connection_resolver.resolve_connection", mock_resolve), \
             patch("citra_workflow.schema_cache.get_or_discover_schema", new_callable=AsyncMock, return_value=None), \
             patch("citra_workflow.tool_skills.build_tool_skills_section", return_value=""), \
             patch("openai.OpenAI"):
            with pytest.raises(ValueError, match="missing required field.*database"):
                await _run_agent_with_tools(
                    system_prompt="test",
                    user_message="test",
                    tier="small",
                    tool_names=["nosql_query"],
                    user_id="u1",
                    nosql_connection_id="conn-nosql-bad",
                )

    @pytest.mark.asyncio
    async def test_missing_s3_bucket_fails_fast(self):
        """S3 connection without bucket should fail before LLM loop."""
        mock_resolve = AsyncMock(return_value={
            "bucket": "",
            "region": "us-east-1",
        })

        from citra_workflow.nodes.agents import _run_agent_with_tools

        with patch("citra_workflow.connection_resolver.resolve_connection", mock_resolve), \
             patch("citra_workflow.schema_cache.get_or_discover_schema", new_callable=AsyncMock, return_value=None), \
             patch("citra_workflow.tool_skills.build_tool_skills_section", return_value=""), \
             patch("openai.OpenAI"):
            with pytest.raises(ValueError, match="missing required field.*bucket"):
                await _run_agent_with_tools(
                    system_prompt="test",
                    user_message="test",
                    tier="small",
                    tool_names=["bucket_read"],
                    user_id="u1",
                    bucket_connection_id="conn-s3-bad",
                )

    @pytest.mark.asyncio
    async def test_missing_sftp_host_fails_fast(self):
        """SFTP connection without host should fail before LLM loop."""
        mock_resolve = AsyncMock(return_value={
            "host": "",
            "port": 22,
        })

        from citra_workflow.nodes.agents import _run_agent_with_tools

        with patch("citra_workflow.connection_resolver.resolve_connection", mock_resolve), \
             patch("citra_workflow.schema_cache.get_or_discover_schema", new_callable=AsyncMock, return_value=None), \
             patch("citra_workflow.tool_skills.build_tool_skills_section", return_value=""), \
             patch("openai.OpenAI"):
            with pytest.raises(ValueError, match="missing required field.*host"):
                await _run_agent_with_tools(
                    system_prompt="test",
                    user_message="test",
                    tier="small",
                    tool_names=["sftp_read"],
                    user_id="u1",
                    sftp_connection_id="conn-sftp-bad",
                )

    @pytest.mark.asyncio
    async def test_connection_resolve_failure_raises(self):
        """When resolve_connection itself fails, agent should raise immediately."""
        mock_resolve = AsyncMock(side_effect=ValueError("Connection 'x' not found"))

        from citra_workflow.nodes.agents import _run_agent_with_tools

        with patch("citra_workflow.connection_resolver.resolve_connection", mock_resolve), \
             patch("citra_workflow.schema_cache.get_or_discover_schema", new_callable=AsyncMock, return_value=None), \
             patch("citra_workflow.tool_skills.build_tool_skills_section", return_value=""), \
             patch("openai.OpenAI"):
            with pytest.raises(ValueError, match="Failed to resolve connection"):
                await _run_agent_with_tools(
                    system_prompt="test",
                    user_message="test",
                    tier="small",
                    tool_names=["sql_query"],
                    user_id="u1",
                    sql_connection_id="conn-nonexistent",
                )

    @pytest.mark.asyncio
    async def test_no_connection_tools_skips_preresolve(self):
        """When only non-connection tools are selected (web_search, code_execute),
        resolve_connection should not be called at all."""
        mock_resolve = AsyncMock()

        mock_llm_resp = MagicMock()
        mock_llm_resp.choices = [MagicMock()]
        mock_llm_resp.choices[0].message.content = "No connections needed"
        mock_llm_resp.choices[0].message.tool_calls = None

        from citra_workflow.nodes.agents import _run_agent_with_tools

        with patch("citra_workflow.connection_resolver.resolve_connection", mock_resolve), \
             patch("citra_workflow.schema_cache.get_or_discover_schema", new_callable=AsyncMock, return_value=None), \
             patch("citra_workflow.tool_skills.build_tool_skills_section", return_value=""), \
             patch("openai.OpenAI") as mock_openai:
            mock_client = MagicMock()
            mock_openai.return_value = mock_client
            mock_client.chat.completions.create.return_value = mock_llm_resp

            result = await _run_agent_with_tools(
                system_prompt="test",
                user_message="search for python",
                tier="small",
                tool_names=["web_search", "code_execute"],
                user_id="u1",
            )

        # No connection-based tools → resolve_connection never called
        mock_resolve.assert_not_called()
        assert result["result"] == "No connections needed"


# ============================================================================
# Per-Tool Timeout Tests
# ============================================================================

class TestPerToolTimeout:

    @pytest.mark.asyncio
    async def test_tool_timeout_returns_descriptive_error(self):
        """When a tool call exceeds AGENT_TOOL_CALL_TIMEOUT, the error
        message should name the tool and the timeout value."""
        from citra_workflow.nodes.agents import _run_agent_with_tools

        mock_resolve = AsyncMock(return_value={
            "connection_string": "postgresql://localhost/db"
        })

        # LLM calls sql_query tool
        tool_call = MagicMock()
        tool_call.function.name = "sql_query"
        tool_call.function.arguments = json.dumps({"sql": "SELECT * FROM big_table"})
        tool_call.id = "call_t1"

        mock_resp_tool = MagicMock()
        mock_resp_tool.choices = [MagicMock()]
        mock_resp_tool.choices[0].message.content = None
        mock_resp_tool.choices[0].message.tool_calls = [tool_call]
        mock_resp_tool.choices[0].message.model_dump = MagicMock(return_value={
            "role": "assistant", "content": None, "tool_calls": [{"id": "call_t1"}]
        })

        mock_resp_final = MagicMock()
        mock_resp_final.choices = [MagicMock()]
        mock_resp_final.choices[0].message.content = "Timed out response"
        mock_resp_final.choices[0].message.tool_calls = None

        # Make the SQL tool "hang" by raising TimeoutError
        async def _slow_sql(*args, **kwargs):
            import asyncio
            await asyncio.sleep(999)

        with patch("citra_workflow.connection_resolver.resolve_connection", mock_resolve), \
             patch("citra_workflow.schema_cache.get_or_discover_schema", new_callable=AsyncMock, return_value=None), \
             patch("citra_workflow.tool_skills.build_tool_skills_section", return_value=""), \
             patch("citra_workflow.nodes.agents._tool_sql_query", side_effect=_slow_sql), \
             patch("citra_workflow.nodes.agents.AGENT_TOOL_CALL_TIMEOUT", 0.01), \
             patch("openai.OpenAI") as mock_openai:
            mock_client = MagicMock()
            mock_openai.return_value = mock_client
            mock_client.chat.completions.create.side_effect = [
                mock_resp_tool, mock_resp_final
            ]

            result = await _run_agent_with_tools(
                system_prompt="test",
                user_message="run query",
                tier="small",
                tool_names=["sql_query"],
                user_id="u1",
                sql_connection_id="conn-sql-1",
            )

        # The tool_calls log should show the timed out call
        assert len(result["tool_calls"]) == 1
        assert result["tool_calls"][0]["tool"] == "sql_query"


# ============================================================================
# Resolved Connection Passthrough Tests (tool functions)
# ============================================================================

class TestResolvedConnectionPassthrough:
    """Verify that tool functions skip resolve_connection when
    resolved_connection kwarg is provided."""

    @pytest.mark.asyncio
    async def test_sql_tool_uses_resolved_connection(self):
        """When resolved_connection is passed, _tool_sql_query should NOT
        call resolve_connection."""
        import sqlalchemy

        resolved = {"connection_string": "sqlite:///:memory:"}

        with patch("citra_workflow.connection_resolver.resolve_connection", new_callable=AsyncMock) as mock_resolve, \
             patch("sqlalchemy.create_engine") as mock_engine:
            # Mock the engine and connection
            mock_conn = MagicMock()
            mock_result = MagicMock()
            mock_result.keys.return_value = ["id"]
            mock_result.fetchmany.return_value = [(1,)]
            mock_conn.__enter__ = MagicMock(return_value=mock_conn)
            mock_conn.__exit__ = MagicMock(return_value=False)
            mock_conn.execute.return_value = mock_result
            mock_eng = MagicMock()
            mock_eng.connect.return_value = mock_conn
            mock_engine.return_value = mock_eng

            from citra_workflow.nodes.agents import _tool_sql_query
            result = await _tool_sql_query(
                "SELECT 1",
                "conn-1", "u1", "test",
                resolved_connection=resolved,
            )

        # resolve_connection should NOT have been called
        mock_resolve.assert_not_called()
        parsed = json.loads(result)
        assert isinstance(parsed, list)

    @pytest.mark.asyncio
    async def test_nosql_tool_uses_resolved_connection(self):
        """When resolved_connection is passed, _tool_nosql_query should NOT
        call resolve_connection."""
        resolved = {
            "connection_string": "mongodb://localhost",
            "database": "testdb",
        }

        mock_cursor = AsyncMock()
        mock_cursor.__aiter__ = MagicMock()

        async def async_gen():
            yield {"_id": "abc", "name": "test"}
        mock_cursor.__aiter__.return_value = async_gen()

        with patch("citra_workflow.connection_resolver.resolve_connection", new_callable=AsyncMock) as mock_resolve, \
             patch("motor.motor_asyncio.AsyncIOMotorClient") as mock_motor:
            mock_client = MagicMock()
            mock_motor.return_value = mock_client
            mock_db = MagicMock()
            mock_client.__getitem__ = MagicMock(return_value=mock_db)
            mock_col = MagicMock()
            mock_db.__getitem__ = MagicMock(return_value=mock_col)
            mock_col.find.return_value.limit.return_value = mock_cursor

            from citra_workflow.nodes.agents import _tool_nosql_query
            result = await _tool_nosql_query(
                "users", {}, "conn-1", "u1", "test",
                resolved_connection=resolved,
            )

        mock_resolve.assert_not_called()
        parsed = json.loads(result)
        assert isinstance(parsed, list)


# ============================================================================
# Environment Fallback Warning Tests
# ============================================================================

class TestEnvironmentFallback:

    @pytest.mark.asyncio
    async def test_unknown_env_logs_warning(self):
        """When an unknown environment is passed, resolve_connection should
        fall back to 'test' and log a warning."""
        from citra_workflow.connection_resolver import resolve_connection

        mock_doc = {
            "connection_id": "conn-1",
            "user_id": "u1",
            "org_id": "org-1",
            "owner_id": "sa-1",
            "type": "sql",
            "test": {"connection_string": "sqlite:///:memory:"},
        }
        mock_col = AsyncMock()
        mock_col.find_one = AsyncMock(return_value=mock_doc)
        mock_db = MagicMock()
        mock_db.__getitem__ = MagicMock(return_value=mock_col)
        mock_client = MagicMock()
        mock_client.__getitem__ = MagicMock(return_value=mock_db)

        with patch("citra_mongo.get_async_mongo_client", return_value=mock_client), \
             patch("citra_mongo.MONGODB_DATABASE", "test_db"), \
             patch("citra_workflow.connection_crypto.decrypt_env_config", side_effect=lambda x: x), \
             patch("citra_workflow.connection_resolver.logger") as mock_logger:
            result = await resolve_connection(
                "conn-1", org_id="org-1", owner_id="sa-1", environment="staging",
            )

        # Should have logged a warning about the fallback
        mock_logger.warning.assert_called_once()
        warning_msg = mock_logger.warning.call_args[0][0]
        assert "staging" in warning_msg or "Unknown" in warning_msg.lower() or "falling back" in warning_msg.lower()
        # Should still return the test config
        assert result["connection_string"] == "sqlite:///:memory:"


# ============================================================================
# Workflow Context Extractor Tests
# ============================================================================

class TestWorkflowContextExtractor:

    def test_extracts_sql_source_context(self):
        """Should extract table names and columns from an upstream SQL source node."""
        from citra_workflow.workflow_context_extractor import extract_workflow_context

        node_map = {
            "sql1": MagicMock(
                type="sql_source",
                config={
                    "connection_id": "conn-sql-1",
                    "query": "SELECT id, name, email FROM users WHERE active = 1",
                },
            ),
            "agent1": MagicMock(type="ai_agent", config={}),
        }
        node_outputs = {
            "sql1": {"items": [{"id": 1, "name": "Alice", "email": "a@b.com"}]},
        }
        incoming = {"agent1": ["sql1"]}

        result = extract_workflow_context("agent1", node_map, node_outputs, incoming)

        assert "sql:conn-sql-1" in result
        ctx = result["sql:conn-sql-1"]
        assert "users" in ctx["tables_mentioned"]
        assert "id" in ctx["columns_seen"]
        assert "name" in ctx["columns_seen"]
        assert "email" in ctx["columns_seen"]
        assert len(ctx["queries"]) == 1

    def test_extracts_nosql_source_context(self):
        """Should extract collection names and fields from upstream NoSQL source."""
        from citra_workflow.workflow_context_extractor import extract_workflow_context

        node_map = {
            "mongo1": MagicMock(
                type="mongo_source",
                config={
                    "connection_id": "conn-nosql-1",
                    "database": "mydb",
                    "collection": "applicants",
                },
            ),
            "agent1": MagicMock(type="ai_agent", config={}),
        }
        node_outputs = {
            "mongo1": {"items": [{"_id": "x", "name": "Bob", "status": "pending"}]},
        }
        incoming = {"agent1": ["mongo1"]}

        result = extract_workflow_context("agent1", node_map, node_outputs, incoming)

        assert "nosql:conn-nosql-1" in result
        ctx = result["nosql:conn-nosql-1"]
        assert "applicants" in ctx["collections_mentioned"]
        assert "_id" in ctx["fields_seen"]
        assert "name" in ctx["fields_seen"]

    def test_upstream_columns_from_direct_parent(self):
        """_upstream_columns should include column names from direct parent output."""
        from citra_workflow.workflow_context_extractor import extract_workflow_context

        node_map = {
            "filter1": MagicMock(type="filter", config={}),
            "agent1": MagicMock(type="ai_agent", config={}),
        }
        node_outputs = {
            "filter1": {"items": [{"score": 95, "label": "A"}]},
        }
        incoming = {"agent1": ["filter1"]}

        result = extract_workflow_context("agent1", node_map, node_outputs, incoming)

        assert "_upstream_columns" in result
        assert "score" in result["_upstream_columns"]["columns"]
        assert "label" in result["_upstream_columns"]["columns"]

    def test_agent_to_agent_chain_extracts_connections(self):
        """An upstream AI Agent node should propagate its connection IDs."""
        from citra_workflow.workflow_context_extractor import extract_workflow_context

        node_map = {
            "agent_upstream": MagicMock(
                type="ai_agent",
                config={
                    "sql_connection_id": "conn-sql-shared",
                    "nosql_connection_id": "conn-nosql-shared",
                },
            ),
            "agent_downstream": MagicMock(type="ai_agent", config={}),
        }
        node_outputs = {
            "agent_upstream": {"items": [{"total": 42, "category": "Electronics"}]},
        }
        incoming = {"agent_downstream": ["agent_upstream"]}

        result = extract_workflow_context("agent_downstream", node_map, node_outputs, incoming)

        # Should have extracted the upstream agent's SQL connection
        assert "sql:conn-sql-shared" in result
        sql_ctx = result["sql:conn-sql-shared"]
        # Output columns should be propagated
        assert "total" in sql_ctx.get("columns_seen", [])
        assert "category" in sql_ctx.get("columns_seen", [])

        # Should also have the NoSQL connection
        assert "nosql:conn-nosql-shared" in result

    def test_multi_hop_ancestors(self):
        """Should walk multiple hops: sql_source -> transform -> agent."""
        from citra_workflow.workflow_context_extractor import extract_workflow_context

        node_map = {
            "sql1": MagicMock(
                type="sql_source",
                config={
                    "connection_id": "conn-sql-deep",
                    "query": "SELECT * FROM orders JOIN products ON orders.product_id = products.id",
                },
            ),
            "transform1": MagicMock(type="transform", config={}),
            "agent1": MagicMock(type="ai_agent", config={}),
        }
        node_outputs = {
            "sql1": {"items": [{"order_id": 1, "product_name": "Widget"}]},
            "transform1": {"items": [{"order_id": 1, "product_name": "Widget", "total": 99}]},
        }
        incoming = {
            "transform1": ["sql1"],
            "agent1": ["transform1"],
        }

        result = extract_workflow_context("agent1", node_map, node_outputs, incoming)

        assert "sql:conn-sql-deep" in result
        ctx = result["sql:conn-sql-deep"]
        assert "orders" in ctx["tables_mentioned"]
        assert "products" in ctx["tables_mentioned"]


# ============================================================================
# Schema Cache Tests
# ============================================================================

class TestSchemaCache:

    @pytest.mark.asyncio
    async def test_get_cached_returns_none_on_miss(self):
        from citra_workflow.schema_cache import get_cached_schema

        with patch("citra_workflow.schema_cache._get_cache") as mock_cache_fn:
            mock_cache_fn.return_value.get.return_value = None
            result = await get_cached_schema("conn-1", "test")

        assert result is None

    @pytest.mark.asyncio
    async def test_get_cached_returns_parsed_json(self):
        from citra_workflow.schema_cache import get_cached_schema

        cached_data = {"type": "sql", "tables": [{"name": "users"}]}
        with patch("citra_workflow.schema_cache._get_cache") as mock_cache_fn:
            mock_cache_fn.return_value.get.return_value = json.dumps(cached_data)
            result = await get_cached_schema("conn-1", "test")

        assert result == cached_data

    @pytest.mark.asyncio
    async def test_cache_schema_stores_with_ttl(self):
        from citra_workflow.schema_cache import cache_schema, SCHEMA_CACHE_TTL

        schema = {"type": "sql", "tables": []}
        with patch("citra_workflow.schema_cache._get_cache") as mock_cache_fn:
            mock_cache = mock_cache_fn.return_value
            await cache_schema("conn-1", "test", schema)

        mock_cache.set.assert_called_once()
        call_args = mock_cache.set.call_args
        assert call_args[1]["ex"] == SCHEMA_CACHE_TTL

    @pytest.mark.asyncio
    async def test_invalidate_clears_both_envs(self):
        from citra_workflow.schema_cache import invalidate_schema

        with patch("citra_workflow.schema_cache._get_cache") as mock_cache_fn:
            mock_cache = mock_cache_fn.return_value
            await invalidate_schema("conn-1")

        mock_cache.delete.assert_called_once()
        delete_args = mock_cache.delete.call_args[0]
        assert "agent:schema:conn-1:test" in delete_args
        assert "agent:schema:conn-1:prod" in delete_args

    @pytest.mark.asyncio
    async def test_three_tier_workflow_sufficient_skips_remote(self):
        """When workflow context has tables + columns, Tier 3 remote discovery
        should be skipped entirely."""
        from citra_workflow.schema_cache import get_or_discover_schema

        wf_context = {
            "sql:conn-1": {
                "type": "sql",
                "connection_id": "conn-1",
                "tables_mentioned": ["orders"],
                "columns_seen": ["id", "amount", "status"],
                "queries": ["SELECT id, amount, status FROM orders"],
            }
        }

        with patch("citra_workflow.schema_cache.get_cached_schema", new_callable=AsyncMock) as mock_get, \
             patch("citra_workflow.schema_discovery.DISCOVERY_FUNCTIONS", {"sql_query": AsyncMock()}):
            result = await get_or_discover_schema(
                "sql_query", "conn-1", "u1", "test",
                workflow_context=wf_context,
            )

        # Should NOT have checked Redis cache since workflow context was sufficient
        mock_get.assert_not_called()
        assert result is not None
        assert result.get("_from_workflow") is True
        assert "orders" in result.get("tables_mentioned", [])


# ============================================================================
# Tool Skills Formatting Tests
# ============================================================================

class TestToolSkills:

    def test_build_skills_section_with_upstream_columns(self):
        """build_tool_skills_section should render _upstream_columns
        in the prompt when workflow_context provides them."""
        from citra_workflow.tool_skills import build_tool_skills_section

        tool_schemas = {"sql_query": None}
        wf_context = {
            "_upstream_columns": {"columns": ["order_id", "product", "total"]},
        }

        result = build_tool_skills_section(tool_schemas, workflow_context=wf_context)

        assert "Upstream Data Context" in result
        assert "order_id" in result
        assert "product" in result
        assert "total" in result

    def test_build_skills_section_without_upstream_columns(self):
        """When no _upstream_columns, footer should not appear."""
        from citra_workflow.tool_skills import build_tool_skills_section

        tool_schemas = {"web_search": None}
        result = build_tool_skills_section(tool_schemas, workflow_context=None)

        assert "Upstream Data Context" not in result

    def test_format_tool_skill_with_workflow_context(self):
        """format_tool_skill should render workflow context block first."""
        from citra_workflow.tool_skills import format_tool_skill

        schema = {
            "_from_workflow": True,
            "type": "sql",
            "tables_mentioned": ["inventory", "suppliers"],
            "columns_seen": ["item_id", "quantity", "supplier_name"],
            "queries": ["SELECT * FROM inventory"],
        }

        result = format_tool_skill("sql_query", schema)

        assert "Known from this workflow" in result
        assert "inventory" in result
        assert "suppliers" in result

    def test_format_tool_skill_code_execute(self):
        """code_execute should get a skill block even without schema."""
        from citra_workflow.tool_skills import format_tool_skill

        result = format_tool_skill("code_execute", None)

        assert "Python Code Execution" in result
        assert "sandbox" in result.lower()

    def test_empty_schema_dict(self):
        """build_tool_skills_section with empty dict returns empty string."""
        from citra_workflow.tool_skills import build_tool_skills_section

        result = build_tool_skills_section({})
        assert result == ""


# ============================================================================
# Tool Using Resolved Connection (integration-level)
# ============================================================================

class TestToolResolvedConnectionIntegration:
    """Integration-level tests: full _run_agent_with_tools with resolved
    connections, verifying the connection is passed through to the tool."""

    @pytest.mark.asyncio
    async def test_multiple_connections_all_preresolve(self):
        """When SQL and S3 connections are configured, both should be
        pre-resolved in a single batch."""
        call_order = []

        async def mock_resolve(conn_id, *, org_id, owner_id="", owner_type="", environment="test"):
            call_order.append(conn_id)
            if conn_id == "conn-sql":
                return {"connection_string": "sqlite:///:memory:"}
            elif conn_id == "conn-s3":
                return {"bucket": "test-bucket", "region": "us-east-1"}
            return {}

        mock_llm_resp = MagicMock()
        mock_llm_resp.choices = [MagicMock()]
        mock_llm_resp.choices[0].message.content = "All done"
        mock_llm_resp.choices[0].message.tool_calls = None

        from citra_workflow.nodes.agents import _run_agent_with_tools

        with patch("citra_workflow.connection_resolver.resolve_connection", side_effect=mock_resolve), \
             patch("citra_workflow.schema_cache.get_or_discover_schema", new_callable=AsyncMock, return_value=None), \
             patch("citra_workflow.tool_skills.build_tool_skills_section", return_value=""), \
             patch("openai.OpenAI") as mock_openai:
            mock_client = MagicMock()
            mock_openai.return_value = mock_client
            mock_client.chat.completions.create.return_value = mock_llm_resp

            result = await _run_agent_with_tools(
                system_prompt="test",
                user_message="hello",
                tier="small",
                tool_names=["sql_query", "bucket_read"],
                user_id="u1",
                sql_connection_id="conn-sql",
                bucket_connection_id="conn-s3",
            )

        # Both connections should have been resolved
        assert "conn-sql" in call_order
        assert "conn-s3" in call_order
        assert result["result"] == "All done"
