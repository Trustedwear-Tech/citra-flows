#!/usr/bin/env bash
# Copyright (c) 2026 Trustedwear Tech Private Limited (https://citra-ai.com)
# Author: Rohit Kumar Chandan
# SPDX-License-Identifier: BUSL-1.1
#
# Licensed under the Business Source License 1.1. Non-production use is granted;
# production use requires a commercial licence until the Change Date, after
# which this file converts to Apache-2.0. See LICENSE at the repository root.

# Create .env from .env.example with FRESH secrets.
#
# Split out of wizard.sh so `make install` gets the same treatment: copying
# .env.example verbatim would leave every install sharing the fixed JWT_SECRET
# that ships in it, and that value is public. Non-interactive by design — the
# wizard prompts, this does not.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"; cd "$ROOT"
[ -f .env ] && { echo "Kept existing .env."; exit 0; }

rand() {
  if command -v openssl >/dev/null 2>&1; then openssl rand -hex 32
  elif command -v python3 >/dev/null 2>&1; then python3 -c 'import secrets;print(secrets.token_hex(32))'
  elif [ -r /dev/urandom ]; then head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n'
  else echo "FATAL: need openssl, python3 or /dev/urandom to generate secrets" >&2; exit 1; fi
}
set_key() {
  local k="$1" v="$2" t; t="$(mktemp)"
  awk -v k="$k" -v v="$v" 'BEGIN{d=0} $0 ~ "^" k "=" {print k "=" v; d=1; next} {print} END{if(!d) print k "=" v}' .env > "$t"
  mv "$t" .env
}
cp .env.example .env
set_key JWT_SECRET "$(rand)"
set_key MONGODB_PASSWORD "$(rand)"
echo "Created .env from .env.example with freshly generated JWT_SECRET and MONGODB_PASSWORD."
echo "Set LLM_API_KEY before using the AI-assisted steps (or run: make wizard)."
