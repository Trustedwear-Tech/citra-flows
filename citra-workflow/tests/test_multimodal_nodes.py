# Copyright (c) 2026 Trustedwear Tech Private Limited (https://citra-ai.com)
# Author: Rohit Kumar Chandan
# SPDX-License-Identifier: BUSL-1.1
#
# Licensed under the Business Source License 1.1. Non-production use is granted;
# production use requires a commercial licence until the Change Date, after
# which this file converts to Apache-2.0. See LICENSE at the repository root.

"""Tests for the multimodal nodes (Track 6): OCR, Audio-Transcribe, Vision-LLM.

The blob store and the OCR/Whisper/vision backends are mocked — these tests
verify the node plumbing: blob-in → backend call → text-out, and fail-loud on
a non-blob input.
"""
import sys
import os
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from citra_workflow.nodes import get_node, NodeContext
from citra_workflow.models import NodeType
from citra_workflow.blob_store import BLOB_KEY


def _img_item():
    return {BLOB_KEY: {"id": "g1", "mime": "image/png", "filename": "scan.png", "size": 9}}


def _audio_item():
    return {BLOB_KEY: {"id": "g2", "mime": "audio/mpeg", "filename": "clip.mp3", "size": 9}}


class TestBinarySourceEmitsBlobRef:
    @pytest.mark.asyncio
    async def test_unknown_binary_file_becomes_blob_not_base64(self):
        """Heavy/binary file content must travel as a {_blob} reference, not
        inline base64 in the item (which would bloat checkpoints / blow the
        16MB Mongo doc limit)."""
        from citra_workflow.nodes import sources
        raw = b"\x00\x01\x02BINARY" * 100
        ctx = NodeContext(node_id="s1", node_config={}, input_data={}, execution_id="e1")
        with patch("citra_workflow.blob_store.put_blob",
                   new=AsyncMock(return_value={BLOB_KEY: {"id": "gX", "mime": "application/octet-stream",
                                                          "filename": "weird.bin", "size": len(raw)}})):
            out = await sources._parse_file_by_type(raw, "binary", "weird.bin", ctx, "test")
        item = out["items"][0]
        assert BLOB_KEY in item                  # blob reference, not content_base64
        assert "content_base64" not in item
        assert out["meta"]["format"] == "blob"


class TestFileFetchSource:
    @pytest.mark.asyncio
    async def test_fetch_url_to_blob(self):
        node = get_node(NodeType.FILE_FETCH)
        ctx = NodeContext(node_id="ff1",
                          node_config={"url": "https://example.com/scan.png"},
                          input_data={}, user_id="u1", execution_id="e1")

        resp = MagicMock()
        resp.content = b"\x89PNG-bytes"
        resp.headers = {"content-type": "image/png"}
        resp.raise_for_status = MagicMock()
        client = MagicMock()
        client.get = AsyncMock(return_value=resp)
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)

        captured = {}

        async def _fake_put(self, ctx, data, *, mime, filename):
            captured.update(data=data, mime=mime, filename=filename)
            return {BLOB_KEY: {"id": "g9", "mime": mime, "filename": filename, "size": len(data)}}

        with patch("httpx.AsyncClient", return_value=client), \
             patch("citra_workflow.utils.ssrf.assert_url_is_public"), \
             patch.object(type(node), "put_blob", new=_fake_put):
            result = await node.execute(ctx)

        assert result["items"][0][BLOB_KEY]["id"] == "g9"
        assert captured["mime"] == "image/png"
        assert captured["filename"] == "scan.png"
        assert captured["data"] == b"\x89PNG-bytes"

    @pytest.mark.asyncio
    async def test_fetch_requires_url(self):
        node = get_node(NodeType.FILE_FETCH)
        ctx = NodeContext(node_id="ff2", node_config={}, input_data={})
        with pytest.raises(ValueError, match="URL"):
            await node.execute(ctx)


class TestOcrNode:
    @pytest.mark.asyncio
    async def test_ocr_blob_to_text(self):
        node = get_node(NodeType.OCR)
        ctx = NodeContext(node_id="ocr1", node_config={},
                          input_data={"items": [_img_item()]}, user_id="u1")
        with patch.object(type(node), "get_blob", new=AsyncMock(return_value=b"\x89PNG")), \
             patch.dict("sys.modules", {"qwen_ocr_proxy": MagicMock(
                 extract_text_from_image=MagicMock(return_value="INVOICE #42"))}):
            result = await node.execute(ctx)
        assert result["items"][0]["text"] == "INVOICE #42"
        assert result["items"][0]["format"] == "image"
        assert result["meta"]["count"] == 1

    @pytest.mark.asyncio
    async def test_ocr_from_url_field(self):
        """Image can arrive as a URL on any field (e.g. an API/source row) —
        the node fetches it, no blob required."""
        node = get_node(NodeType.OCR)
        ctx = NodeContext(node_id="ocrU", node_config={},
                          input_data={"items": [{"image_url": "https://ex.com/scan.png", "id": 7}]},
                          user_id="u1")
        with patch("citra_workflow.nodes.multimodal._fetch_url_bytes",
                   new=AsyncMock(return_value=(b"PNGBYTES", "image/png"))), \
             patch.dict("sys.modules", {"qwen_ocr_proxy": MagicMock(
                 extract_text_from_image=MagicMock(return_value="FROM URL"))}):
            result = await node.execute(ctx)
        assert result["items"][0]["text"] == "FROM URL"

    @pytest.mark.asyncio
    async def test_ocr_from_base64_field(self):
        """Image can arrive as base64 on any field (e.g. content_base64 from the
        file parser) — decoded locally, no blob/URL."""
        import base64 as _b64
        node = get_node(NodeType.OCR)
        payload = b"\x89PNG-real-bytes"
        ctx = NodeContext(node_id="ocrB", node_config={},
                          input_data={"items": [{"content_base64": _b64.b64encode(payload).decode()}]},
                          user_id="u1")
        captured = {}

        def _ocr(image_bytes, **kw):
            captured["bytes"] = image_bytes
            return "FROM B64"

        with patch.dict("sys.modules", {"qwen_ocr_proxy": MagicMock(extract_text_from_image=_ocr)}):
            result = await node.execute(ctx)
        assert result["items"][0]["text"] == "FROM B64"
        assert captured["bytes"] == payload  # decoded correctly

    @pytest.mark.asyncio
    async def test_ocr_explicit_media_field(self):
        """The user can pin which field holds the media."""
        node = get_node(NodeType.OCR)
        ctx = NodeContext(node_id="ocrF", node_config={"media_field": "scan_link"},
                          input_data={"items": [{"scan_link": "https://ex.com/a.jpg", "noise": "x"}]},
                          user_id="u1")
        with patch("citra_workflow.nodes.multimodal._fetch_url_bytes",
                   new=AsyncMock(return_value=(b"JPGBYTES", "image/jpeg"))), \
             patch.dict("sys.modules", {"qwen_ocr_proxy": MagicMock(
                 extract_text_from_image=MagicMock(return_value="PINNED"))}):
            result = await node.execute(ctx)
        assert result["items"][0]["text"] == "PINNED"

    @pytest.mark.asyncio
    async def test_ocr_fails_loud_when_no_media(self):
        node = get_node(NodeType.OCR)
        ctx = NodeContext(node_id="ocr2", node_config={},
                          input_data={"items": [{"text": "no media here"}]})
        with pytest.raises(ValueError, match="no resolvable media"):
            await node.execute(ctx)


class TestAudioTranscribeNode:
    @pytest.mark.asyncio
    async def test_audio_blob_to_text(self):
        node = get_node(NodeType.AUDIO_TRANSCRIBE)
        ctx = NodeContext(node_id="a1", node_config={},
                          input_data={"items": [_audio_item()]}, user_id="u1")
        with patch.object(type(node), "get_blob", new=AsyncMock(return_value=b"RIFF")), \
             patch.dict("sys.modules", {"whisper_proxy": MagicMock(
                 transcribe_audio=MagicMock(return_value="hello world"))}):
            result = await node.execute(ctx)
        assert result["items"][0]["text"] == "hello world"
        assert result["items"][0]["format"] == "audio"


class TestVisionLlmNode:
    @pytest.mark.asyncio
    async def test_vision_blob_to_answer(self):
        node = get_node(NodeType.VISION_LLM)
        ctx = NodeContext(node_id="v1", node_config={"prompt": "What is this?"},
                          input_data={"items": [_img_item()]}, user_id="u1")

        fake_msg = MagicMock()
        fake_msg.content = "A red car."
        fake_resp = MagicMock()
        fake_resp.choices = [MagicMock(message=fake_msg)]
        fake_client = MagicMock()
        fake_client.chat.completions.create = MagicMock(return_value=fake_resp)

        fake_citra_llm = MagicMock(
            get_vision_client=MagicMock(return_value=fake_client),
            get_vision_model=MagicMock(return_value="qwen-vl"),
            get_vision_extra_body=MagicMock(return_value={}),
        )
        with patch.object(type(node), "get_blob", new=AsyncMock(return_value=b"\x89PNG")), \
             patch.dict("sys.modules", {"citra_llm": fake_citra_llm}):
            result = await node.execute(ctx)

        assert result["items"][0]["answer"] == "A red car."
        assert result["items"][0]["format"] == "vision"
        # The image was sent as a multimodal image_url content block.
        _, kwargs = fake_client.chat.completions.create.call_args
        content = kwargs["messages"][0]["content"]
        assert any(c.get("type") == "image_url" for c in content)
        assert any(c.get("type") == "text" for c in content)

    @pytest.mark.asyncio
    async def test_mime_sniffed_from_bytes_overrides_wrong_descriptor(self):
        """The descriptor claims octet-stream, but the bytes are PNG — the node
        sniffs and sends the model the correct image/png data URL."""
        node = get_node(NodeType.VISION_LLM)
        png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
        item = {BLOB_KEY: {"id": "g", "mime": "application/octet-stream", "filename": "x"}}
        ctx = NodeContext(node_id="vS", node_config={"prompt": "what?"},
                          input_data={"items": [item]}, user_id="u1")
        fake_msg = MagicMock(); fake_msg.content = "ok"
        fake_resp = MagicMock(); fake_resp.choices = [MagicMock(message=fake_msg)]
        fake_client = MagicMock()
        fake_client.chat.completions.create = MagicMock(return_value=fake_resp)
        fake_llm = MagicMock(get_vision_client=MagicMock(return_value=fake_client),
                             get_vision_model=MagicMock(return_value="v"),
                             get_vision_extra_body=MagicMock(return_value={}))
        with patch.object(type(node), "get_blob", new=AsyncMock(return_value=png)), \
             patch.dict("sys.modules", {"citra_llm": fake_llm}):
            await node.execute(ctx)
        content = fake_client.chat.completions.create.call_args.kwargs["messages"][0]["content"]
        img = next(c for c in content if c.get("type") == "image_url")
        assert img["image_url"]["url"].startswith("data:image/png;base64,")  # sniffed, not octet-stream

    @pytest.mark.asyncio
    async def test_media_over_size_cap_fails_loud(self):
        node = get_node(NodeType.OCR)
        item = {BLOB_KEY: {"id": "g", "mime": "image/png", "filename": "big.png"}}
        ctx = NodeContext(node_id="ocrBig", node_config={},
                          input_data={"items": [item]}, user_id="u1")
        with patch("citra_workflow.nodes.multimodal.MAX_MEDIA_BYTES", 10), \
             patch.object(type(node), "get_blob", new=AsyncMock(return_value=b"x" * 100)):
            with pytest.raises(ValueError, match="over the"):
                await node.execute(ctx)

    @pytest.mark.asyncio
    async def test_vision_requires_prompt(self):
        node = get_node(NodeType.VISION_LLM)
        ctx = NodeContext(node_id="v2", node_config={},
                          input_data={"items": [_img_item()]})
        with pytest.raises(ValueError, match="[Pp]rompt"):
            await node.execute(ctx)
