# Copyright (c) 2026 Trustedwear Tech Private Limited (https://citra-ai.com)
# Author: Rohit Kumar Chandan
# SPDX-License-Identifier: BUSL-1.1
#
# Licensed under the Business Source License 1.1. Non-production use is granted;
# production use requires a commercial licence until the Change Date, after
# which this file converts to Apache-2.0. See LICENSE at the repository root.

"""
Citra Workflow Engine
=====================
Visual agent workflows. Imported by:

* **Citra-Service** — for the HTTP router (CRUD + trigger) and shared
  data models. Does NOT execute workflows in-process anymore; it
  enqueues to Citra-Worker.
* **Citra-Worker** — for the executor + scheduler. Owns runtime
  execution and cron/interval scheduling.

Public re-exports below are stable; import paths into the submodules
themselves (e.g. ``citra_workflow.nodes.agents``) are also part of the
contract because tests reach into them.
"""
from .models import (  # noqa: F401
    WorkflowDefinition,
)

__all__ = [
    "WorkflowDefinition",
]
