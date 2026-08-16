"""Tests for _merge_env_preserving_secrets — the guard that stops a connection
edit from overwriting a stored secret with the display mask.

list/get return secrets masked (••••••••). If that mask (or a blank) is echoed
back on update, re-encrypting it would store the mask AS the credential and
silently break the connection. The merge keeps the existing secret for any
masked/blank field, while taking freshly-typed values and non-secret fields
from the incoming payload.
"""

import sys
import os
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from citra_workflow.router import (
    _merge_env_preserving_secrets,
    _looks_masked_or_blank,
)


class TestLooksMaskedOrBlank:
    def test_blank_and_none(self):
        assert _looks_masked_or_blank("") is True
        assert _looks_masked_or_blank("   ") is True
        assert _looks_masked_or_blank(None) is True

    def test_masked(self):
        assert _looks_masked_or_blank("••••••••") is True
        assert _looks_masked_or_blank("mssql+pyo••••••••") is True

    def test_real_value(self):
        assert _looks_masked_or_blank("mssql+pyodbc://u:p@h/db") is False


class TestMergeEnvPreservingSecrets:
    def test_masked_secret_is_kept_from_existing(self):
        """The classic corruption case: the UI echoes the masked connection
        string; the stored real value must survive."""
        with patch(
            "citra_workflow.router.decrypt_env_config",
            return_value={"connection_string": "mssql://real:pw@host/db", "password": "realpw"},
        ):
            merged = _merge_env_preserving_secrets(
                {"connection_string": "mssql://re••••••••", "password": "", "host": "newhost"},
                {"connection_string": "<ciphertext>"},  # opaque; decrypt is patched
            )
        assert merged["connection_string"] == "mssql://real:pw@host/db"  # masked → kept
        assert merged["password"] == "realpw"                            # blank → kept
        assert merged["host"] == "newhost"                               # non-secret → updated

    def test_freshly_typed_secret_replaces_existing(self):
        with patch(
            "citra_workflow.router.decrypt_env_config",
            return_value={"connection_string": "old-dsn"},
        ):
            merged = _merge_env_preserving_secrets(
                {"connection_string": "brand-new-dsn"},
                {"connection_string": "<ciphertext>"},
            )
        assert merged["connection_string"] == "brand-new-dsn"

    def test_masked_secret_dropped_when_no_existing(self):
        """No prior value to fall back to → drop the masked field rather than
        persist the mask."""
        merged = _merge_env_preserving_secrets(
            {"connection_string": "••••••••", "host": "h"},
            None,
        )
        assert "connection_string" not in merged
        assert merged["host"] == "h"

    def test_headers_preserved_when_empty(self):
        with patch(
            "citra_workflow.router.decrypt_env_config",
            return_value={"headers": {"Authorization": "Bearer real"}},
        ):
            merged = _merge_env_preserving_secrets(
                {"url": "https://api.example.com", "headers": {}},
                {"headers": "<ciphertext>"},
            )
        assert merged["headers"] == {"Authorization": "Bearer real"}

    def test_headers_replaced_when_provided(self):
        with patch(
            "citra_workflow.router.decrypt_env_config",
            return_value={"headers": {"Authorization": "Bearer old"}},
        ):
            merged = _merge_env_preserving_secrets(
                {"headers": {"Authorization": "Bearer new"}},
                {"headers": "<ciphertext>"},
            )
        assert merged["headers"] == {"Authorization": "Bearer new"}
