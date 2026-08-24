# Copyright (c) 2026 Trustedwear Tech Private Limited (https://citra-ai.com)
# Author: Rohit Kumar Chandan
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may not
# use this file except in compliance with the License. You may obtain a copy of
# the License at http://www.apache.org/licenses/LICENSE-2.0

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
