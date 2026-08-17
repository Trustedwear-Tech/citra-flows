#!/usr/bin/env bash
# Copyright (c) 2026 Trustedwear Tech Private Limited (https://citra-ai.com)
# Author: Rohit Kumar Chandan
# SPDX-License-Identifier: BUSL-1.1
#
# Licensed under the Business Source License 1.1. Non-production use is granted;
# production use requires a commercial licence until the Change Date, after
# which this file converts to Apache-2.0. See LICENSE at the repository root.

# Citra Flows — guided first-run setup.
#
# Creates .env with FRESH random secrets, then brings the stack up. The point of
# the fresh secrets is that .env.example ships a fixed JWT_SECRET: it is fine as
# a placeholder, but every install that copies it verbatim shares one signing
# key, and that key is readable by anyone with the repo. Anything reachable from
# the network must not start that way.
#
# Re-runnable: an existing .env is kept as-is, so running this again will not
# rotate secrets or discard values you set by hand.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
ENV_FILE="$REPO_ROOT/.env"
cd "$REPO_ROOT"

say()  { printf '\n\033[1m%s\033[0m\n' "$1"; }
ok()   { printf '  [ok] %s\n' "$1"; }
warn() { printf '  [!!] %s\n' "$1"; }

# A random secret. openssl where available, else python3, else /dev/urandom —
# no silent fallback to a weak value: if none of the three work, we stop.
rand() {
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -hex 32
  elif command -v python3 >/dev/null 2>&1; then
    python3 -c 'import secrets; print(secrets.token_hex(32))'
  elif [ -r /dev/urandom ]; then
    head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n'
  else
    echo "FATAL: no way to generate a random secret (need openssl, python3 or /dev/urandom)" >&2
    exit 1
  fi
}

# Replace KEY=... in .env, portably (BSD and GNU sed disagree about -i).
set_key() {
  local key="$1" val="$2" tmp
  tmp="$(mktemp)"
  awk -v k="$key" -v v="$val" '
    BEGIN { done = 0 }
    $0 ~ "^" k "=" { print k "=" v; done = 1; next }
    { print }
    END { if (!done) print k "=" v }
  ' "$ENV_FILE" > "$tmp"
  mv "$tmp" "$ENV_FILE"
}

say "Citra Flows setup"

# -- 1. .env ------------------------------------------------------------------
if [ -f "$ENV_FILE" ]; then
  ok "Found an existing .env — keeping it (values you set are preserved)."
else
  cp .env.example "$ENV_FILE"
  set_key JWT_SECRET       "$(rand)"
  set_key MONGODB_PASSWORD "$(rand)"
  # Short on purpose: it is typed into a login form, and it is printed below.
  set_key ADMIN_PASSWORD   "$(rand | cut -c1-12)"
  ok ".env created with freshly generated JWT_SECRET, MONGODB_PASSWORD and ADMIN_PASSWORD"
fi

# -- 2. model access ----------------------------------------------------------
# Flows calls an OpenAI-compatible endpoint. Without a key the stack still comes
# up; only the AI-assisted steps fail, and they fail loudly, so this is a prompt
# rather than a hard requirement.
current_key="$(grep -m1 '^LLM_API_KEY=' "$ENV_FILE" | cut -d= -f2- || true)"
if [ -z "${current_key}" ]; then
  say "Model access"
  echo "  Flows uses an OpenAI-compatible endpoint for its AI-assisted steps."
  echo "  Leave blank to skip — the stack runs, those steps will error until set."
  printf '  LLM_API_KEY: '
  read -r llm_key || llm_key=""
  if [ -n "$llm_key" ]; then
    set_key LLM_API_KEY "$llm_key"
    set_key LLM_SMALL_API_KEY "$llm_key"
    ok "model key stored"
  else
    warn "no model key set — AI-assisted steps will fail until LLM_API_KEY is filled in"
  fi
fi

# -- 3. bring it up -----------------------------------------------------------
# The user service is built out of the citra-common submodule; a clone made
# without --recurse-submodules has an empty directory there and the build
# fails with a missing Dockerfile. Cheap to fix automatically.
if [ ! -f citra-common/Citra-User-Service/package.json ]; then
  say "Fetching the citra-common submodule (bundled user service)"
  git submodule update --init
fi

say "Starting the stack"
echo "  Flows provisions its OWN Mongo, Redis, MinIO and user service"
echo "  (docker-compose.infra.yml + docker-compose.quickstart.yml)."
echo "  To run against an existing Citra-AI deployment's stores instead, stop here"
echo "  and use docker-compose.shared.yml — see its header."
# Not `make up`: make is usually absent on Windows, and this script must work
# everywhere the README sends people. This is exactly what `make up` runs.
docker compose -f docker-compose.quickstart.yml up -d --build --wait \
  citra-workflow citra-worker citra-flows-ui

say "Done"
echo "  UI:       http://localhost:${FLOWS_UI_PORT:-8088}"
echo "  Sign in:  $(grep -m1 '^ADMIN_EMAIL=' "$ENV_FILE" | cut -d= -f2-)  /  $(grep -m1 '^ADMIN_PASSWORD=' "$ENV_FILE" | cut -d= -f2-)"
echo "  Verify:   python scripts/smoke_test.py"
