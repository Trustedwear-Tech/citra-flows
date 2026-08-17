# Copyright (c) 2026 Trustedwear Tech Private Limited (https://citra-ai.com)
# Author: Rohit Kumar Chandan
# SPDX-License-Identifier: BUSL-1.1
#
# Licensed under the Business Source License 1.1. Non-production use is granted;
# production use requires a commercial licence until the Change Date, after
# which this file converts to Apache-2.0. See LICENSE at the repository root.

"""Runtime workflow halt control — the workflow-engine kill switches.

Mirror of smart-app-service/automation_control.py for the WORKFLOW engine. A
single Mongo collection (``workflow_control``) holds halt records at three
scopes, checked before a scheduled/manual workflow run is enqueued:

    scope_type : "global" | "org" | "dept"
    scope_id   : "*" (global) | <org_id> | "<org_id>:<dept_id>"
    enabled    : bool   (True = HALTED)
    reason, actor, updated_at

Precedence (first match halts): global > org > dept. So an org/super admin
flips ``global`` (stop ALL workflows) or ``org``; an IT dept admin freezes their
``dept``. Dept ids are org-qualified ("<org>:<dept>") because they are not
unique across orgs. Cached in-process a few seconds; a write invalidates it.

FAIL-OPEN: if the control store can't be read, runs are ALLOWED and the failure
is logged loudly — a flag-store blip must not stop all workflows on its own.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger("citra_workflow.workflow_control")

SCOPE_TYPES = ("global", "org", "dept")

_cache: Dict[str, Any] = {"at": 0.0, "controls": []}
_CACHE_TTL_SECONDS = 5.0


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def invalidate_cache() -> None:
    _cache["at"] = 0.0


async def _active_controls(col) -> List[dict]:
    now = time.time()
    if (now - _cache["at"]) < _CACHE_TTL_SECONDS:
        return _cache["controls"]
    try:
        docs = await col.find({"enabled": True}).to_list(length=2000)
    except Exception as e:  # fail-open
        logger.error("workflow_control: control-store read FAILED (fail-open, runs ALLOWED): %s", e)
        return []
    _cache["at"] = now
    _cache["controls"] = docs
    return docs


async def get_halt(col, *, org_id: Optional[str], dept_tokens: Optional[List[str]]) -> Optional[dict]:
    """The halting record (or None) for a workflow with this org + dept tokens."""
    controls = await _active_controls(col)
    if not controls:
        return None
    depset = {d for d in (dept_tokens or []) if d}
    by_type: Dict[str, List[dict]] = {t: [] for t in SCOPE_TYPES}
    for c in controls:
        by_type.get(c.get("scope_type"), []).append(c)
    for c in by_type["global"]:
        return c
    for c in by_type["org"]:
        if org_id and c.get("scope_id") == org_id:
            return c
    for c in by_type["dept"]:
        if c.get("scope_id") in depset:
            return c
    return None


async def set_control(col, *, scope_type: str, scope_id: Optional[str], enabled: bool, actor: str, reason: str = "") -> dict:
    if scope_type not in SCOPE_TYPES:
        raise ValueError(f"invalid scope_type {scope_type!r}")
    sid = "*" if scope_type == "global" else (scope_id or "")
    if scope_type != "global" and not sid:
        raise ValueError(f"scope_id required for scope_type={scope_type}")
    key = {"scope_type": scope_type, "scope_id": sid}
    doc = {**key, "enabled": bool(enabled), "reason": reason or "", "actor": actor, "updated_at": _now_iso()}
    await col.update_one(key, {"$set": doc}, upsert=True)
    invalidate_cache()
    return doc


async def list_controls(col) -> List[dict]:
    try:
        docs = await col.find({"enabled": True}).sort("updated_at", -1).to_list(length=2000)
    except Exception as e:
        logger.error("workflow_control: list failed: %s", e)
        return []
    for d in docs:
        d.pop("_id", None)
    return docs
