"""MessageProcessor 单元测试 (在 adapter / wechat / ai 边界 mock)。

覆盖 KF 编排主干:
    dedup → 媒体 → conversation_id get/save → run_workflow → compose → send → chatwoot
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.protocols.base import (
    InboundMessage,
    InMemoryDedupStore,
    OutboundReply,
    ProtocolAdapter,
)
from app.services.conversation_store import InMemoryConversationStore
from app.services.message_processor import MessageProcessor


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class _FakeAdapter(ProtocolAdapter):
    """协议无关测试替身, 记录 send 调用。"""

    dedup_ttl = 300

    def __init__(self, dedup):
        self._dedup = dedup
        self.sent: list = []
        self.send_ok = True

    @property
    def dedup(self):
        return self._dedup

    async def receive(self, request):
        return []

    async def send(self, inbound, reply):
        self.sent.append((inbound, reply))
        return self.send_ok

    def build_sync_ack(self, timestamp, nonce, text=""):
        return "success"


def _inbound(msgid="m1", msg_type="text", text="hi", **kw):
    base = dict(
        protocol="kf", msgid=msgid, msg_type=msg_type, text=text,
        user_id="ext_u", open_kfid="kf_1",
    )
    base.update(kw)
    return InboundMessage(**base)


def _make_processor(ai=None, wechat=None, media=None, conv=None):
    ai = ai or MagicMock()
    ai.run_workflow = AsyncMock(return_value={"content": "AI回复", "text": "AI回复"})
    ai.upload_file = AsyncMock(return_value="dify_file_id_x")
    wechat = wechat or MagicMock()
    wechat.download_media = AsyncMock(return_value=b"\x89PNG bytes")
    media = media or MagicMock()
    media.download_and_process_media = AsyncMock(return_value={"error": None, "converted": False})
    conv = conv or InMemoryConversationStore()
    return MessageProcessor(wechat, media, ai, conv), ai, wechat, media, conv


# ---------------------------------------------------------------------------
# text flow
# ---------------------------------------------------------------------------


async def test_text_flow_end_to_end():
    proc, ai, wechat, media, conv = _make_processor()
    dedup = InMemoryDedupStore()
    adapter = _FakeAdapter(dedup)

    await proc.process(_inbound(text="你好"), adapter)

    ai.run_workflow.assert_awaited_once()
    kw = ai.run_workflow.await_args.kwargs
    args = ai.run_workflow.await_args.args
    assert kw["user_id"] == "ext_u"
    assert kw["conversation_id"] is None  # 首轮
    assert args[0]["text"] == "你好"
    # send 被调用, 文本来自 compose_multimodal_markdown (命中 content 字段)
    assert len(adapter.sent) == 1
    assert "AI回复" in adapter.sent[0][1].text


async def test_conversation_id_persisted_and_reused():
    proc, ai, wechat, media, conv = _make_processor()
    ai.run_workflow = AsyncMock(
        return_value={"content": "r", "text": "r", "conversation_id": "conv-abc"}
    )
    dedup = InMemoryDedupStore()
    adapter = _FakeAdapter(dedup)

    # 第一轮: 返回 conv-abc, 持久化
    await proc.process(_inbound(msgid="m1", text="第一轮"), adapter)
    assert await conv.get("ext_u", "kf_1") == "conv-abc"

    # 第二轮: 应读出 conv-abc 透传给 run_workflow
    ai.run_workflow.reset_mock()
    await proc.process(_inbound(msgid="m2", text="第二轮"), adapter)
    kw = ai.run_workflow.await_args.kwargs
    assert kw["conversation_id"] == "conv-abc"


async def test_duplicate_msgid_skipped():
    proc, ai, wechat, media, conv = _make_processor()
    dedup = InMemoryDedupStore()
    adapter = _FakeAdapter(dedup)

    await proc.process(_inbound(msgid="dup", text="x"), adapter)
    await proc.process(_inbound(msgid="dup", text="x"), adapter)

    # 只调一次 AI
    assert ai.run_workflow.await_count == 1
    assert len(adapter.sent) == 1


async def test_empty_text_skips_workflow():
    proc, ai, wechat, media, conv = _make_processor()
    dedup = InMemoryDedupStore()
    adapter = _FakeAdapter(dedup)

    await proc.process(_inbound(text=""), adapter)
    ai.run_workflow.assert_not_awaited()
    assert adapter.sent == []


# ---------------------------------------------------------------------------
# image flow
# ---------------------------------------------------------------------------


async def test_image_flow_downloads_and_uploads():
    proc, ai, wechat, media, conv = _make_processor()
    dedup = InMemoryDedupStore()
    adapter = _FakeAdapter(dedup)

    inbound = _inbound(msg_type="image", text="", media_ref="img_mid", media_kind="media_id")
    await proc.process(inbound, adapter)

    wechat.download_media.assert_awaited_once_with("img_mid")
    ai.upload_file.assert_awaited_once()
    args = ai.upload_file.await_args.args
    assert args[0] == b"\x89PNG bytes"
    assert "wechat_image_img_mid.jpg" == args[1]
    sent_input = ai.run_workflow.await_args.args[0]
    assert sent_input["file_image_id"] == "dify_file_id_x"


async def test_image_download_failure_skips_send():
    proc, ai, wechat, media, conv = _make_processor()
    wechat.download_media = AsyncMock(side_effect=RuntimeError("404"))
    dedup = InMemoryDedupStore()
    adapter = _FakeAdapter(dedup)

    await proc.process(
        _inbound(msg_type="image", text="", media_ref="bad_mid", media_kind="media_id"),
        adapter,
    )
    ai.run_workflow.assert_not_awaited()
    assert adapter.sent == []


# ---------------------------------------------------------------------------
# voice flow
# ---------------------------------------------------------------------------


async def test_voice_flow_converted_wav():
    proc, ai, wechat, media, conv = _make_processor()
    media.download_and_process_media = AsyncMock(
        return_value={"error": None, "converted": True, "wav_path": "/tmp/v.wav"}
    )
    # patch aiofiles.open 读取 wav
    dedup = InMemoryDedupStore()
    adapter = _FakeAdapter(dedup)

    import sys
    fake_aiofiles = MagicMock()

    class _FC:
        async def __aenter__(self):
            self.f = MagicMock()
            self.f.read = AsyncMock(return_value=b"WAVBYTES")
            return self.f

        async def __aexit__(self, *a):
            return False

    fake_aiofiles.open = lambda *a, **k: _FC()
    with patch.dict(sys.modules, {"aiofiles": fake_aiofiles}):
        await proc.process(
            _inbound(msg_type="voice", text="", media_ref="v_mid", media_kind="media_id"),
            adapter,
        )

    sent_input = ai.run_workflow.await_args.args[0]
    assert sent_input["file_voice_id"] == "dify_file_id_x"
    # 上传的是 wav 字节 + wav 文件名
    args = ai.upload_file.await_args.args
    assert args[0] == b"WAVBYTES"
    assert args[1].endswith(".wav")


async def test_voice_transcode_failure_falls_back_to_amr():
    proc, ai, wechat, media, conv = _make_processor()
    media.download_and_process_media = AsyncMock(return_value={"error": "no ffmpeg"})
    dedup = InMemoryDedupStore()
    adapter = _FakeAdapter(dedup)

    await proc.process(
        _inbound(msg_type="voice", text="", media_ref="v_mid", media_kind="media_id"),
        adapter,
    )
    args = ai.upload_file.await_args.args
    assert args[0] == b"\x89PNG bytes"  # voice_content (原始)
    assert args[1].endswith(".amr")


# ---------------------------------------------------------------------------
# unsupported / chatwoot
# ---------------------------------------------------------------------------


async def test_unsupported_msg_type_skips():
    proc, ai, wechat, media, conv = _make_processor()
    dedup = InMemoryDedupStore()
    adapter = _FakeAdapter(dedup)

    await proc.process(_inbound(msg_type="video", text=""), adapter)
    ai.run_workflow.assert_not_awaited()
    assert adapter.sent == []


async def test_chatwoot_notify_called_after_send_for_kf():
    proc, ai, wechat, media, conv = _make_processor()
    dedup = InMemoryDedupStore()
    adapter = _FakeAdapter(dedup)

    sync_mock = MagicMock()
    sync_mock.notify_incoming = AsyncMock(return_value=True)
    sync_mock.aclose = AsyncMock()
    with patch(
        "app.services.chatwoot_sync_service.ChatwootSyncService",
        return_value=sync_mock,
    ):
        await proc.process(_inbound(text="hi"), adapter)

    sync_mock.notify_incoming.assert_awaited_once()
    payload = sync_mock.notify_incoming.await_args.kwargs
    assert payload["open_kfid"] == "kf_1"
    assert payload["external_userid"] == "ext_u"
    assert payload["message_data"]["origin"] == 2


async def test_send_failure_skips_chatwoot():
    proc, ai, wechat, media, conv = _make_processor()
    dedup = InMemoryDedupStore()
    adapter = _FakeAdapter(dedup)
    adapter.send_ok = False

    with patch(
        "app.services.chatwoot_sync_service.ChatwootSyncService"
    ) as cw_cls:
        sync_mock = MagicMock()
        sync_mock.notify_incoming = AsyncMock(return_value=True)
        sync_mock.aclose = AsyncMock()
        cw_cls.return_value = sync_mock
        await proc.process(_inbound(text="hi"), adapter)

    # send 失败 → 不应通知 chatwoot
    sync_mock.notify_incoming.assert_not_awaited()


async def test_empty_workflow_result_skips_send():
    proc, ai, wechat, media, conv = _make_processor()
    ai.run_workflow = AsyncMock(return_value={"content": "", "text": ""})
    dedup = InMemoryDedupStore()
    adapter = _FakeAdapter(dedup)

    await proc.process(_inbound(text="hi"), adapter)
    assert adapter.sent == []
