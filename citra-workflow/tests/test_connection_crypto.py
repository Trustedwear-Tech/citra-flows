# Copyright (c) 2026 Trustedwear Tech Private Limited (https://citra-ai.com)
# Author: Rohit Kumar Chandan
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may not
# use this file except in compliance with the License. You may obtain a copy of
# the License at http://www.apache.org/licenses/LICENSE-2.0

"""Tests for connection_crypto — widened secret-field coverage + guard."""

import pytest

from citra_workflow.connection_crypto import (
    encrypt_env_config,
    decrypt_env_config,
    assert_encrypted_envelope,
    is_encrypted,
    encrypt_value,
    ConnectionDecryptionError,
    SECRET_FIELDS,
)


@pytest.fixture(autouse=True)
def _key(monkeypatch):
    monkeypatch.setenv("CONNECTION_ENCRYPTION_KEY", "a" * 64)


def _sample_env():
    return {
        # secrets
        "connection_string": "postgres://u:p@h/db",
        "url": "https://api.example.com",
        "username": "svc_user",
        "password": "s3cr3t-pw",
        "private_key": "-----BEGIN KEY-----abc",
        "access_key_id": "AKIA123",
        "secret_access_key": "wJalrXUtnF",
        "headers": {"Authorization": "Bearer tok123"},
        # non-secrets
        "host": "db.internal",
        "port": "5432",
        "database": "sales",
        "region": "ap-south-1",
    }


def test_all_secret_fields_encrypted_and_roundtrip():
    enc = encrypt_env_config(_sample_env())
    # every secret scalar is now ciphertext
    for f in SECRET_FIELDS:
        assert is_encrypted(enc[f]), f"{f} should be encrypted"
    assert is_encrypted(enc["headers"]["Authorization"])
    # non-secret fields untouched
    assert enc["host"] == "db.internal"
    assert enc["port"] == "5432"
    assert enc["database"] == "sales"
    # round-trips back to the originals
    dec = decrypt_env_config(enc)
    src = _sample_env()
    for f in SECRET_FIELDS:
        assert dec[f] == src[f]
    assert dec["headers"]["Authorization"] == "Bearer tok123"


def test_guard_rejects_cleartext_secret():
    enc = encrypt_env_config(_sample_env())
    enc["password"] = "still-plaintext"  # simulate a bypassed encrypt
    with pytest.raises(ValueError) as ei:
        assert_encrypted_envelope(enc)
    assert "password" in str(ei.value)


def test_guard_passes_fully_encrypted_envelope():
    assert_encrypted_envelope(encrypt_env_config(_sample_env()))  # must not raise


def test_idempotent_skip_encrypted():
    once = encrypt_env_config(_sample_env())
    twice = encrypt_env_config(once, skip_encrypted=True)
    # values unchanged (no double-encryption) and still decrypt correctly
    assert twice["password"] == once["password"]
    assert decrypt_env_config(twice)["password"] == "s3cr3t-pw"


def test_decrypt_tolerates_legacy_plaintext():
    # A pre-backfill doc: connection_string encrypted, password still plaintext.
    env = {
        "connection_string": encrypt_value("postgres://u:p@h/db"),
        "password": "legacy-plaintext-pw",
    }
    dec = decrypt_env_config(env)
    assert dec["connection_string"] == "postgres://u:p@h/db"
    assert dec["password"] == "legacy-plaintext-pw"  # returned as-is, no crash


def test_decrypt_fails_loud_on_key_mismatch(monkeypatch):
    """Bug 007: a value that IS a Fernet token but was encrypted with a
    DIFFERENT key must raise (not silently return ciphertext downstream)."""
    # encrypt with key A
    monkeypatch.setenv("CONNECTION_ENCRYPTION_KEY", "A" * 64)
    enc = encrypt_env_config({"connection_string": "postgres://u:p@h/db"})
    assert is_encrypted(enc["connection_string"])
    # now the runtime has key B → decryption must fail loud
    monkeypatch.setenv("CONNECTION_ENCRYPTION_KEY", "B" * 64)
    with pytest.raises(ConnectionDecryptionError) as ei:
        decrypt_env_config(enc)
    assert "connection_string" in str(ei.value)
    assert "key mismatch" in str(ei.value).lower()


def test_decrypt_fails_loud_on_header_key_mismatch(monkeypatch):
    monkeypatch.setenv("CONNECTION_ENCRYPTION_KEY", "A" * 64)
    enc = encrypt_env_config({"headers": {"Authorization": "Bearer tok"}})
    monkeypatch.setenv("CONNECTION_ENCRYPTION_KEY", "B" * 64)
    with pytest.raises(ConnectionDecryptionError) as ei:
        decrypt_env_config(enc)
    assert "Authorization" in str(ei.value)


def test_is_encrypted_discriminates():
    assert is_encrypted(encrypt_value("x"))
    assert not is_encrypted("plain-password")
    assert not is_encrypted("")
    assert not is_encrypted(None)
