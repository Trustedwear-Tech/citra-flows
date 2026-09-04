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
#   scripts/quickstart/wizard.sh            install, or resume/repair a stack
#   scripts/quickstart/wizard.sh --fresh    full cleanup, then set up from nothing
#   scripts/quickstart/wizard.sh --help     what each mode does
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

usage() {
  cat <<'EOF'
Citra Flows — guided setup wizard.

Usage:  scripts/quickstart/wizard.sh [--fresh] [-h|--help]

Without options — install, or RESUME:
  Idempotent, safe to re-run any time (after a reboot, a .env edit, or a
  failed first attempt). An existing .env is kept exactly as-is — secrets,
  keys and credentials you set are preserved — and you are prompted only
  for required values that are still missing. Containers are started or
  updated in place; the data in the Mongo/Redis/MinIO volumes survives.

--fresh — full cleanup, then set up from nothing:
  Stops the stack and DELETES its volumes — every workflow, run, account
  and file output in this install. .env is moved aside to
  .env.bak.<timestamp> (never deleted), and the normal setup then runs,
  asking everything again. Asks for confirmation before touching anything.

-h, --help — this text.
EOF
}

FRESH=0
for arg in "$@"; do
  case "$arg" in
    -h|--help) usage; exit 0 ;;
    --fresh)   FRESH=1 ;;
    *) echo "unknown option: $arg" >&2; echo "" >&2; usage >&2; exit 2 ;;
  esac
done

# Before the first question and before .env is written. INSTALL.md claims
# "Docker is the only prerequisite"; nothing verified even that one.
. "$REPO_ROOT/scripts/quickstart/preflight.sh"
preflight || exit 1

# Colours: green = success, red = failure, yellow = caution. Only on a
# terminal — piped/CI output and NO_COLOR stay plain text.
if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
  C_G=$'\033[32m'; C_R=$'\033[31m'; C_Y=$'\033[33m'; C_B=$'\033[1m'; C_0=$'\033[0m'
else
  C_G=""; C_R=""; C_Y=""; C_B=""; C_0=""
fi
# What this run actually did — printed in the final summary.
RUN_SUMMARY=""
did() { RUN_SUMMARY="${RUN_SUMMARY}    - $1\n"; }
say()  { printf '\n%s%s%s\n' "$C_B" "$1" "$C_0"; }
ok()   { printf '  %s[ok]%s %s\n' "$C_G" "$C_0" "$1"; }
warn() { printf '  %s[!!]%s %s\n' "$C_Y" "$C_0" "$1"; }
# One '*' per character. Shown wherever a secret was entered, so the user can
# tell that a paste landed — and, via the length, that it landed exactly once.
mask() { printf '%*s' "${#1}" '' | tr ' ' '*'; }

# --- Checkpoints -------------------------------------------------------------
# Every completed step is appended to .wizard-state.log (gitignored), and a
# failing run records the step it died in — so the next run can say exactly
# where the last one got to. The log is a RECORD, not an authority: the
# progress report re-verifies every step against .env and Docker, because a
# log that outlives a manual `docker compose down -v` would otherwise lie.
STATE_FILE="$REPO_ROOT/.wizard-state.log"
CURRENT_STEP="preflight"
ckpt() { printf '%s  done: %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$1" >> "$STATE_FILE"; }
trap 'rc=$?; if [ "$rc" -ne 0 ]; then
        word="FAILED"; case "$rc" in 130|143) word="INTERRUPTED";; esac
        printf "%s  %s during: %s (exit %s)\n" "$(date '\''+%Y-%m-%d %H:%M:%S'\'')" "$word" "$CURRENT_STEP" "$rc" >> "$STATE_FILE"
        echo "" >&2
        echo "  ${C_R}[!!] $word during: $CURRENT_STEP.${C_0} Completed steps are kept —" >&2
        echo "       just re-run the wizard; it resumes from here." >&2
      fi' EXIT
# Ctrl-C / kill: without these, bash skips the EXIT trap on a fatal signal
# and the interruption would never reach the state log.
trap 'exit 130' INT
trap 'exit 143' TERM

stack_running() {
  docker compose -f docker-compose.quickstart.yml ps -q citra-workflow 2>/dev/null | grep -q .
}
progress_report() {
  local s_env="pending" s_adm="pending" s_mod="pending" s_stk="pending"
  [ -f "$ENV_FILE" ] && s_env="done   "
  if [ -f "$ENV_FILE" ] \
     && [ -n "$(grep -m1 '^ADMIN_EMAIL=' "$ENV_FILE" | cut -d= -f2- | tr -d '\r')" ] \
     && [ -n "$(grep -m1 '^ADMIN_PASSWORD=' "$ENV_FILE" | cut -d= -f2- | tr -d '\r')" ]; then
    s_adm="done   "
  fi
  if [ -f "$ENV_FILE" ] && [ -n "$(grep -m1 '^LLM_MODEL=' "$ENV_FILE" | cut -d= -f2- | tr -d '\r')" ]; then
    s_mod="done   "
  fi
  stack_running && s_stk="running"
  echo ""
  echo "  Progress — verified against .env and Docker, not just the log:"
  echo "    [${s_env}] .env with generated secrets"
  echo "    [${s_adm}] admin credentials (ADMIN_EMAIL / ADMIN_PASSWORD)"
  echo "    [${s_mod}] model configuration"
  echo "    [${s_stk}] stack containers"
  if [ -f "$STATE_FILE" ]; then
    echo "    log: .wizard-state.log — last entry:"
    tail -1 "$STATE_FILE" | sed 's/^/      /'
    case "$(tail -1 "$STATE_FILE")" in
      *FAILED*|*INTERRUPTED*) echo "    Resuming from that step now." ;;
    esac
  fi
  echo "  'pending' runs now; 'done' is kept as it is. The stack step always"
  echo "  reconciles (fast when nothing changed), so a config edit is applied."
}

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

# -- 0. --fresh: cleanup before anything else ---------------------------------
if [ "$FRESH" = 1 ]; then
  say "Fresh setup — full cleanup first"
  echo "  This STOPS the stack and DELETES its volumes: every workflow, run,"
  echo "  account and file output in this install is gone for good."
  echo "  .env is moved aside to .env.bak.<timestamp>, not deleted."
  printf '  Continue? [y/N]: '
  read -r ans || ans=""
  case "$ans" in
    y|Y|yes|YES) ;;
    # A declined confirmation is a decision, not a failure — disarm the
    # failure trap so the state log does not record it as one.
    *) trap - EXIT; echo "  Aborted — nothing was touched."; exit 1 ;;
  esac
  CURRENT_STEP="fresh cleanup (down -v)"
  docker compose -f docker-compose.quickstart.yml down -v --remove-orphans
  ok "stack stopped, volumes removed"
  if [ -f "$ENV_FILE" ]; then
    bak="$ENV_FILE.bak.$(date +%Y%m%d-%H%M%S)"
    mv "$ENV_FILE" "$bak"
    ok "old .env moved to ${bak##*/} (restore it with: mv ${bak##*/} .env)"
  fi
  # The old log describes the install that was just deleted; archive it with
  # the .env so the new log starts at zero and cannot claim finished steps.
  [ -f "$STATE_FILE" ] && mv "$STATE_FILE" "$STATE_FILE.bak.$(date +%Y%m%d-%H%M%S)"
  ckpt "fresh cleanup — volumes deleted, previous .env and state log archived"
  did "full cleanup: stack + volumes deleted, previous .env archived"
fi

progress_report

# -- 1. .env ------------------------------------------------------------------
CURRENT_STEP=".env creation"
if [ -f "$ENV_FILE" ]; then
  ok "Found an existing .env — keeping it (values you set are preserved)."
  did ".env kept — your existing values preserved"
else
  cp .env.example "$ENV_FILE"
  set_key JWT_SECRET       "$(rand)"
  set_key MONGODB_PASSWORD "$(rand)"
  ok ".env created with freshly generated JWT_SECRET and MONGODB_PASSWORD"
  ckpt ".env created with fresh secrets"
  did ".env created with freshly generated secrets"
fi

# -- first account: REQUIRED, and yours ---------------------------------------
# There are NO default credentials, deliberately: a default is a credential
# every install on the internet shares. Runs whenever either value is missing
# (fresh .env, or an older one), so the wizard never starts a stack that the
# init container would refuse anyway. A non-interactive run must have set
# both in .env beforehand — EOF here is a hard stop, not a silent default.
CURRENT_STEP="admin credentials"
cur_email="$(grep -m1 '^ADMIN_EMAIL=' "$ENV_FILE" | cut -d= -f2- | tr -d '\r' || true)"
cur_pw="$(grep -m1 '^ADMIN_PASSWORD=' "$ENV_FILE" | cut -d= -f2- | tr -d '\r' || true)"
if [ -z "$cur_email" ] || [ -z "$cur_pw" ]; then
  say "First account (seeded as super_admin) — required, no defaults"
  while [ -z "$cur_email" ]; do
    printf '  Admin email (your sign-in id, shaped like x@y.z): '
    if ! read -r cur_email; then
      echo "" >&2
      echo "  ${C_R}[FAIL]${C_0} no input available — set ADMIN_EMAIL and ADMIN_PASSWORD in .env and re-run." >&2
      exit 1
    fi
    case "$cur_email" in
      *@*.*) ;;
      *) [ -n "$cur_email" ] && echo "  ${C_Y}[!!]${C_0} not an email address"; cur_email="" ;;
    esac
  done
  set_key ADMIN_EMAIL "$cur_email"
  while [ -z "$cur_pw" ]; do
    # -s: nothing echoes while typing or pasting. The masked line printed
    # after is how you verify a paste landed, and landed exactly once.
    printf '  Admin password (min 8 characters; input hidden): '
    if ! read -rs cur_pw; then
      echo "" >&2
      echo "  ${C_R}[FAIL]${C_0} no input available — set ADMIN_PASSWORD in .env and re-run." >&2
      exit 1
    fi
    echo ""
    if [ "${#cur_pw}" -lt 8 ]; then
      [ -n "$cur_pw" ] && echo "  ${C_Y}[!!]${C_0} too short — 8 characters minimum (got ${#cur_pw})"
      cur_pw=""
    fi
  done
  ok "password captured: $(mask "$cur_pw")  (${#cur_pw} characters)"
  set_key ADMIN_PASSWORD "$cur_pw"
  echo "  Every workflow, run and connection is scoped to an org id."
  printf '  Org id [local]: '
  read -r admin_org || admin_org=""
  [ -n "$admin_org" ] && set_key ADMIN_ORG_ID "$admin_org"
  ckpt "admin credentials set (${cur_email})"
  did "admin credentials captured for ${cur_email}"
fi

# -- 2. model access ----------------------------------------------------------
CURRENT_STEP="model configuration"
# Flows calls an OpenAI-compatible endpoint. Without a key the stack still comes
# up; only the AI-assisted steps fail, and they fail loudly, so this is a prompt
# rather than a hard requirement.
current_key="$(grep -m1 '^LLM_API_KEY=' "$ENV_FILE" | cut -d= -f2- || true)"
if [ -z "${current_key}" ]; then
  say "Model access"
  echo "  Flows uses an OpenAI-compatible endpoint for its AI-assisted steps."
  echo ""
  echo "  No key yet? Create a free account at https://openrouter.ai and make"
  echo "  a new key at https://openrouter.ai/keys. The account and the key"
  echo "  cost nothing; usage is pay-as-you-go — light usage costs little to"
  echo "  nothing (rate-limited free-tier models exist), heavier usage needs"
  echo "  a small credit balance bought up front."
  echo ""
  echo "  Leave blank to skip — the stack runs, those steps will error until set."
  printf '  LLM_API_KEY (input hidden; Enter to skip): '
  read -rs llm_key || llm_key=""
  echo ""
  if [ -n "$llm_key" ]; then
    set_key LLM_API_KEY "$llm_key"
    set_key LLM_SMALL_API_KEY "$llm_key"
    ok "model key stored: $(mask "$llm_key")  (${#llm_key} characters)"
    ckpt "model key stored"
    did "model API key stored"
  else
    warn "no model key set — AI-assisted steps will fail until LLM_API_KEY is filled in"
    did "model key skipped — AI-assisted steps will error until LLM_API_KEY is set"
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
  ckpt "model configured"
  did "model set to the quick-start default"
else
  ok "model: ${current_model} (kept)"
fi

# -- 3. bring it up -----------------------------------------------------------
CURRENT_STEP="tree completeness check"
# citra-common is vendored as ordinary tracked files (the submodule is gone),
# so it is present in every clone AND every release tarball — a tarball has no
# .git, so the old `git submodule update --init` repair could never have run
# there anyway. If it is missing, the tree itself is broken: say so, rather
# than let the image build die later on a missing Dockerfile.
if [ ! -f citra-common/Citra-User-Service/package.json ]; then
  echo "  ${C_R}[FAIL]${C_0} citra-common/Citra-User-Service is missing — this tree is incomplete." >&2
  echo "         Re-clone the repository, or re-download the release tarball." >&2
  exit 1
fi

say "Starting the stack"
echo "  Flows provisions its OWN Mongo, Redis, MinIO and user service"
echo "  (docker-compose.infra.yml + docker-compose.quickstart.yml)."
echo "  To run against an existing Citra-AI deployment's stores instead, stop here"
echo "  and use docker-compose.shared.yml — see its header."
# Not `make up`: make is usually absent on Windows, and this script must work
# everywhere the README sends people. This is exactly what `make up` runs.
CURRENT_STEP="stack up (docker compose up --build --wait)"
docker compose -f docker-compose.quickstart.yml up -d --build --wait \
  citra-workflow citra-worker citra-flows-ui
ckpt "stack up — services healthy"
did "stack built and started — all services healthy"
CURRENT_STEP="done"

# Every value from .env, not the shell environment: FLOWS_UI_PORT is set in
# the file, so $FLOWS_UI_PORT here would print the default even after the
# user changed the port.
envval() { grep -m1 "^$1=" "$ENV_FILE" | cut -d= -f2- | tr -d '\r' || true; }
ui_port="$(envval FLOWS_UI_PORT)";  ui_port="${ui_port:-8088}"
api_port="$(envval FLOWS_API_PORT)"; api_port="${api_port:-9200}"
org_id="$(envval ADMIN_ORG_ID)";    org_id="${org_id:-local}"

say "${C_G}Done — Citra Flows is running${C_0}"
echo ""
echo "  What happened in this run:"
printf '%b' "${RUN_SUMMARY:-    - nothing to change — everything was already in place\n}"
echo ""
echo "  ${C_G}Open the UI:${C_0}   http://localhost:${ui_port}"
echo "  API docs:      http://localhost:${api_port}/docs"
echo ""
admin_pw="$(envval ADMIN_PASSWORD)"
echo "  Sign in:   $(envval ADMIN_EMAIL)  /  $(mask "$admin_pw") (${#admin_pw} characters)"
echo "             seeded as super_admin in org '${org_id}' — every workflow,"
echo "             run and connection you create is scoped to that org."
echo "             The password is the one you chose; it is never printed."
echo "             Both live in .env (grep ^ADMIN_ .env)."
echo ""
echo "  What next:"
echo "    1. Verify end to end:  python scripts/smoke_test.py"
echo "       (signs in, authors a workflow, runs it, asserts it completed)"
echo "    2. Open the UI, sign in, and hit 'New Workflow' — the AI panel"
echo "       drafts your first pipeline from a plain-English description."
echo "    3. Add teammates from your account (no public sign-up) —"
echo "       INSTALL.md, section 'Sign in'."
echo "    4. Re-run this wizard any time to resume or change values;"
echo "       --fresh starts over, --help explains both."
ckpt "done — install complete"
