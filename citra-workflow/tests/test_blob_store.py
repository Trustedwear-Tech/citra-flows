"""Tests for the binary-blob store (Track 4 — multimodal foundation).

GridFS I/O is mocked; the descriptor logic and put/get plumbing are exercised
directly.
"""
import sys
import os
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from citra_workflow import blob_store
from citra_workflow.blob_store import (
    is_blob, blob_descriptor, put_blob, get_blob, find_blobs, BLOB_KEY,
)


class TestBlobDescriptor:
    def test_is_blob_true(self):
        assert is_blob({BLOB_KEY: {"id": "abc", "mime": "image/png"}}) is True

    def test_is_blob_false(self):
        assert is_blob({"text": "hello"}) is False
        assert is_blob({BLOB_KEY: {"mime": "image/png"}}) is False  # no id
        assert is_blob("not a dict") is False
        assert is_blob(None) is False

    def test_descriptor_shape(self):
        d = blob_descriptor("gid1", mime="image/png", filename="x.png", size=42)
        assert d == {BLOB_KEY: {"id": "gid1", "mime": "image/png",
                                "filename": "x.png", "size": 42}}

    def test_descriptor_defaults_and_extra(self):
        d = blob_descriptor("g", mime="", filename="", size=0, extra={"page": 1})
        assert d[BLOB_KEY]["mime"] == "application/octet-stream"
        assert d[BLOB_KEY]["filename"] == "blob"
        assert d[BLOB_KEY]["page"] == 1

    def test_find_blobs_filters(self):
        items = [{"text": "a"}, {BLOB_KEY: {"id": "1"}}, {"text": "b"},
                 {BLOB_KEY: {"id": "2"}}]
        blobs = find_blobs(items)
        assert len(blobs) == 2
        assert all(is_blob(b) for b in blobs)


class TestPutGetBlob:
    @pytest.mark.asyncio
    async def test_put_blob_stores_and_returns_descriptor(self):
        fake_bucket = MagicMock()
        fake_bucket.upload_from_stream = AsyncMock(return_value="GRID123")
        with patch.object(blob_store, "_bucket", return_value=fake_bucket):
            desc = await put_blob(b"\x89PNG...", mime="image/png",
                                  filename="scan.png", execution_id="exec1")
        assert desc[BLOB_KEY]["id"] == "GRID123"
        assert desc[BLOB_KEY]["mime"] == "image/png"
        assert desc[BLOB_KEY]["filename"] == "scan.png"
        assert desc[BLOB_KEY]["size"] == len(b"\x89PNG...")
        # bytes + metadata (incl. execution_id) were passed to GridFS
        args, kwargs = fake_bucket.upload_from_stream.call_args
        assert args[0] == "scan.png"
        assert kwargs["metadata"]["execution_id"] == "exec1"

    @pytest.mark.asyncio
    async def test_put_blob_rejects_non_bytes(self):
        with pytest.raises(TypeError):
            await put_blob("a string", mime="text/plain")  # type: ignore

    @pytest.mark.asyncio
    async def test_get_blob_reads_bytes(self):
        stream = MagicMock()
        stream.read = AsyncMock(return_value=b"the-bytes")
        fake_bucket = MagicMock()
        fake_bucket.open_download_stream = AsyncMock(return_value=stream)
        with patch.object(blob_store, "_bucket", return_value=fake_bucket), \
             patch("bson.ObjectId", side_effect=lambda x: x):
            data = await get_blob({BLOB_KEY: {"id": "GRID123"}})
        assert data == b"the-bytes"

    @pytest.mark.asyncio
    async def test_get_blob_on_non_blob_raises(self):
        with pytest.raises(ValueError):
            await get_blob({"text": "not a blob"})
