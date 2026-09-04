#!/usr/bin/env bash
# Copyright (c) 2026 Trustedwear Tech Private Limited (https://citra-ai.com)
# Author: Rohit Kumar Chandan
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may not
# use this file except in compliance with the License. You may obtain a copy of
# the License at http://www.apache.org/licenses/LICENSE-2.0

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

# Before the first question and before .env is written. INSTALL.md claims
# "Docker is the only prerequisite"; nothing verified even that one.
. "$REPO_ROOT/scripts/quickstart/preflight.sh"
preflight || exit 1

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
  ok ".env created with freshly generated JWT_SECRET and MONGODB_PASSWORD"
fi

# -- first account: REQUIRED, and yours ---------------------------------------
# There are NO default credentials, deliberately: a default is a credential
# every install on the internet shares. Runs whenever either value is missing
# (fresh .env, or an older one), so the wizard never starts a stack that the
# init container would refuse anyway. A non-interactive run must have set
# both in .env beforehand — EOF here is a hard stop, not a silent default.
cur_email="$(grep -m1 '^ADMIN_EMAIL=' "$ENV_FILE" | cut -d= -f2- | tr -d '\r')"
cur_pw="$(grep -m1 '^ADMIN_PASSWORD=' "$ENV_FILE" | cut -d= -f2- | tr -d '\r')"
if [ -z "$cur_email" ] || [ -z "$cur_pw" ]; then
  say "First account (seeded as super_admin) — required, no defaults"
  while [ -z "$cur_email" ]; do
    printf '  Admin email (your sign-in id, shaped like x@y.z): '
    if ! read -r cur_email; then
      echo "" >&2
      echo "  [FAIL] no input available — set ADMIN_EMAIL and ADMIN_PASSWORD in .env and re-run." >&2
      exit 1
    fi
    case "$cur_email" in
      *@*.*) ;;
      *) [ -n "$cur_email" ] && echo "  [!!] not an email address"; cur_email="" ;;
    esac
  done
  set_key ADMIN_EMAIL "$cur_email"
  while [ -z "$cur_pw" ]; do
    printf '  Admin password (min 8 characters): '
    if ! read -r cur_pw; then
      echo "" >&2
      echo "  [FAIL] no input available — set ADMIN_PASSWORD in .env and re-run." >&2
      exit 1
    fi
    if [ "${#cur_pw}" -lt 8 ]; then
      [ -n "$cur_pw" ] && echo "  [!!] too short — 8 characters minimum"
      cur_pw=""
    fi
  done
  set_key ADMIN_PASSWORD "$cur_pw"
  echo "  Every workflow, run and connection is scoped to an org id."
  printf '  Org id [local]: '
  read -r admin_org || admin_org=""
  [ -n "$admin_org" ] && set_key ADMIN_ORG_ID "$admin_org"
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

# A key alone is not a working configuration: the assistant resolves a model per
# tier and errors out when the name is empty, so an install with a key and no
# model fails on the first thing anyone does with it.
#
# Checked OUTSIDE the key prompt above, because that prompt only runs when no
# key is present. Anyone arriving with a key already set -- a re-run, a copied
# .env, a hand-edited file -- skipped the block entirely and never got a model,
# which is exactly the install that then fails.
#
# Only filled when BLANK. A value already there -- shipped in .env.example, or
# one you chose yourself -- is used as it stands and never overwritten.
current_model="$(grep -m1 '^LLM_MODEL=' "$ENV_FILE" | cut -d= -f2- || true)"
if [ -z "${current_model}" ]; then
  set_key LLM_MODEL "deepseek/deepseek-v4-pro:nitro"
  set_key LLM_SMALL_MODEL "deepseek/deepseek-v4-pro:nitro"
  ok "model set to deepseek/deepseek-v4-pro:nitro"
else
  ok "model: ${current_model} (kept)"
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

# Every value from .env, not the shell environment: FLOWS_UI_PORT is set in
# the file, so $FLOWS_UI_PORT here would print the default even after the
# user changed the port.
envval() { grep -m1 "^$1=" "$ENV_FILE" | cut -d= -f2- | tr -d '\r'; }
ui_port="$(envval FLOWS_UI_PORT)";  ui_port="${ui_port:-8088}"
api_port="$(envval FLOWS_API_PORT)"; api_port="${api_port:-9200}"
org_id="$(envval ADMIN_ORG_ID)";    org_id="${org_id:-local}"

say "Done — Citra Flows is running"
echo "  UI:        http://localhost:${ui_port}"
echo "  API docs:  http://localhost:${api_port}/docs"
echo ""
echo "  Sign in:   $(envval ADMIN_EMAIL)  /  $(envval ADMIN_PASSWORD)"
echo "             seeded as super_admin in org '${org_id}' — every workflow,"
echo "             run and connection you create is scoped to that org."
echo "             Credentials are the ones you chose — they live in .env"
echo "             (grep ^ADMIN_ .env)."
echo ""
echo "  No public sign-up: add teammates from this account — see INSTALL.md,"
echo "  section 'Sign in'."
echo ""
echo "  Verify:    python scripts/smoke_test.py"
