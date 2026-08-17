# Copyright (c) 2026 Trustedwear Tech Private Limited (https://citra-ai.com)
# Author: Rohit Kumar Chandan
# SPDX-License-Identifier: BUSL-1.1
#
# Licensed under the Business Source License 1.1. Non-production use is granted;
# production use requires a commercial licence until the Change Date, after
# which this file converts to Apache-2.0. See LICENSE at the repository root.

"""Resolve a connection_id to concrete connection details at runtime."""

from __future__ import annotations
import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


async def resolve_connection(
    connection_id: str,
    *,
    org_id: str,
    owner_id: str = "",
    owner_type: str = "",
    environment: str = "test",
) -> Dict[str, Any]:
    """Load a ConnectionProfile from MongoDB and return the decrypted config
    for the requested environment.

    SECURITY (C4): the connection is resolved ONLY if it belongs to the
    same tenant (``org_id``) as the running workflow. Tenant isolation is
    the hard boundary and always fails closed.

    Service-Account (SA) scoping: connections are created owned by the
    creating user's *work service account* (``owner_type="service_account"``,
    ``owner_id=work_sa_id``). Workflows, however, are an org/IT-managed
    surface and are **always org-owned** (``owner_type="org"``,
    ``owner_id=org_id``). The two ``owner_id`` values therefore live in
    *different namespaces* and must NOT be compared directly — doing so made
    every saved connection unusable at runtime (Bug 003).

    Correct rule:
      * org / dept-owned run  → org_id match is sufficient (a connection
        created within the org by any of its SAs is usable);
      * service-account-owned run → additionally require the connection's
        owning SA to match the run's SA (private-to-SA isolation preserved).

    ``owner_type``/``owner_id`` describe the *running workflow's* owner.

    Returns a dict with keys depending on connection type:
      SQL  → {"connection_string": "..."}
      Mongo → {"connection_string": "...", "database": "..."}
      API  → {"url": "...", "headers": {...}}
    Plus any ``extra`` overrides.
    """
    from citra_mongo import get_async_mongo_client, MONGODB_DATABASE
    from .connection_crypto import decrypt_env_config

    client = get_async_mongo_client()
    db = client[MONGODB_DATABASE]
    doc = await db["WorkflowConnections"].find_one(
        {"connection_id": connection_id}
    )
    if not doc:
        raise ValueError(f"Connection '{connection_id}' not found")

    # ── Ownership scope check — fail closed ──────────────────────────────
    # 1) Tenant isolation (hard boundary): the connection must belong to the
    #    same org as the running workflow.
    conn_org = doc.get("org_id") or ""
    conn_owner = doc.get("owner_id") or ""
    if not org_id or conn_org != org_id:
        raise ValueError(
            f"Connection '{connection_id}' is not accessible from this "
            f"workflow — it belongs to a different tenant, or predates "
            f"org scoping (recreate it)."
        )
    # 2) Service-Account isolation: only enforce when the RUNNING workflow is
    #    itself service-account-owned. Org/dept-owned workflows (the normal
    #    case) are bounded by the org_id check above — comparing the
    #    connection's SA owner_id against an org-owned workflow's owner_id
    #    (which is the org_id) is a namespace mismatch and previously broke
    #    every saved connection (Bug 003). Fails closed within the SA case.
    if owner_type == "service_account" and owner_id and conn_owner and conn_owner != owner_id:
        logger.warning(
            "Connection '%s' SA owner mismatch (conn_owner=%s run_owner=%s org=%s) — blocked",
            connection_id, conn_owner, owner_id, org_id,
        )
        raise ValueError(
            f"Connection '{connection_id}' belongs to a different Service "
            f"Account and cannot be used by this workflow."
        )

    env_key = environment if environment in ("test", "prod") else "test"
    if env_key != environment:
        logger.warning(
            "Unknown environment '%s' for connection '%s' — falling back to 'test'",
            environment, connection_id,
        )
    env_cfg = doc.get(env_key, {})
    if not env_cfg:
        raise ValueError(
            f"No '{env_key}' config on connection '{doc.get('name', connection_id)}'"
        )

    decrypted = decrypt_env_config(env_cfg)

    result: Dict[str, Any] = {}
    conn_type = doc.get("type", "")

    if conn_type == "sql":
        result["connection_string"] = decrypted.get("connection_string") or ""
    elif conn_type == "mongo":
        conn_str = decrypted.get("connection_string") or ""
        result["connection_string"] = conn_str
        # Extract database from URI if not explicitly stored
        db_name = decrypted.get("database") or ""
        if not db_name and conn_str:
            from urllib.parse import urlparse
            parsed = urlparse(conn_str)
            db_name = (parsed.path or "").lstrip("/").split("?")[0]
        result["database"] = db_name
    elif conn_type == "api":
        result["url"] = decrypted.get("url") or ""
        result["headers"] = decrypted.get("headers") or {}
    elif conn_type == "bucket":
        result["bucket_name"] = decrypted.get("bucket") or ""
        result["bucket"] = result["bucket_name"]  # alias for backward compat
        result["aws_region"] = decrypted.get("region") or "us-east-1"
        result["region"] = result["aws_region"]
        result["aws_access_key_id"] = decrypted.get("access_key_id") or ""
        result["access_key_id"] = result["aws_access_key_id"]
        result["aws_secret_access_key"] = decrypted.get("secret_access_key") or ""
        result["secret_access_key"] = result["aws_secret_access_key"]
        result["endpoint_url"] = decrypted.get("endpoint_url") or ""
    elif conn_type == "sftp":
        result["host"] = decrypted.get("host") or ""
        result["port"] = decrypted.get("port") or 22
        result["username"] = decrypted.get("username") or ""
        result["password"] = decrypted.get("password") or ""
        result["private_key"] = decrypted.get("private_key") or ""
        result["protocol"] = decrypted.get("protocol") or "sftp"
    elif conn_type == "smtp":
        result["host"] = decrypted.get("host") or "smtp.gmail.com"
        result["port"] = decrypted.get("port") or 587
        result["username"] = decrypted.get("username") or ""
        result["password"] = decrypted.get("password") or ""
        result["from_address"] = decrypted.get("from_address") or decrypted.get("username") or ""

    # Merge any extras
    result.update(decrypted.get("extra", {}))
    return result
