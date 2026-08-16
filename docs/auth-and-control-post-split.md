# Auth, roles and automation control — where citra-flows actually stands post-split

**Question this answers.** Flows was built inside Citra-AI with two IT roles and
org-owned workflows. It is now a standalone product. Does login still work on its
own? Do the roles still mean what they meant? And did the Workflow Automation
Control card come across with it?

Short answers: **login is fully standalone and owes Citra-AI nothing.** The two
roles survived intact and are enforced. **The Automation Control card did not
come across** — the backend and the API client are both there, the screen is not.
And there is one operational gap bigger than any of it: **you cannot create a
second user.**

Everything below was read in the code and then confirmed against the running
service on 2026-08-14.

---

## 1. Login — standalone, no tie to Citra-AI

| | |
|---|---|
| Endpoints | **exactly two**: `POST /api/auth/login`, `GET /api/auth/me` (`auth_routes.py:227,252`) |
| Token | HS256 over a local `JWT_SECRET`, no default — the service refuses to mint without it (`auth_routes.py:79-89`) |
| TTL | 12 hours (`WORKFLOW_TOKEN_TTL_SECONDS`, `auth_routes.py:56`) |
| Claims | `user_id, email, org_id, dept_ids, roles, name, iat, jti, exp` |
| Users | Mongo `workflow_users`, bcrypt password hashes, unique index on email |
| First user | `ensure_bootstrap_user()` at startup, gated on `WORKFLOW_BOOTSTRAP_EMAIL` + `_PASSWORD`; no-ops if any user exists (`auth_routes.py:174-202`) |

**No dependency on Citra-AI for authentication.** No RS256, no JWKS, no IdP, no
user-service call, no shared service key. The split is clean on this axis.

Two stale strings remain and are harmless because nothing verifies them:
`JWT_ISSUER` still defaults to `"Citra-AI"` (`middleware.py:372`), and
`citra-auth/README.md:42` still says the secret "must match what user-service
signs with". Neither affects behaviour — the issuer is never checked.

One live tie does remain, but it is not auth: **all outbound email goes to
Citra-AI's user-service** at `USER_SERVICE_URL` (`notifications.py:227,237`,
`nodes/outputs.py:494,545`), with no SMTP fallback. Failure alerts and the email
output node stop working the moment flows is deployed away from Citra-AI.

### The gap that matters most: there is no way to add a user

`create_user()` exists (`auth_routes.py:123`) and is **not routed**. Its only
caller is the bootstrap function. There is no endpoint, no CLI script, and no
admin screen to create a user, change a password, disable an account, or assign
a role.

So today, one bootstrap account exists and **every additional person has to be
inserted into Mongo by hand.** The module's own docstring
(`auth_routes.py:21-22`) says further accounts are "created by an admin through
`create_user`" — describing a surface that was never built or did not survive
the split. For a product whose entire authorization model is *which roles is
this person assigned*, having no way to assign them is the blocking gap.

---

## 2. Roles — the two-role model survived, and is enforced

Access requires **either** a workflow role **or** IT-department admin:

```
_has_workflow_access(claims):            # router.py:286-294
    roles ∩ {super_admin, org_admin, IT-workflow}   →  allowed
    OR  dept_admin  AND  "it" ∈ dept_ids            →  allowed
```

`IT_DEPT_ID` comes from `WORKFLOW_IT_DEPT_ID`, defaulting to `"it"`
(`router.py:283`). **That variable is in neither `.env` nor `.env.example`**, so
the IT department slug is `"it"` by silent default — worth pinning explicitly.

Verified against the running API:

| Identity | `GET /api/workflows` |
|---|---|
| `roles=["user"]` | **403** |
| `roles=["dept_admin"], dept_ids=["it"]` | **200** |
| `roles=["dept_admin"], dept_ids=["hr"]` | **403** |

So "IT-workflow" and "IT admin" still mean what they meant.

### Workflows belong to the org — and only to the org

`owner_type`/`owner_id` in a create request are **ignored**; every workflow is
stamped `org` + the caller's org (`router.py:2599-2604`). Org scoping is applied
on every list and per-document path.

**But there is no per-user assignment, and this is the part that differs from
what you may expect.** `_check_workflow_action(workflow, request, action)`
(`router.py:377-404`) takes an `action` of `read`, `run` or `edit` — and **never
reads it**. The rule for all three is identical:

> hold any workflow-access role **and** be in the workflow's org.

Consequences, all deliberate-looking but worth confirming:

- Any IT-role user in an org can **edit, execute, deploy, roll back and delete
  every workflow in that org**, including ones they did not author.
- Any such user can **approve or reject any execution**, not only ones they
  started (`router.py:333-335`).
- Org connections — including stored credentials — are readable and deletable by
  any workflow-access member (`router.py:1635-1710`).
- `WorkflowVisibility` (`models.py:169-184`) is stored on every workflow and
  projected into listings, but **no authorization check ever consults it**.
  `get_workflow`'s docstring calls itself "visibility-checked"
  (`router.py:2767`); it is not.

If "a workflow member is assigned" is meant to be a real constraint, it does not
exist today. What exists is a single org-wide IT tier.

### `/api/admin/*` is not a higher tier

Every `/api/admin/*` **read** surface uses the same `_require_workflow_access`
gate as the normal API (`router.py:2927, 2991, 3091`). The only genuinely
tiered check in the repo is the halt **write** (`router.py:3109-3143`): global
scope is super-admin only, org scope needs org_admin, dept scope needs
dept_admin of that dept.

Note that `PATCH /api/workflows/{id}/schedule` (`router.py:3049`) — enable or
disable cron, rewrite the cron expression — is available to **any** workflow-
access role, with no admin tier at all.

---

## 3. Automation Control — backend yes, card no

**The card is not in the flows UI.** It was not ported.

What exists:

| Layer | State |
|---|---|
| Backend kill switches + schedule control | **Complete and enforced** — `router.py:2953-3154`, checked on execute (`4220`, `4332`), on webhook (`2314`), and in the scheduler (`scheduler.py:880-895`) |
| JS API client | **Present** — `listWorkflowAutomation`, `setWorkflowSchedule`, `listWorkflowHalt`, `setWorkflowHalt` (`ui/services/WorkflowService.js:677-703`) |
| UI screen | **Absent** — those four methods have **zero call sites** anywhere in `ui/`. No `AutomationControl` component, no route, no card |

The string "Automation Control" appears three times in the entire repo: a
backend section header, a scheduler comment, and the comment above those four
orphaned client methods. Nothing renders it.

What the UI *does* have, which is not the same thing:

- **Deploy / undeploy** on the canvas — the only automation on-off a user can
  reach (`WorkflowCanvas.js:818, 871`)
- **Cron authored inside the graph**, on the `scheduled_trigger` node, lifted
  into `payload.schedule` on save (`WorkflowCanvas.js:656-663`)
- **`paused`** in the run views, which means a human-approval pause on one run —
  not a scheduler pause

So the halt/kill-switch capability is live and reachable by API, and invisible in
the product. Wiring the card is a **UI-only task**: no backend work needed.

---

## 4. Security findings, confirmed against the running service

Ordered by what I would fix first.

| # | Finding | Evidence |
|---|---|---|
| 1 | **No way to create users** (§1) — blocks the whole role model | `create_user` unrouted |
| 2 | **Token revocation is not enforced.** `register_revocation_checker()` is never called, so the `revoked_tokens` blocklist does nothing and a 12-hour token cannot be killed early | `revocation.py:38`, never invoked outside tests |
| 3 | **`/openapi.json` and `/docs` are public** whenever `ENVIRONMENT` ≠ prod, and the shipped `.env` says `dev` — **live-confirmed 200 unauthenticated** | `middleware.py:383-385`, `.env:16` |
| 4 | **`GET /api/workflows/scheduler/health` has no authorization** — **live-confirmed 200 with a role-less, org-less token.** On the leader instance it returns `registered_jobs`, the workflow_ids of every scheduled workflow across all orgs | `router.py:3991`; its docstring claims it is unauthenticated, which is also wrong |
| 5 | **No rate limiting or lockout on `/api/auth/login`.** The rate limiters exist (`router.py:107,153`) and are not applied to auth routes | |
| 6 | **Empty-org bypass.** `_require_workflow_access` refuses a blank `org_id`; `_check_workflow_action` does not. Nine per-document handlers call only the latter, so an identity with `org_id == ""` matches any workflow whose org is also `""` | `router.py:401` vs `308-312` |
| 7 | **UI has no role gating.** No component reads `user.roles`. Admin surfaces are offered to everyone and fail at the API; `WorkflowListScreen` only reacts to a 403 after the fact | `WorkflowListScreen.js:73, 359-371` |
| 8 | **Token in `localStorage`** under `citra_flows_token` — readable by any script on the origin | `config.js:37-38` |

Separately, and not auth: **`_WRITE_NODE_TYPES` is referenced at
`router.py:428` and defined nowhere**, so `_validate_deploy_environment` raises
`NameError` and **every deploy of a workflow with at least one node returns
500**. Pre-existing, unrelated to the split.

---

## 5. Plan

### Phase 1 — make the product usable and safe to expose

1. **User management.** Route `create_user`, and add password change, disable,
   and role assignment. Gate to `org_admin` + `super_admin`, org-scoped. Without
   this nothing else in the role model is operable.
2. **Enforce revocation.** Call `register_revocation_checker()` at startup so the
   blocklist works, and add logout-invalidates-token.
3. **Close the doc leak.** Serve `/docs` and `/openapi.json` behind auth
   regardless of `ENVIRONMENT`, or gate on an explicit opt-in flag.
4. **Gate `scheduler/health`.** Require workflow access; return only the caller's
   org's jobs. Fix the docstring, which contradicts the code either way.
5. **Rate-limit login.** The limiter already exists; apply it.

### Phase 2 — the Automation Control card

UI-only; the API is done and the client methods are already written.

- New screen in `WorkflowBuilderScreen`'s view set, driven by
  `listWorkflowAutomation()` / `listWorkflowHalt()`
- Per-workflow: schedule on/off + cron, via `setWorkflowSchedule()`
- Kill switches at global / org / dept scope via `setWorkflowHalt()`, with the
  scope tiers the backend already enforces
- Show halt state where it bites — on the canvas and in the run views — so a
  halted workflow is not a silent no-op

### Phase 3 — decide what "assigned" should mean

This one needs your decision before any code.

Today: **one org-wide IT tier.** Anyone with an IT role can do anything to any
workflow in their org. `action` is ignored and `WorkflowVisibility` is dead
weight.

Either:

- **(a) Confirm org-wide is correct** for an IT-authored product — then delete
  `WorkflowVisibility`, the unused `action` parameter and `_visibility_filter`
  (`router.py:353`, already never called), and fix the docstrings that claim
  checks which do not happen; or
- **(b) Make assignment real** — honour `action` in `_check_workflow_action`,
  give workflows an author/assignee, and let visibility mean something.

Do not leave it as-is: the fields and docstrings currently describe (b) while the
code does (a), which is how someone ends up trusting a check that is not there.

### Phase 4 — cut the last runtime tie

Give notifications an SMTP path so email does not require Citra-AI's
user-service. Until then flows is not independently deployable, whatever the
auth story says.

### Phase 5 — residue

Delete or route the dead surfaces so the code stops describing a product that is
not there: `_visibility_filter`, `_dept_scoped_node_types`, `_data_discovery_url`,
`Roles.DECISION_APP_BUILDER`, the `on_behalf_of_*` read at `router.py:2649` that
can never fire, and the SA-ownership docstrings on live handlers. Pin
`WORKFLOW_IT_DEPT_ID` in `.env.example` rather than relying on the `"it"`
default. Fix `claims["email"]`, which is permanently `""` because the middleware
only ever sets `request.state.user_email` — every audit trail currently records a
uuid where it means to record an email.
