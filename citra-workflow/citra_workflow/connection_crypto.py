# Copyright (c) 2026 Trustedwear Tech Private Limited (https://citra-ai.com)
# Author: Rohit Kumar Chandan
# SPDX-License-Identifier: BUSL-1.1
#
# Licensed under the Business Source License 1.1. Non-production use is granted;
# production use requires a commercial licence until the Change Date, after
# which this file converts to Apache-2.0. See LICENSE at the repository root.

"""
Connection Credential Encryption
=================================
Encrypts/decrypts connection credentials before storing in MongoDB.
Uses Fernet symmetric encryption with a key from environment.

ALL credential-bearing fields of an EnvironmentConfig are encrypted at rest
(see ``SECRET_FIELDS`` + ``headers``). Non-secret fields (host, port, database,
region, bucket, protocol, endpoint_url, from_address, type, …) stay plaintext
so they remain displayable and filterable.

The key (``CONNECTION_ENCRYPTION_KEY``) is delivered to the process env at boot
by the Vault bag loader — it is NOT read from Vault per request. The same key
must be present in every service that executes workflows (citra-workflow,
Citra-Worker, Citra-Service), because decryption happens at execution time.
"""

import base64
import hashlib
import os
from typing import Any, Dict

from cryptography.fernet import Fernet, InvalidToken

# Scalar EnvironmentConfig fields that carry credential material. Kept in one
# place so encrypt / decrypt / the write-time guard / masking all agree.
SECRET_FIELDS = (
    "connection_string",
    "url",
    "username",
    "password",
    "private_key",
    "access_key_id",
    "secret_access_key",
)

# All Fernet tokens are version 0x80 and base64-encode to this prefix.
_FERNET_PREFIX = "gAAAAA"


def _get_fernet() -> Fernet:
    """Derive a Fernet key from the CONNECTION_ENCRYPTION_KEY env var."""
    secret = os.getenv("CONNECTION_ENCRYPTION_KEY", "")
    if not secret:
        raise RuntimeError(
            "CONNECTION_ENCRYPTION_KEY env var is required for connection encryption"
        )
    # Derive a 32-byte key via SHA-256 then base64-encode for Fernet
    key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest())
    return Fernet(key)


def encrypt_value(plaintext: str) -> str:
    """Encrypt a plaintext string, return base64-encoded ciphertext."""
    if not plaintext:
        return ""
    return _get_fernet().encrypt(plaintext.encode()).decode()


def decrypt_value(ciphertext: str) -> str:
    """Decrypt a ciphertext string back to plaintext."""
    if not ciphertext:
        return ""
    return _get_fernet().decrypt(ciphertext.encode()).decode()


def is_encrypted(value: Any) -> bool:
    """Best-effort check that ``value`` is already a Fernet token.

    Used to keep ``encrypt_env_config`` idempotent (backfill safety) and to
    power the write-time guard. A real credential is astronomically unlikely
    to be a valid Fernet token, so false positives are not a practical risk.
    """
    if not isinstance(value, str) or not value.startswith(_FERNET_PREFIX):
        return False
    try:
        raw = base64.urlsafe_b64decode(value.encode())
    except Exception:
        return False
    return len(raw) >= 73 and raw[0] == 0x80


def encrypt_env_config(config: dict, *, skip_encrypted: bool = False) -> dict:
    """Encrypt every secret field in an EnvironmentConfig dict.

    ``skip_encrypted=True`` leaves already-encrypted values untouched — used by
    the backfill migration so re-running it (or running it on a doc whose
    ``connection_string``/``url``/``headers`` were encrypted by the old code
    path) never double-encrypts.
    """
    result = dict(config)
    for field in SECRET_FIELDS:
        val = result.get(field)
        if val and isinstance(val, str):
            if skip_encrypted and is_encrypted(val):
                continue
            result[field] = encrypt_value(val)
    # Header values may carry auth tokens.
    headers = result.get("headers")
    if headers:
        result["headers"] = {
            k: (v if (skip_encrypted and is_encrypted(v)) else encrypt_value(v))
            for k, v in headers.items()
        }
    return result


class ConnectionDecryptionError(ValueError):
    """Raised when a value that IS a Fernet token cannot be decrypted with the
    current key — i.e. a CONNECTION_ENCRYPTION_KEY mismatch between the service
    that encrypted the connection and the service executing the workflow.

    This is deliberately a hard failure: silently returning the still-encrypted
    ciphertext (the previous behaviour) caused confusing downstream errors like
    "Could not parse SQLAlchemy URL" or AWS "AuthorizationHeaderMalformed"
    (Bug 007). Failing loud here makes the real cause obvious.
    """


def decrypt_env_config(config: dict) -> dict:
    """Decrypt every secret field in an EnvironmentConfig dict.

    - Genuine legacy plaintext (a value that is NOT a Fernet token) is returned
      as-is, so pre-encryption connections keep working until the backfill runs.
    - A value that IS a Fernet token but fails to decrypt means the current
      CONNECTION_ENCRYPTION_KEY does not match the one it was encrypted with.
      That is a configuration error, NOT legacy plaintext, so we FAIL LOUD with
      a clear message instead of letting ciphertext leak downstream (Bug 007).
    """
    result = dict(config)
    for field in SECRET_FIELDS:
        val = result.get(field)
        if val and isinstance(val, str):
            try:
                result[field] = decrypt_value(val)
            except InvalidToken:
                if is_encrypted(val):
                    raise ConnectionDecryptionError(
                        f"connection credential decryption failed for field "
                        f"'{field}': encryption key mismatch — the workflow "
                        f"runtime's CONNECTION_ENCRYPTION_KEY does not match the "
                        f"key used to encrypt this connection. Align the key "
                        f"across services or re-create the connection."
                    )
                # else: genuine legacy plaintext — leave as-is
    headers = result.get("headers")
    if headers:
        out: Dict[str, Any] = {}
        for k, v in headers.items():
            if isinstance(v, str) and v:
                try:
                    out[k] = decrypt_value(v)
                except InvalidToken:
                    if is_encrypted(v):
                        raise ConnectionDecryptionError(
                            f"connection header credential decryption failed "
                            f"for '{k}': encryption key mismatch (see "
                            f"CONNECTION_ENCRYPTION_KEY). Align the key or "
                            f"re-create the connection."
                        )
                    out[k] = v  # legacy plaintext
            else:
                out[k] = v
        result["headers"] = out
    return result


def assert_encrypted_envelope(config: dict) -> None:
    """Fail-closed guard: raise if any secret field would be stored cleartext.

    Call this AFTER ``encrypt_env_config`` and BEFORE persisting, so a missing
    field in ``SECRET_FIELDS``, a bypassed encrypt call, or a future bug can
    never silently write a plaintext credential to Mongo.
    """
    offenders = []
    for field in SECRET_FIELDS:
        val = config.get(field)
        if val and isinstance(val, str) and not is_encrypted(val):
            offenders.append(field)
    for k, v in (config.get("headers") or {}).items():
        if v and isinstance(v, str) and not is_encrypted(v):
            offenders.append(f"headers.{k}")
    if offenders:
        raise ValueError(
            "refusing to store connection with cleartext secret field(s): "
            + ", ".join(offenders)
        )
