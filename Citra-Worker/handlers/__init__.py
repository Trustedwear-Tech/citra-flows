# Copyright (c) 2026 Trustedwear Tech Private Limited (https://citra-ai.com)
# Author: Rohit Kumar Chandan
# SPDX-License-Identifier: BUSL-1.1
#
# Licensed under the Business Source License 1.1. Non-production use is granted;
# production use requires a commercial licence until the Change Date, after
# which this file converts to Apache-2.0. See LICENSE at the repository root.

"""
Handler modules. Importing this package side-imports every module so
the @register decorators run and populate the registry.

Add a new handler:
  1. Drop a file in this directory: e.g. `report_handlers.py`
  2. Inside, decorate async functions with @register("name").
  3. Import the module here: `from . import report_handlers`
"""
from . import workflow_handlers  # noqa: F401  — side-import for registration
