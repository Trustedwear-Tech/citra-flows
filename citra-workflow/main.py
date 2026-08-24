# Copyright (c) 2026 Trustedwear Tech Private Limited (https://citra-ai.com)
# Author: Rohit Kumar Chandan
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may not
# use this file except in compliance with the License. You may obtain a copy of
# the License at http://www.apache.org/licenses/LICENSE-2.0

"""Entry-point shim — `python main.py` matches the local-dev launch
convention used across other Citra services. Boots the workflow service
on the configured port (default 9200)."""
import os

import uvicorn

if __name__ == "__main__":
    uvicorn.run(
        "citra_workflow.main:app",
        host=os.getenv("WORKFLOW_SERVICE_HOST", "0.0.0.0"),
        port=int(os.getenv("WORKFLOW_SERVICE_PORT", "9200")),
        reload=os.getenv("WORKFLOW_SERVICE_RELOAD", "false").lower() == "true",
        log_level=os.getenv("LOG_LEVEL", "info").lower(),
    )
