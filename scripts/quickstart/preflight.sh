# Copyright (c) 2026 Trustedwear Tech Private Limited (https://citra-ai.com)
# Author: Rohit Kumar Chandan
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may not
# use this file except in compliance with the License. You may obtain a copy of
# the License at http://www.apache.org/licenses/LICENSE-2.0

# Check the host can run this BEFORE anything is asked or written.
#
# Sourced, not executed -- `. scripts/quickstart/preflight.sh; preflight`.
#
# INSTALL.md says "Docker is the only prerequisite". Nothing checked even that.
# A host without it generated .env and then failed with a bare
# "docker: command not found", which is true and useless. In the wizard it is
# worse: it asks for a model endpoint and key first, so the answers were
# collected and thrown away.
#
# We check and instruct. We do NOT install -- Docker needs root, differs on
# every platform, and a setup script that quietly puts a daemon on someone's
# machine has done more than "run the setup script" grants it.

preflight() {
  local fail=0

  if ! command -v docker >/dev/null 2>&1; then
    echo "  [X] docker is not on PATH." >&2
    echo "      Install Docker Desktop (macOS/Windows) or Docker Engine (Linux):" >&2
    echo "        https://docs.docker.com/get-docker/" >&2
    echo "      On Windows, run this from Git Bash or WSL after Docker Desktop starts." >&2
    fail=1
  else
    # Installed-but-not-running is the most common state, and its failure
    # otherwise surfaces much later as a connection refused from inside compose.
    if ! docker info >/dev/null 2>&1; then
      echo "  [X] docker is installed but the daemon is not reachable." >&2
      echo "      Start Docker Desktop, or: sudo systemctl start docker" >&2
      echo "      \`docker version\` should print a Server section; if it does not, it is not running." >&2
      fail=1
    fi
    if ! docker compose version >/dev/null 2>&1; then
      echo "  [X] 'docker compose' (v2) is unavailable." >&2
      if command -v docker-compose >/dev/null 2>&1; then
        echo "      You have the old v1 'docker-compose'. This stack needs v2." >&2
      fi
      echo "        https://docs.docker.com/compose/install/" >&2
      fail=1
    fi
  fi

  # python3 is not needed to BRING THE STACK UP -- compose does that, and the
  # secret generator falls back to openssl or /dev/urandom. It is needed for
  # `python scripts/smoke_test.py`, which the README puts immediately after
  # `make wizard` as the way to confirm the install worked. Advisory, not fatal:
  # a stack that came up is still a stack that came up.
  if ! command -v python3 >/dev/null 2>&1 && ! command -v python >/dev/null 2>&1; then
    echo "  [!] python3 is not on PATH. The stack will still start, but" >&2
    echo "      \`python scripts/smoke_test.py\` -- the check that proves the install" >&2
    echo "      works end to end -- cannot run. Standard library only, nothing to pip install." >&2
  fi

  # Advisory. Milvus is the heavy one; a smaller box still installs, it just
  # swaps. Not worth blocking on, and not worth guessing wrong about either.
  local kb=""
  [ -r /proc/meminfo ] && kb=$(awk '/MemTotal/{print $2}' /proc/meminfo 2>/dev/null || true)
  if [ -n "$kb" ] && [ "$kb" -lt 15000000 ] 2>/dev/null; then
    echo "  [!] $((kb / 1024 / 1024)) GB RAM detected; 16 GB is the tested floor (the worker plus the data stores)." >&2
  fi

  if [ "$fail" -ne 0 ]; then
    echo "" >&2
    echo "  Prerequisites are not met -- stopping before anything is written." >&2
    echo "  See Requirements in README.md and INSTALL.md." >&2
    return 1
  fi
  return 0
}
