# Copyright (c) 2026 Trustedwear Tech Private Limited (https://citra-ai.com)
# Author: Rohit Kumar Chandan
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may not
# use this file except in compliance with the License. You may obtain a copy of
# the License at http://www.apache.org/licenses/LICENSE-2.0

"""
Backfill: encrypt the previously-plaintext secret fields in WorkflowConnections.
================================================================================

Before the encryption coverage was widened, only ``connection_string`` / ``url``
/ ``headers`` were encrypted; ``username`` / ``password`` / ``private_key`` /
``access_key_id`` / ``secret_access_key`` were stored in CLEARTEXT. This script
encrypts those remaining fields in place, using the SAME ``CONNECTION_ENCRYPTION_KEY``.

It is **idempotent** — `encrypt_env_config(..., skip_encrypted=True)` leaves any
already-encrypted value untouched, so re-running it (or running it on a doc
whose 3 legacy fields are already encrypted) never double-encrypts. After it
runs, ``assert_encrypted_envelope`` verifies no cleartext secret remains.

Requirements (env):
  - CONNECTION_ENCRYPTION_KEY  (the SAME key the app uses)
  - MONGODB_URI / MONGODB_DATABASE  (consumed by citra_mongo)

Usage:
  python -m scripts.backfill_connection_encryption --dry-run
  python -m scripts.backfill_connection_encryption
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

# Allow running as a plain script from the repo root or the scripts/ dir.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from citra_mongo import get_async_mongo_client, MONGODB_DATABASE  # noqa: E402
from citra_workflow.connection_crypto import (  # noqa: E402
    encrypt_env_config,
    assert_encrypted_envelope,
    is_encrypted,
    SECRET_FIELDS,
)


def _cleartext_secret_fields(env: dict) -> list[str]:
    """Secret fields present in this envelope that are NOT yet encrypted."""
    if not env:
        return []
    out = [
        f for f in SECRET_FIELDS
        if env.get(f) and isinstance(env[f], str) and not is_encrypted(env[f])
    ]
    out += [
        f"headers.{k}" for k, v in (env.get("headers") or {}).items()
        if v and isinstance(v, str) and not is_encrypted(v)
    ]
    return out


async def main(dry_run: bool) -> int:
    if not os.getenv("CONNECTION_ENCRYPTION_KEY"):
        print("ERROR: CONNECTION_ENCRYPTION_KEY is not set — refusing to run.", file=sys.stderr)
        return 2

    client = get_async_mongo_client()
    db = client[MONGODB_DATABASE]
    coll = db["WorkflowConnections"]

    scanned = modified = 0
    total_fields = 0

    async for doc in coll.find({}):
        scanned += 1
        updates: dict = {}
        for env_key in ("test", "prod"):
            env = doc.get(env_key)
            if not env:
                continue
            pending = _cleartext_secret_fields(env)
            if not pending:
                continue
            new_env = encrypt_env_config(env, skip_encrypted=True)
            assert_encrypted_envelope(new_env)  # fail-closed safety net
            updates[env_key] = new_env
            total_fields += len(pending)
            print(
                f"  {doc.get('connection_id', doc.get('_id'))} "
                f"[{env_key}] -> encrypt {', '.join(pending)}"
            )

        if updates:
            modified += 1
            if not dry_run:
                await coll.update_one({"_id": doc["_id"]}, {"$set": updates})

    verb = "would encrypt" if dry_run else "encrypted"
    print(
        f"\n{'DRY-RUN — ' if dry_run else ''}scanned {scanned} connection(s); "
        f"{verb} {total_fields} cleartext secret field(s) across {modified} doc(s)."
    )
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Backfill-encrypt WorkflowConnections secret fields.")
    ap.add_argument("--dry-run", action="store_true", help="report only; do not write")
    args = ap.parse_args()
    raise SystemExit(asyncio.run(main(args.dry_run)))
