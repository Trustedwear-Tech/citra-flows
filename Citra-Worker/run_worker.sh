#!/usr/bin/env bash
# Copyright (c) 2026 Trustedwear Tech Private Limited (https://citra-ai.com)
# Author: Rohit Kumar Chandan
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may not
# use this file except in compliance with the License. You may obtain a copy of
# the License at http://www.apache.org/licenses/LICENSE-2.0

# Local dev launcher for Citra-Worker.
#
# Workers share a codebase with Citra-Service (the workflow engine
# defer-imports `services.*` from Citra-Service). This launcher
# prepends Citra-Service to PYTHONPATH so those imports resolve at
# runtime. In Docker we do the same in the Dockerfile.
# (Mongo lives in the `citra-mongo` shared package — installed via
# requirements.txt, no PYTHONPATH gymnastics needed.)
#
# Usage:
#   ./run_worker.sh
#   CITRA_WORKER_CONCURRENCY=8 ./run_worker.sh

set -euo pipefail

HERE="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
CITRA_SERVICE_DIR="$( cd "$HERE/../Citra-Service" && pwd )"

if [ ! -d "$CITRA_SERVICE_DIR" ]; then
  echo "ERR: cannot locate Citra-Service at $CITRA_SERVICE_DIR" >&2
  exit 1
fi

export PYTHONPATH="${CITRA_SERVICE_DIR}${PYTHONPATH:+:$PYTHONPATH}"
echo "→ PYTHONPATH includes $CITRA_SERVICE_DIR"

cd "$HERE"
exec python -m worker "$@"
