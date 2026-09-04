<!--
  Copyright (c) 2026 Trustedwear Tech Private Limited (https://citra-ai.com)
  Author: Rohit Kumar Chandan
  SPDX-License-Identifier: Apache-2.0

  Licensed under the Apache License, Version 2.0 (the "License"); you may not
  use this file except in compliance with the License. You may obtain a copy of
  the License at http://www.apache.org/licenses/LICENSE-2.0
-->

# Install Citra Flows

Runs on a laptop. Clone it, or download a release — both give an identical,
self-contained tree, and the release needs no `git` at all:

```bash
curl -sSL https://github.com/Trustedwear-Tech/citra-flows/archive/refs/tags/v0.2.0.tar.gz | tar xz
cd citra-flows-0.2.0
```

Then, any OS:

```bash
cp .env.example .env
# REQUIRED: open .env and set ADMIN_EMAIL and ADMIN_PASSWORD — your sign-in
# credentials. There are no defaults; the stack refuses to start without them.
docker compose -f docker-compose.quickstart.yml up -d --build --wait citra-workflow citra-worker citra-flows-ui
```

(Or skip the hand-editing: `scripts/quickstart/wizard.sh` asks for them.)

| Need | Why |
|------|-----|
| **Docker Engine 24+** with **Compose v2** | builds and runs everything |
| **8 GB+ RAM** | the worker plus the data stores |
| **A model endpoint** | OpenRouter, OpenAI, DeepSeek, Ollama, or your own vLLM |
| **Internet on first run** | base images and `pip`/`npm` installs |
| **python3** *(optional)* | only for `scripts/smoke_test.py` — standard library, nothing to install |

You do **not** need Node.js (it runs inside the containers), and `git` only if
you clone — the release tarball is self-contained. `make wizard` and
`make install` check all of this first and name whatever is missing before
writing anything.

> `git submodule update --init` used to be the first command here.
> `citra-common` is vendored as ordinary files now, so there is nothing to
> initialise — and a downloaded release, which has no `.git` at all, could
> never have run it.

(PowerShell: `copy .env.example .env`)

That builds four images, starts nine containers, and returns once they are
healthy. First run takes 5–10 minutes — mostly `pip install`, `npm install`
and the Expo bundle; afterwards it is seconds.

Then verify it actually works:

```bash
python scripts/smoke_test.py
```

It signs in, authors a workflow, runs it, and asserts the run reaches
**completed**. Standard library only, so there is nothing to install first.

> **If you have `make`** (Linux, macOS, WSL) `make install` runs the first
> block and `make smoke` the second. It is a shorthand for the commands above,
> not a requirement — `make` is usually absent on Windows.

Open the UI:

| | |
|---|---|
| **UI** | http://localhost:8088 |
| **API docs** | http://localhost:9200/docs |
| **Sign in** | `ADMIN_EMAIL` / `ADMIN_PASSWORD` from your `.env` |

This engine stores no accounts. Sign-in is proxied to **Citra-User-Service**,
which owns users, passwords, orgs, departments and roles — and the quickstart
ships one, built from the vendored `citra-common` tree. The first account is seeded
on every `up` by the one-shot `citra-user-service-init` container from
`ADMIN_EMAIL` / `ADMIN_PASSWORD` in `.env` (idempotent — re-running resets the
password to the `.env` value). It is created as **`super_admin`** in the org
**`ADMIN_ORG_ID`** (default `local`) — every workflow, run and connection you
create is scoped to that org. The credentials are the ones YOU set — there are
no defaults, deliberately: a default credential is a credential every install
shares. The wizard prompts for both and prints them when it finishes; set by
hand, they are always readable with `grep ^ADMIN_ .env`. Changing them in
`.env` and re-running `up` re-seeds (that is also the password-recovery path).

There is no public sign-up. Create further accounts (org admins, members) from
the seeded admin with `create-admin.js` inside the user-service container:

```bash
docker compose -f docker-compose.quickstart.yml exec citra-user-service \
  node src/scripts/create-admin.js someone@your.org 'their-password' 'Their Name' --role=user --org=local
```

Pointing at an EXISTING Citra-User-Service instead? Set `USER_SERVICE_URL` to
it, and set `JWT_SECRET` to the **same value that service uses** — it issues
the token and this service verifies it, so a mismatch means login succeeds and
every subsequent request returns 401.

## No Docker?

See [Running from source](#running-from-source) below. You will need Python
3.11, Node 20, and your own MongoDB and two Redis instances.

---

## What gets started

| Service | Purpose |
|---|---|
| `citra-flows-ui` | The web app (nginx serving the Expo web build) |
| `citra-workflow` | The API — auth, CRUD, and enqueuing runs |
| `citra-worker` | **Executes runs** and owns the cron scheduler |
| `citra-user-service` + `-init` | Sign-in: owns accounts, issues tokens; `-init` seeds the first account |
| `mongodb` | Workflow definitions, run history, users |
| `redis` | Cache and the scheduler's leader-election lock |
| `queue-redis` | The durable job queue (Redis Streams) |
| `minio` + `minio-init` | S3-compatible store for file outputs |

**The worker is not optional.** The API only puts a job on the queue; nothing in
the API process consumes it. A stack without a healthy worker passes every
health check and leaves every run sitting in `queued`. `make smoke` exists
largely to catch exactly that.

Only the UI and API publish host ports. The data stores are reachable only on
the compose network, so this stack will not collide with a Mongo or Redis you
already run.

## Everyday commands

`make` on the left, the plain command on the right. Set
`C=docker compose -f docker-compose.quickstart.yml` and they are one-liners.

| | |
|---|---|
| `make ps` | `$C ps` |
| `make logs` | `$C logs -f citra-workflow citra-worker` |
| `make logs SERVICE=citra-worker` | `$C logs -f citra-worker` |
| `make restart` | `$C restart citra-workflow citra-worker citra-flows-ui` |
| `make down` | `$C down` — stop, keep data |
| `make destroy` | `$C down -v` — stop and delete all data |
| `make smoke` | `python scripts/smoke_test.py` |
| `make test` | `cd citra-workflow && python -m pytest tests/ -m "not integration" -q` |

## Configuration

All of it is `.env` (copied from `.env.example`). The values shipped there
are development-only and deliberately committed so a fresh clone runs with no
setup. **Change them before anyone else can reach this.**

### Connecting a model

AI features — the AI workflow author, and the LLM / agent / classifier /
summarizer nodes — need an OpenAI-compatible endpoint. Everything else (the
builder, sources, transforms, conditions, loops, outputs, scheduling, approvals)
works without one.

```bash
LLM_BASE_URL=https://openrouter.ai/api/v1
LLM_API_KEY=sk-...
LLM_MODEL=deepseek/deepseek-v4-pro
```

Point it at your own inference and nothing leaves your network:

```bash
# Ollama on the host, reached from inside the container
LLM_BASE_URL=http://host.docker.internal:11434/v1
LLM_API_KEY=ollama
LLM_MODEL=llama3.1
```

Leave `LLM_API_KEY` empty and nodes that need a model fail with a clear message
rather than silently doing nothing.

### Changing ports

```bash
FLOWS_UI_PORT=8088
FLOWS_API_PORT=9200
```

`FLOWS_API_PORT` is compiled **into the UI bundle** (the browser has to reach
the API directly), so after changing it:

```bash
docker compose -f docker-compose.quickstart.yml build
docker compose -f docker-compose.quickstart.yml up -d --wait citra-workflow citra-worker citra-flows-ui
```

(`make build && make up`) — a restart alone will not pick it up.

### Adding or changing an account

Accounts are not managed here — use Citra-User-Service. The block below is kept
only for the pre-delegation local store, which is no longer read:

```bash
docker compose -f docker-compose.quickstart.yml down -v
docker compose -f docker-compose.quickstart.yml up -d --wait citra-workflow citra-worker citra-flows-ui
```

(`make destroy && make up`) — this wipes all local data and re-bootstraps.

The email must look routable. Reserved domains — `.local`, `.test`, `.invalid`
— are rejected at sign-in, and an account created with one could never log in.

---

## Running from source

For working on the code itself. You need Python 3.11+, Node 20+, and running
MongoDB and two Redis instances.

```bash
# 1. Data stores only (published on the compose network; add ports: to reach
#    them from the host, or point .env at your own)
docker compose -f docker-compose.infra.yml up -d

# 2. API
cd citra-workflow
pip install -r requirements.txt
uvicorn citra_workflow.main:app --port 9200

# 3. Worker — in a second terminal. Runs stay queued without it.
cd Citra-Worker
pip install -r requirements.txt
python -m worker

# 4. UI — in a third
cd ui
npm install
npm run web
```

The shared libraries (`citra-mongo`, `citra-auth`, `citra-queue`,
`citra-cache`, `citra-llm`, `citra-service-utils`) live in the `citra-common`
vendored into this repository as ordinary files, so it builds with no extra step; the build will
fail with missing paths. They are installed as editable
sibling packages by those `requirements.txt` files, so an edit to one is picked
up without reinstalling.

Run the tests:

```bash
cd citra-workflow
python -m pytest tests/ -m "not integration" -q     # unit; no services needed
python -m pytest tests/ -m integration -q           # needs live backends;
                                                    # each skips if absent
```

`tests/test_vector_backends_live.py` documents the `docker run` line for each
vector database it exercises.

---

## Troubleshooting

**The smoke test says a run never left `queued`.**
The worker is not consuming the queue. Check it:
`docker compose -f docker-compose.quickstart.yml logs -f citra-worker`. Note
the worker's consumer group is created when it first starts, so a run submitted
while no worker existed is not replayed — submit a new one after it is up.

**Sign-in returns 503, or "the identity service is unreachable".**
`USER_SERVICE_URL` is unset or wrong. This engine proxies login to
Citra-User-Service; it cannot authenticate anyone on its own.

**Login succeeds, then every request 401s.**
`JWT_SECRET` differs between this service and Citra-User-Service. The token is
issued there and verified here, so both must match byte for byte. The login
endpoint checks this and returns 502 with that message rather than handing back
a token nothing will accept.

**The UI loads but every call fails, or the browser console shows CORS errors.**
The API allows exactly `http://localhost:$FLOWS_UI_PORT`. If you reach the UI
by any other name (a LAN IP, `127.0.0.1` rather than `localhost`), add it to
`CORS_ALLOWED_ORIGINS` and restart.

**A port is already taken.**
Change `FLOWS_UI_PORT` / `FLOWS_API_PORT` in `.env`, then rebuild (see
[Changing ports](#changing-ports) — the API port is baked into the UI bundle).

**A REST source node fails with "internal / private addresses are not allowed".**
That is the SSRF guard, working as intended. It refuses `localhost`, `127.x`
and private ranges so a workflow cannot be pointed at your internal network.

**Something is wedged.**
`docker compose -f docker-compose.quickstart.yml down -v` then re-run the two
install commands. That rebuilds from nothing and deletes all local data.
