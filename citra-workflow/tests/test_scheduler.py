# Copyright (c) 2026 Trustedwear Tech Private Limited (https://citra-ai.com)
# Author: Rohit Kumar Chandan
# SPDX-License-Identifier: BUSL-1.1
#
# Licensed under the Business Source License 1.1. Non-production use is granted;
# production use requires a commercial licence until the Change Date, after
# which this file converts to Apache-2.0. See LICENSE at the repository root.

"""
Unit tests for WorkflowSchedulerManager — register, unregister, approval timeouts.
APScheduler is mocked throughout.
"""

import sys
import os
import pytest
from unittest.mock import patch, MagicMock, AsyncMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from citra_workflow.scheduler import WorkflowSchedulerManager


class TestSchedulerManager:

    def test_register_workflow(self):
        mgr = WorkflowSchedulerManager()
        mgr._scheduler = MagicMock()
        mgr._started = True

        with patch("apscheduler.triggers.cron.CronTrigger"):
            mgr.register_workflow("w1", "u1", {
                "cron_expression": "0 9 * * 1",
                "timezone": "UTC",
            })

        assert "w1" in mgr._jobs
        mgr._scheduler.add_job.assert_called_once()

    def test_register_invalid_cron(self):
        mgr = WorkflowSchedulerManager()
        mgr._scheduler = MagicMock()
        mgr._started = True

        # Only 3 parts instead of 5 — should log error and return
        mgr.register_workflow("w1", "u1", {"cron_expression": "0 9 *"})
        assert "w1" not in mgr._jobs
        mgr._scheduler.add_job.assert_not_called()

    def test_register_no_cron(self):
        """No cron expression → no job added."""
        mgr = WorkflowSchedulerManager()
        mgr._scheduler = MagicMock()
        mgr._started = True

        mgr.register_workflow("w1", "u1", {"cron_expression": None})
        assert "w1" not in mgr._jobs

    def test_unregister_workflow(self):
        mgr = WorkflowSchedulerManager()
        mgr._scheduler = MagicMock()
        mgr._jobs["w1"] = "wf_cron_w1"

        mgr.unregister_workflow("w1")
        assert "w1" not in mgr._jobs
        mgr._scheduler.remove_job.assert_called_once_with("wf_cron_w1")

    def test_unregister_nonexistent(self):
        mgr = WorkflowSchedulerManager()
        mgr._scheduler = MagicMock()
        # Should not raise
        mgr.unregister_workflow("doesnt_exist")
        mgr._scheduler.remove_job.assert_not_called()

    def test_shutdown(self):
        mgr = WorkflowSchedulerManager()
        mgr._scheduler = MagicMock()
        mgr._started = True

        mgr.shutdown()
        mgr._scheduler.shutdown.assert_called_once_with(wait=False)
        assert mgr._started is False

    def test_register_without_scheduler_warns(self):
        mgr = WorkflowSchedulerManager()
        mgr._scheduler = None
        # Should not raise
        mgr.register_workflow("w1", "u1", {"cron_expression": "0 9 * * 1"})
        assert "w1" not in mgr._jobs
