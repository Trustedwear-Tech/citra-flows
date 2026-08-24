# Copyright (c) 2026 Trustedwear Tech Private Limited (https://citra-ai.com)
# Author: Rohit Kumar Chandan
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may not
# use this file except in compliance with the License. You may obtain a copy of
# the License at http://www.apache.org/licenses/LICENSE-2.0

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
