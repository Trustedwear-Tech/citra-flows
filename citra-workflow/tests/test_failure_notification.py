"""
Unit tests for workflow failure email notification.
Tests send_execution_failure_notification() and the executor._notify_failure() wiring.
"""

import sys
import os
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))


# ============================================================================
# send_execution_failure_notification() tests
# ============================================================================

class TestSendExecutionFailureNotification:

    @pytest.mark.asyncio
    async def test_sends_email_with_correct_subject(self):
        """Email subject should contain workflow name."""
        with patch("citra_workflow.notifications._send_email", new_callable=AsyncMock) as mock_send:
            mock_send.return_value = True
            from citra_workflow.notifications import send_execution_failure_notification

            result = await send_execution_failure_notification(
                to_email="test@example.com",
                user_name="Test User",
                workflow_name="My Data Pipeline",
                execution_id="exec-123",
                failed_node_label="API Source",
                error_message="Connection timeout",
            )

            assert result is True
            mock_send.assert_called_once()
            args = mock_send.call_args
            assert args[0][0] == "test@example.com"
            assert "My Data Pipeline" in args[0][1]  # subject
            assert "❌" in args[0][1]

    @pytest.mark.asyncio
    async def test_html_contains_error_details(self):
        """HTML body should include failed node label, error message, and execution ID."""
        with patch("citra_workflow.notifications._send_email", new_callable=AsyncMock) as mock_send:
            mock_send.return_value = True
            from citra_workflow.notifications import send_execution_failure_notification

            await send_execution_failure_notification(
                to_email="test@example.com",
                user_name="Alice",
                workflow_name="ETL Pipeline",
                execution_id="exec-456",
                failed_node_label="SQL Source",
                error_message="Connection refused to database",
            )

            html_body = mock_send.call_args[0][3]  # 4th positional arg
            assert "SQL Source" in html_body
            assert "Connection refused" in html_body
            assert "exec-456" in html_body
            assert "Alice" in html_body

    @pytest.mark.asyncio
    async def test_html_includes_retry_info(self):
        """When retry_count > 0, HTML should show retry count and individual errors."""
        with patch("citra_workflow.notifications._send_email", new_callable=AsyncMock) as mock_send:
            mock_send.return_value = True
            from citra_workflow.notifications import send_execution_failure_notification

            await send_execution_failure_notification(
                to_email="test@example.com",
                user_name="Bob",
                workflow_name="API Sync",
                execution_id="exec-789",
                failed_node_label="API Call",
                error_message="Rate limited",
                retry_count=3,
                retry_errors=["Rate limited", "Rate limited", "Rate limited"],
            )

            html_body = mock_send.call_args[0][3]
            assert "3" in html_body  # retry count
            assert "Attempt 1" in html_body
            assert "Rate limited" in html_body

    @pytest.mark.asyncio
    async def test_text_fallback_includes_details(self):
        """Plain text fallback should include key information."""
        with patch("citra_workflow.notifications._send_email", new_callable=AsyncMock) as mock_send:
            mock_send.return_value = True
            from citra_workflow.notifications import send_execution_failure_notification

            await send_execution_failure_notification(
                to_email="test@example.com",
                user_name="Charlie",
                workflow_name="Report Generator",
                execution_id="exec-abc",
                failed_node_label="PDF Export",
                error_message="Render failed",
                retry_count=1,
            )

            text_body = mock_send.call_args[0][2]  # 3rd positional arg
            assert "Report Generator" in text_body
            assert "PDF Export" in text_body
            assert "Render failed" in text_body
            assert "Retries: 1" in text_body

    @pytest.mark.asyncio
    async def test_no_retry_info_when_zero(self):
        """When retry_count=0, HTML should NOT include retry section."""
        with patch("citra_workflow.notifications._send_email", new_callable=AsyncMock) as mock_send:
            mock_send.return_value = True
            from citra_workflow.notifications import send_execution_failure_notification

            await send_execution_failure_notification(
                to_email="test@example.com",
                user_name="Dave",
                workflow_name="Quick Job",
                execution_id="exec-def",
                failed_node_label="LLM Processor",
                error_message="Model overloaded",
            )

            html_body = mock_send.call_args[0][3]
            assert "retried" not in html_body.lower() or "0" not in html_body
            # The retry_info block should be empty
            text_body = mock_send.call_args[0][2]
            assert "Retries:" not in text_body

    @pytest.mark.asyncio
    async def test_deep_link_in_html(self):
        """HTML should contain a deep link to the execution details."""
        with patch("citra_workflow.notifications._send_email", new_callable=AsyncMock) as mock_send:
            mock_send.return_value = True
            from citra_workflow.notifications import send_execution_failure_notification

            await send_execution_failure_notification(
                to_email="test@example.com",
                user_name="Eve",
                workflow_name="Test WF",
                execution_id="exec-deep-link",
                failed_node_label="Node X",
                error_message="Error",
            )

            html_body = mock_send.call_args[0][3]
            assert "view=execution" in html_body
            assert "exec-deep-link" in html_body


# ============================================================================
# executor._notify_failure() wiring tests
# ============================================================================

class TestExecutorNotifyFailure:

    @pytest.mark.asyncio
    async def test_notify_failure_looks_up_user_email(self):
        """_notify_failure should query MongoDB for user email and call send_execution_failure_notification."""
        from citra_workflow.models import WorkflowExecution, WorkflowDefinition

        execution = WorkflowExecution(
            execution_id="exec-test",
            workflow_id="wf-test",
            user_id="uid-123",
        )
        workflow = WorkflowDefinition(
            workflow_id="wf-test",
            user_id="uid-123",
            name="Test Workflow",
        )

        mock_users_col = AsyncMock()
        mock_users_col.find_one = AsyncMock(return_value={
            "uid": "uid-123",
            "email": "user@example.com",
            "displayName": "Test User",
        })

        mock_db = MagicMock()
        mock_db.__getitem__ = MagicMock(return_value=mock_users_col)

        mock_client = MagicMock()
        mock_client.__getitem__ = MagicMock(return_value=mock_db)

        with patch("citra_cache.get_cache_manager", return_value=MagicMock()):
            from citra_workflow.executor import WorkflowExecutor
            executor = WorkflowExecutor()

        with patch("citra_mongo.get_async_mongo_client", return_value=mock_client), \
             patch("citra_mongo.MONGODB_DATABASE", "test_db"), \
             patch("citra_workflow.notifications.send_execution_failure_notification",
                   new_callable=AsyncMock) as mock_notify:
            mock_notify.return_value = True

            await executor._notify_failure(
                execution, workflow,
                failed_node_label="API Source",
                error_message="Timeout",
                retry_count=2,
                retry_errors=["Timeout", "Timeout"],
            )

            mock_users_col.find_one.assert_called_once()
            mock_notify.assert_called_once()

    @pytest.mark.asyncio
    async def test_notify_failure_no_email_logs_warning(self):
        """If user has no email, _notify_failure should log warning and not crash."""
        from citra_workflow.models import WorkflowExecution, WorkflowDefinition

        execution = WorkflowExecution(
            execution_id="exec-no-email",
            workflow_id="wf-test",
            user_id="uid-no-email",
        )
        workflow = WorkflowDefinition(
            workflow_id="wf-test",
            user_id="uid-no-email",
            name="Test Workflow",
        )

        mock_users_col = AsyncMock()
        mock_users_col.find_one = AsyncMock(return_value={"uid": "uid-no-email"})

        mock_db = MagicMock()
        mock_db.__getitem__ = MagicMock(return_value=mock_users_col)

        mock_client = MagicMock()
        mock_client.__getitem__ = MagicMock(return_value=mock_db)

        with patch("citra_cache.get_cache_manager", return_value=MagicMock()):
            from citra_workflow.executor import WorkflowExecutor
            executor = WorkflowExecutor()

        with patch("citra_mongo.get_async_mongo_client", return_value=mock_client), \
             patch("citra_mongo.MONGODB_DATABASE", "test_db"):
            # Should not raise
            await executor._notify_failure(
                execution, workflow,
                failed_node_label="Node X",
                error_message="Error",
            )

    @pytest.mark.asyncio
    async def test_notify_failure_exception_does_not_propagate(self):
        """If notification sending fails, it should not propagate the exception."""
        from citra_workflow.models import WorkflowExecution, WorkflowDefinition

        execution = WorkflowExecution(
            execution_id="exec-err",
            workflow_id="wf-test",
            user_id="uid-err",
        )
        workflow = WorkflowDefinition(
            workflow_id="wf-test",
            user_id="uid-err",
            name="Test Workflow",
        )

        with patch("citra_cache.get_cache_manager", return_value=MagicMock()):
            from citra_workflow.executor import WorkflowExecutor
            executor = WorkflowExecutor()

        with patch("citra_mongo.get_async_mongo_client", side_effect=Exception("DB down")), \
             patch("citra_mongo.MONGODB_DATABASE", "test_db"):
            # Should not raise despite MongoDB being down
            await executor._notify_failure(
                execution, workflow,
                failed_node_label="Node X",
                error_message="Error",
            )

    @pytest.mark.asyncio
    async def test_notify_failure_emails_owner_only_and_ignores_injected_support(self):
        """_notify_failure emails ONLY the owner (author_email). Arbitrary
        recipients are not supported — even a support_emails value injected via
        a raw doc (legacy / API tampering) must never receive an email."""
        from citra_workflow.models import WorkflowExecution, WorkflowDefinition

        execution = WorkflowExecution(
            execution_id="exec-owner", workflow_id="wf-test", user_id="uid-ba",
        )
        # Build from a raw dict that smuggles in support_emails — the model
        # drops the unknown field, and the executor never reads it regardless.
        workflow = WorkflowDefinition.model_validate({
            "workflow_id": "wf-test", "user_id": "uid-ba", "name": "Nightly Triage",
            "author_email": "ba@example.com",
            "notifications": {
                "notify_on_failure": True,
                "support_emails": ["outside@gmail.com", "ops@example.com"],
            },
        })

        with patch("citra_cache.get_cache_manager", return_value=MagicMock()):
            from citra_workflow.executor import WorkflowExecutor
            executor = WorkflowExecutor()

        with patch("citra_workflow.notifications.send_execution_failure_notification",
                   new_callable=AsyncMock) as mock_notify:
            mock_notify.return_value = True
            await executor._notify_failure(
                execution, workflow,
                failed_node_label="Smart App Invoker", error_message="boom",
            )

            # Owner only — the injected outside-org address is never emailed.
            assert mock_notify.call_count == 1
            sent_to = {c.kwargs["to_email"] for c in mock_notify.call_args_list}
            assert sent_to == {"ba@example.com"}

    @pytest.mark.asyncio
    async def test_notify_failure_respects_notify_on_failure_off(self):
        """When notifications.notify_on_failure is False, no email is sent."""
        from citra_workflow.models import (
            WorkflowExecution, WorkflowDefinition, WorkflowNotifications,
        )

        execution = WorkflowExecution(
            execution_id="exec-muted", workflow_id="wf-test", user_id="uid-ba",
        )
        workflow = WorkflowDefinition(
            workflow_id="wf-test", user_id="uid-ba", name="Muted Workflow",
            author_email="ba@example.com",
            notifications=WorkflowNotifications(notify_on_failure=False),
        )

        with patch("citra_cache.get_cache_manager", return_value=MagicMock()):
            from citra_workflow.executor import WorkflowExecutor
            executor = WorkflowExecutor()

        with patch("citra_workflow.notifications.send_execution_failure_notification",
                   new_callable=AsyncMock) as mock_notify:
            await executor._notify_failure(
                execution, workflow,
                failed_node_label="Node X", error_message="Error",
            )
            mock_notify.assert_not_called()
