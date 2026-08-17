<!--
  Copyright (c) 2026 Trustedwear Tech Private Limited (https://citra-ai.com)
  Author: Rohit Kumar Chandan
  SPDX-License-Identifier: BUSL-1.1

  Licensed under the Business Source License 1.1. Non-production use is granted;
  production use requires a commercial licence until the Change Date, after
  which this file converts to Apache-2.0. See LICENSE at the repository root.
-->

# Workflow visibility & ownership

Status: **current state + the rule it enforces.** This note is the
authoritative spec for *who can see, run, and edit a workflow* in
`citra-workflow`. It exists because the model is easy to get wrong:
workflows are **not** an "IT product" and Smart Apps a "BA product" —
both are the same kind of artifact under one uniform rule.

## The principle

> Every workflow is **owned**, and visible only to the owner plus the
> people the owner (or a department/org admin over it) is entitled to.
> There is no global "all workflows" list. Nothing is visible by
> department *job function* — only by ownership and explicit grant.

A workflow built by a Business Analyst (BA) is **not** an "IT workflow."
It is the automation half of that BA's Smart App — same owner, same
Service Account, same visibility scope as the AgentSpec. The IT
department never sees it, because it was never theirs. Symmetrically, a
workflow IT builds for system-to-system integration is invisible to BA.

**IT and BA are peer departments.** The model is department-agnostic:
"IT" and "BA" are just two `dept_id`s. No code branches on which
department a user is in.

## Ownership model

Each workflow doc carries two identity layers (see `create_workflow` in
[`router.py`](../citra-workflow/citra_workflow/router.py)):

| Layer | Fields | Mutable? | Meaning |
|---|---|---|---|
| **Author** | `author_user_id`, `author_email`, `author_at` | No | Who first created it. Audit only — never an authorization input. |
| **Owner** | `owner_type`, `owner_id` | Via transfer only | Who the workflow *belongs to*. All authorization derives from this. |

`owner_type` is one of — `owner_type="user"` is **rejected** so a
workflow never dies with the person who made it:

- **`service_account`** — owned by a Service Account (SA). The normal
  case. A BA's workflow is owned by the BA's **Work SA**. Survives the
  BA leaving; transferable.
- **`dept`** — owned by a department. Org/dept-admin-managed escalation
  tier.
- **`org`** — owned by the org. Requires `org_admin`/`super_admin` to
  create.

Every doc also records `org_id` and `dept_ids` (the owning SA's
departments, copied from the creator's JWT at create time). `dept_ids`
is what links an SA-owned workflow back to a department for admin
visibility — see the dept-admin lens below.

## Who can SEE a workflow — the three list lenses

`GET /api/workflows?scope=` serves three lenses. A user only ever sees
rows one of these lenses returns.

| Scope | Returns | For |
|---|---|---|
| `mine` | Workflows on the caller's **own Work SA** only. | Everyone. |
| `shared` | Workflows on any **other** SA the caller is admin or member of. | Cross-SA / cross-dept grants (support team, co-owners). |
| `admin` | **dept_admin** → every workflow tagged to one of the caller's departments (incl. SA-owned ones, via `dept_ids` overlap). **org_admin / super_admin** → every workflow in the org. Non-admins get **403**. | Audit / production-support visibility. |

The crucial consequence: **a department's `dept_admin` sees every SA's
workflows in that department.** A BA-department dept_admin sees all the
BA team's workflows. The IT-department dept_admin sees IT's — and *not*
BA's, because the `dept_ids` don't overlap. Only `org_admin` /
`super_admin` see across departments.

## Cross-department grants — the support team

The BA owns their Work SA and is its admin. To give a production-support
person (who may sit in a *different* department) visibility, the SA
admin **adds them to the SA** — as a member (read + run) or admin (full
control). They then see those workflows under their **`shared`** lens.

This is the only sanctioned way a cross-department user gains access to
another department's workflows short of being an org admin. The BA
decides who; the grant is explicit and per-SA.

## Action authorization — read / run / edit

Listing answers "see"; `_check_workflow_action` answers "may I *do* X."
For `owner_type = service_account`:

| Principal | read | run | edit |
|---|---|---|---|
| `super_admin` | ✅ | ✅ | ✅ |
| `org_admin` (own org, `org_admin_override` on) | ✅ | ✅ | ✅ |
| SA **admin** | ✅ | ✅ | ✅ |
| SA **member** | ✅ | ✅ | ❌ |
| `dept_admin` of the SA's department | ✅ | ❌ | ❌ |
| Anyone else | ❌ | ❌ | ❌ |

The `dept_admin` row is the **audit lens**: a department admin can *see*
(read) any workflow in their department, including SA-owned ones, but
**run and edit stay with the owning SA** — the BA still owns the SA.
`visibility.read/run/edit` on the doc can broaden the audience further
(`dept` / `org` / `public`) but never narrows the rules above.

## Error notifications

When a deployed workflow fails, the alert reaches **the workflow's owner
(the BA who authored it) and the support recipients the BA configured**
— never a platform-wide default and never the IT department.

This is a first-class field on the workflow,
`notifications: WorkflowNotifications` (see `models.py`):

- `notify_on_failure` (bool, default `true`) — master switch.
- `support_emails` (list) — extra recipients beyond the author; often
  cross-department production-support staff.

`WorkflowExecutor._notify_failure` resolves the recipient set as
`author_email ∪ support_emails` (deduped, case-insensitive). Workflows
created before `author_email` existed fall back to the author's
user-doc email. The BA edits this list in the **workflow Settings**
modal (gear icon in the canvas toolbar).

## The defect fixed alongside this doc

The `scope=admin` list lens already returned a dept_admin the SA-owned
workflows in their department, but `_check_workflow_action` did **not**
grant that same dept_admin `read` on an `owner_type=service_account`
workflow. A dept_admin saw rows in their "Admin · dept" tab that 403'd
on open. Fixed by adding the dept-admin read lens to
`_check_workflow_action`, making the open path consistent with the list
path.

## Known gaps / future work

1. **Support as a role, not a per-workflow chore.** Today a support
   person is added per-SA. If a BA forgets, a broken workflow has nobody
   watching. Consider a dept- or SA-scoped *support* role granted once
   and inherited by every workflow on the SA.
2. **Notification recipients are per-workflow, not per-SA.** A BA sets
   `support_emails` on each workflow. If they own many workflows, an
   SA-level default that workflows inherit would save repetition — and
   pairs naturally with gap 1 (a support *role* on the SA).
3. **`SmartAppInvokerNode` references are unmanaged.** The node takes a
   free-text `app_slug` + `action`; nothing pins an AgentSpec version or
   surfaces "which workflows invoke this app." Under SA ownership the BA
   owns both sides so this is intra-team hygiene, not a cross-team
   contract — but a managed picker + version pin would still help. See
   [`smart-app-architecture.md`](smart-app-architecture.md).
4. **Workflow as a Smart App tab.** A BA reaches their workflow through
   a generic "Workflows" screen today. The cleaner end state is an
   "Automation / Schedule" tab *inside* the Smart App, so visibility
   inherits from the Smart App and the BA never context-switches.

## Related

- [`access-control.md`](access-control.md) — read/write role gating for
  the dept MCP and SmartApp data plane (a separate concern).
- [`smart-app-architecture.md`](smart-app-architecture.md) — Smart App
  build vs runtime; the two invocation patterns (human-fed vs
  system-fed).
