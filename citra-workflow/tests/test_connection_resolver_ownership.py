"""Ownership-scope tests for resolve_connection (Bug 003 regression).

Bug 003: connections are created owned by the creating user's *work service
account* (owner_type="service_account", owner_id=work_sa_id), but workflows are
always org-owned (owner_type="org", owner_id=org_id). The resolver previously
compared connection.owner_id (an SA id) against the running workflow's owner_id
(an org id) — different namespaces — so EVERY saved connection failed at runtime
with "belongs to a different Service Account".

Fix: tenant isolation (org_id) is the hard boundary; the SA-equality check only
applies when the *running workflow* is itself service-account-owned.
"""
from __future__ import annotations

import sys
import types
from unittest.mock import AsyncMock, MagicMock

import pytest

from citra_workflow import connection_resolver


def _install_fake_mongo(monkeypatch, conn_doc):
    """Make resolve_connection's `from citra_mongo import ...` return a fake
    client whose WorkflowConnections.find_one yields ``conn_doc``."""
    col = MagicMock()
    col.find_one = AsyncMock(return_value=conn_doc)
    db = MagicMock()
    db.__getitem__ = MagicMock(return_value=col)
    client = MagicMock()
    client.__getitem__ = MagicMock(return_value=db)

    fake_mod = types.ModuleType("citra_mongo")
    fake_mod.get_async_mongo_client = MagicMock(return_value=client)
    fake_mod.MONGODB_DATABASE = "testdb"
    monkeypatch.setitem(sys.modules, "citra_mongo", fake_mod)

    # decrypt_env_config is imported as `from .connection_crypto import ...`
    monkeypatch.setattr(
        connection_resolver,
        "__name__",
        connection_resolver.__name__,
    )
    import citra_workflow.connection_crypto as cc
    monkeypatch.setattr(
        cc, "decrypt_env_config",
        lambda env: {"connection_string": "postgresql://u:p@h:5432/db"},
        raising=True,
    )


def _conn_doc(owner_type="service_account", owner_id="sa-123", org_id="org-1"):
    return {
        "connection_id": "c1",
        "type": "sql",
        "org_id": org_id,
        "owner_type": owner_type,
        "owner_id": owner_id,
        "name": "ClaudeLiveTest_Postgres",
        "test": {"connection_string": "enc"},
        "prod": {},
    }


@pytest.mark.asyncio
async def test_org_workflow_can_use_sa_owned_connection_same_org(monkeypatch):
    """THE BUG 003 CASE: org-owned workflow, SA-owned connection, same org → works."""
    _install_fake_mongo(monkeypatch, _conn_doc(owner_type="service_account",
                                               owner_id="sa-123", org_id="org-1"))
    out = await connection_resolver.resolve_connection(
        "c1", org_id="org-1", owner_id="org-1", owner_type="org", environment="test",
    )
    assert out["connection_string"].startswith("postgresql://")


@pytest.mark.asyncio
async def test_dept_workflow_can_use_sa_owned_connection_same_org(monkeypatch):
    _install_fake_mongo(monkeypatch, _conn_doc(org_id="org-1"))
    out = await connection_resolver.resolve_connection(
        "c1", org_id="org-1", owner_id="dept-9", owner_type="dept", environment="test",
    )
    assert out["connection_string"].startswith("postgresql://")


@pytest.mark.asyncio
async def test_cross_org_is_blocked(monkeypatch):
    """Tenant isolation: different org always fails closed."""
    _install_fake_mongo(monkeypatch, _conn_doc(org_id="org-1"))
    with pytest.raises(ValueError, match="different tenant|not accessible"):
        await connection_resolver.resolve_connection(
            "c1", org_id="org-2", owner_id="org-2", owner_type="org", environment="test",
        )


@pytest.mark.asyncio
async def test_sa_owned_run_matching_sa_works(monkeypatch):
    """SA-owned run using its own SA's connection → works."""
    _install_fake_mongo(monkeypatch, _conn_doc(owner_id="sa-123", org_id="org-1"))
    out = await connection_resolver.resolve_connection(
        "c1", org_id="org-1", owner_id="sa-123", owner_type="service_account",
        environment="test",
    )
    assert out["connection_string"].startswith("postgresql://")


@pytest.mark.asyncio
async def test_sa_owned_run_mismatched_sa_is_blocked(monkeypatch):
    """SA-owned run referencing a DIFFERENT SA's connection → blocked (isolation kept)."""
    _install_fake_mongo(monkeypatch, _conn_doc(owner_id="sa-OTHER", org_id="org-1"))
    with pytest.raises(ValueError, match="different Service Account"):
        await connection_resolver.resolve_connection(
            "c1", org_id="org-1", owner_id="sa-123", owner_type="service_account",
            environment="test",
        )


@pytest.mark.asyncio
async def test_default_owner_type_does_not_block_same_org(monkeypatch):
    """Backward-compat: a caller that omits owner_type must not block (org isolation only)."""
    _install_fake_mongo(monkeypatch, _conn_doc(owner_id="sa-123", org_id="org-1"))
    out = await connection_resolver.resolve_connection(
        "c1", org_id="org-1", owner_id="org-1", environment="test",
    )
    assert out["connection_string"].startswith("postgresql://")
