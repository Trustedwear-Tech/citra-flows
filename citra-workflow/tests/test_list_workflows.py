# Copyright (c) 2026 Trustedwear Tech Private Limited (https://citra-ai.com)
# Author: Rohit Kumar Chandan
# SPDX-License-Identifier: BUSL-1.1
#
# Licensed under the Business Source License 1.1. Non-production use is granted;
# production use requires a commercial licence until the Change Date, after
# which this file converts to Apache-2.0. See LICENSE at the repository root.

"""Tests for GET /api/workflows list — pagination + name/description search.

Focus on how the Mongo ``match`` filter is assembled, since that is what
guarantees search composes correctly with org scoping, the smart-app-kind
filter, and the ``linked_smart_app_id`` scope — and that ``has_more`` is
derived correctly. The DB is faked so we can capture the pipeline / match that
the endpoint builds.
"""

import os
import re
import sys

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from citra_workflow import router as wf_router
from citra_workflow.router import list_workflows


class _AggCursor:
    """Async cursor over a fixed list of docs (what aggregate() returns)."""

    def __init__(self, docs):
        self._docs = docs

    def __aiter__(self):
        async def _gen():
            for d in self._docs:
                yield d
        return _gen()


def _make_db(docs, total):
    """Fake DB that records the aggregate pipeline and the count match."""
    captured = {}
    wf_col = MagicMock()

    def _aggregate(pipeline):
        captured["pipeline"] = pipeline
        return _AggCursor(docs)

    async def _count(match):
        captured["match"] = match
        return total

    wf_col.aggregate = MagicMock(side_effect=_aggregate)
    wf_col.count_documents = AsyncMock(side_effect=_count)

    db = MagicMock()
    db.__getitem__ = MagicMock(return_value=wf_col)
    return db, captured


def _doc(wf_id, name, kind="dept_data_flow"):
    return {"workflow_id": wf_id, "name": name, "description": "", "workflow_kind": kind}


async def _call(*, search=None, include_smart_app_action=False,
                linked_smart_app_id=None, skip=0, limit=30, docs=None, total=0):
    db, captured = _make_db(docs or [], total)
    with patch.object(wf_router, "_db", return_value=db), \
         patch.object(wf_router, "_require_workflow_access",
                      return_value={"org_id": "org-1"}):
        resp = await list_workflows(
            MagicMock(),
            skip=skip,
            limit=limit,
            include_smart_app_action=include_smart_app_action,
            linked_smart_app_id=linked_smart_app_id,
            search=search,
        )
    # The page query and the count must share the same match.
    assert captured["pipeline"][0]["$match"] is captured["match"]
    return resp, captured["match"]


# ─── search composes with the other filters ────────────────────────────

@pytest.mark.asyncio
async def test_search_composes_with_org_scope_and_kind_filter():
    _, match = await _call(search="payroll")
    # org scoping preserved
    assert match["org_id"] == "org-1"
    assert match["is_active"] == {"$ne": False}
    # default IT view still hides smart_app_action
    assert match["workflow_kind"] == {"$ne": "smart_app_action"}
    # search applied to name + description, case-insensitive
    assert match["$or"] == [
        {"name": {"$regex": "payroll", "$options": "i"}},
        {"description": {"$regex": "payroll", "$options": "i"}},
    ]


@pytest.mark.asyncio
async def test_search_with_include_smart_app_action_drops_kind_filter():
    _, match = await _call(search="payroll", include_smart_app_action=True)
    assert "workflow_kind" not in match           # not excluded anymore
    assert "$or" in match                          # search still applied
    assert match["org_id"] == "org-1"


@pytest.mark.asyncio
async def test_search_with_linked_smart_app_id_scope():
    _, match = await _call(search="payroll", linked_smart_app_id="app-9")
    assert match["linked_smart_app_id"] == "app-9"
    assert "workflow_kind" not in match            # explicit app scope: all kinds
    assert "$or" in match
    assert match["org_id"] == "org-1"


@pytest.mark.asyncio
async def test_search_term_is_regex_escaped():
    # Regex metacharacters must be matched literally, not interpreted.
    raw = "a.b*(c)+"
    _, match = await _call(search=raw)
    expected = re.escape(raw)
    assert match["$or"][0]["name"]["$regex"] == expected
    assert ".*" not in match["$or"][0]["name"]["$regex"]


@pytest.mark.asyncio
async def test_blank_search_adds_no_or_clause():
    _, match = await _call(search="   ")
    assert "$or" not in match


@pytest.mark.asyncio
async def test_none_search_adds_no_or_clause():
    _, match = await _call(search=None)
    assert "$or" not in match


# ─── has_more derivation ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_has_more_true_when_more_pages_remain():
    docs = [_doc(f"wf-{i}", f"Flow {i}") for i in range(30)]
    resp, _ = await _call(skip=0, limit=30, docs=docs, total=100)
    assert resp["total"] == 100
    assert resp["has_more"] is True
    assert len(resp["workflows"]) == 30


@pytest.mark.asyncio
async def test_has_more_false_on_last_page():
    docs = [_doc(f"wf-{i}", f"Flow {i}") for i in range(10)]
    resp, _ = await _call(skip=90, limit=30, docs=docs, total=100)
    assert resp["has_more"] is False  # 90 + 10 == 100


@pytest.mark.asyncio
async def test_search_total_reflects_filtered_set():
    # total comes from count_documents(match) so it tracks the filtered set.
    docs = [_doc("wf-1", "Payroll sync")]
    resp, match = await _call(search="payroll", docs=docs, total=1)
    assert resp["total"] == 1
    assert resp["has_more"] is False
    assert "$or" in match
