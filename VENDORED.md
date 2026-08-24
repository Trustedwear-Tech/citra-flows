<!--
  Copyright (c) 2026 Trustedwear Tech Private Limited (https://citra-ai.com)
  Author: Rohit Kumar Chandan
  SPDX-License-Identifier: Apache-2.0

  Licensed under the Apache License, Version 2.0 (the "License"); you may not
  use this file except in compliance with the License. You may obtain a copy of
  the License at http://www.apache.org/licenses/LICENSE-2.0
-->

# Vendored code

`citra-flows` is **independent**: it shares no repository with the other
Citra products and takes no dependency on a running Citra Decision System.

Independence is bought with duplication. These come from the Citra platform and
are vendored here rather than imported from a shared package:

| Path | Origin | Why |
|---|---|---|
| `citra-common/` (submodule) | Citra shared packages — `citra-auth`, `citra-mongo`, `citra-cache`, `citra-queue`, `citra-llm`, `citra-service-utils` | **No longer vendored.** They live in the `citra-common` repo and arrive as a pinned submodule, so both products share one copy instead of drifting. Each still installs on its own and pulls only what it needs. Citra-AI is the source of truth for their contents. |
| `bucket.py` | `Citra-Service/bucket.py` | Object storage (S3 / MinIO). Workflow output nodes defer-import `get_client` / `upload_file`. |
| `services/code_executor.py` `services/sandbox_file_cache.py` `services/sandbox_pool.py` | `Citra-Service/services/` | The `code_block` node's Docker sandbox. |
| `sandbox/Dockerfile` | `Citra-Service/Dockerfile.quick-chat-sandbox` | Image for the above. Named for a feature that no longer exists; rename freely. |

**There is no merge path back.** Upstream fixes will not arrive automatically.
That is the accepted cost of independence, not an oversight.
