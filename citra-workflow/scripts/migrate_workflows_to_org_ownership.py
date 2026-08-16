"""
One-time migration: normalise every workflow document to org ownership.

Reason
------
Workflows used to be ownable by service accounts, departments, or orgs
(see WorkflowDefinition.owner_type in citra_workflow/models.py before
the IT-workflow rollout). The policy is now: every workflow is owned by
the org. This script brings legacy rows into the new shape so the
single-rule authorization in router.py (_visibility_filter and
_check_workflow_action) returns them correctly.

What it does
------------
1. For every Workflows document where owner_type != "org":
     - push the prior (owner_type, owner_id) onto previous_owners with a
       "migration_2026" reason
     - set owner_type="org"
     - set owner_id to the doc's org_id (or, when org_id is missing,
       resolve from the author's CitraAIUser doc and backfill)
     - set lifecycle_stage="org_managed"
     - stamp migration_audit with the prior state

2. For every Workflows document whose author_user_id does NOT currently
   hold one of {IT-workflow, org_admin, super_admin} AND is not a
   dept_admin in the IT department:
     - flag quarantine_status="author_not_it_role"
     - preserve author_user_id for audit
     - the row stays org-owned and visible to current IT users; the flag
       is a signal for the org_admin to either grant IT-workflow to that
       author retroactively or archive the workflow

3. Print a summary by category. Re-runnable; the script skips rows that
   are already in the target shape.

Usage
-----
    python scripts/migrate_workflows_to_org_ownership.py            # dry run
    python scripts/migrate_workflows_to_org_ownership.py --apply    # write

Requires the same MONGODB_URI / MONGODB_DATABASE env vars as the service.
The IT-dept slug is read from WORKFLOW_IT_DEPT_ID (default "it"), matching
citra_workflow/router.py and Citra-User-Service/src/middleware/authMiddleware.js.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections import Counter
from datetime import datetime
from typing import Any, Dict, Optional

from citra_mongo import get_async_mongo_client, MONGODB_DATABASE


IT_DEPT_ID = (os.environ.get("WORKFLOW_IT_DEPT_ID") or "it").lower()
WORKFLOW_ROLES = {"super_admin", "org_admin", "IT-workflow"}


async def _user_has_workflow_role(users_coll, author_user_id: str) -> bool:
    if not author_user_id:
        return False
    user = await users_coll.find_one(
        {"email": author_user_id},
        projection={"roles": 1, "dept_ids": 1},
    )
    if not user:
        return False
    roles = set(user.get("roles") or [])
    if roles & WORKFLOW_ROLES:
        return True
    if "dept_admin" in roles:
        depts = [str(d).lower() for d in (user.get("dept_ids") or [])]
        if IT_DEPT_ID in depts:
            return True
    return False


async def _resolve_org_id(users_coll, doc: Dict[str, Any]) -> Optional[str]:
    """Prefer doc.org_id; fall back to the author's user record."""
    if doc.get("org_id"):
        return doc["org_id"]
    author = doc.get("author_user_id") or doc.get("user_id") or ""
    if not author:
        return None
    user = await users_coll.find_one({"email": author}, projection={"org_id": 1})
    return (user or {}).get("org_id")


async def main(apply_changes: bool) -> int:
    client = get_async_mongo_client()
    db = client[MONGODB_DATABASE]
    workflows = db["Workflows"]
    users = db["users"]

    now = datetime.utcnow()
    counts = Counter()
    quarantined = []
    orphan_no_org = []

    cursor = workflows.find({})
    async for doc in cursor:
        counts["scanned"] += 1
        wf_id = doc.get("workflow_id") or str(doc.get("_id"))
        owner_type = (doc.get("owner_type") or "").strip()
        owner_id = (doc.get("owner_id") or "").strip()

        org_id = await _resolve_org_id(users, doc)
        if not org_id:
            counts["orphan_no_org"] += 1
            orphan_no_org.append(wf_id)
            continue

        update_set: Dict[str, Any] = {}
        update_push: Dict[str, Any] = {}

        # 1. owner_type / owner_id / lifecycle_stage
        if owner_type != "org" or owner_id != org_id:
            counts[f"owner_was_{owner_type or 'unset'}"] += 1
            update_set.update({
                "owner_type": "org",
                "owner_id": org_id,
                "org_id": org_id,
                "lifecycle_stage": "org_managed",
                "owner_changed_at": now,
                "owner_changed_by": "migration_2026",
                "updated_at": now,
            })
            update_push["previous_owners"] = {
                "owner_type": owner_type or "unset",
                "owner_id": owner_id,
                "changed_at": now,
                "changed_by": "migration_2026",
                "reason": "org-only-ownership migration",
            }
        else:
            counts["already_org_owned"] += 1

        # 2. quarantine_status for authors who are not IT-role today
        author = doc.get("author_user_id") or doc.get("user_id") or ""
        if author:
            has_role = await _user_has_workflow_role(users, author)
            if not has_role and doc.get("quarantine_status") != "author_not_it_role":
                update_set["quarantine_status"] = "author_not_it_role"
                counts["quarantined"] += 1
                quarantined.append({"workflow_id": wf_id, "author": author})

        if not update_set:
            continue

        op: Dict[str, Any] = {"$set": update_set}
        if update_push:
            op["$push"] = update_push

        if apply_changes:
            await workflows.update_one({"_id": doc["_id"]}, op)
            counts["updated"] += 1
        else:
            counts["would_update"] += 1

    print("=" * 60)
    print(f"Migration {'APPLIED' if apply_changes else 'DRY RUN'} at {now.isoformat()}")
    print(f"IT_DEPT_ID = {IT_DEPT_ID!r}")
    print("=" * 60)
    for k, v in sorted(counts.items()):
        print(f"  {k:>24} : {v}")
    if quarantined:
        print()
        print(f"Quarantined ({len(quarantined)}) — author not in IT roles:")
        for q in quarantined[:50]:
            print(f"  {q['workflow_id']}  author={q['author']}")
        if len(quarantined) > 50:
            print(f"  ... and {len(quarantined) - 50} more")
    if orphan_no_org:
        print()
        print(f"Skipped — no org_id resolvable ({len(orphan_no_org)}):")
        for wf in orphan_no_org[:20]:
            print(f"  {wf}")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true",
                        help="actually write changes (default: dry run)")
    args = parser.parse_args()
    sys.exit(asyncio.run(main(args.apply)))
