# Copyright (c) 2026 Trustedwear Tech Private Limited (https://citra-ai.com)
# Author: Rohit Kumar Chandan
# SPDX-License-Identifier: BUSL-1.1
#
# Licensed under the Business Source License 1.1. Non-production use is granted;
# production use requires a commercial licence until the Change Date, after
# which this file converts to Apache-2.0. See LICENSE at the repository root.

"""Entry-point shim — `python main.py` matches the local-dev launch
convention used across other Citra services. Delegates to worker._main().
The canonical module entry point `python -m worker` still works."""
import asyncio

import worker

if __name__ == "__main__":
    try:
        asyncio.run(worker._main())
    except KeyboardInterrupt:
        pass
