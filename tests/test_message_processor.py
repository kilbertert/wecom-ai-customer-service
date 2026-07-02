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

    async def send(self, inbound, reply, trace=None):
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


# ---------------------------------------------------------------------------
# bot flow (trace + canned reply + conversation_id)
# ---------------------------------------------------------------------------


def _bot_inbound(msgid="bm1", msg_type="text", text="hi", **kw):
    base = dict(
        protocol="bot", msgid=msgid, msg_type=msg_type, text=text,
        user_id="bot_u", response_url="https://r/x",
    )
    base.update(kw)
    return InboundMessage(**base)


async def test_bot_text_flow_emits_trace_and_sends():
    proc, ai, wechat, media, conv = _make_processor()
    dedup = InMemoryDedupStore()
    adapter = _FakeAdapter(dedup)
    # adapter.send is _FakeAdapter.send (no trace rendering, just records)
    await proc.process(_bot_inbound(text="你好"), adapter)
    ai.run_workflow.assert_awaited_once()
    # bot scope = "bot"
    assert await conv.get("bot_u", "bot") is None  # ai returned no conversation_id
    assert len(adapter.sent) == 1


async def test_bot_conversation_id_persisted_under_bot_scope():
    proc, ai, wechat, media, conv = _make_processor()
    ai.run_workflow = AsyncMock(
        return_value={"content": "r", "text": "r", "conversation_id": "bot-conv-1"}
    )
    dedup = InMemoryDedupStore()
    adapter = _FakeAdapter(dedup)
    await proc.process(_bot_inbound(msgid="b1", text="第一轮"), adapter)
    assert await conv.get("bot_u", "bot") == "bot-conv-1"
    ai.run_workflow.reset_mock()
    await proc.process(_bot_inbound(msgid="b2", text="第二轮"), adapter)
    assert ai.run_workflow.await_args.kwargs["conversation_id"] == "bot-conv-1"


async def test_bot_unsupported_msg_type_sends_canned_reply():
    proc, ai, wechat, media, conv = _make_processor()
    dedup = InMemoryDedupStore()
    adapter = _FakeAdapter(dedup)
    await proc.process(_bot_inbound(msg_type="video", text=""), adapter)
    ai.run_workflow.assert_not_awaited()
    # canned reply sent
    assert len(adapter.sent) == 1
    assert "不支持" in adapter.sent[0][1].text


async def test_bot_empty_text_mixed_sends_canned_reply():
    proc, ai, wechat, media, conv = _make_processor()
    dedup = InMemoryDedupStore()
    adapter = _FakeAdapter(dedup)
    await proc.process(_bot_inbound(msg_type="mixed", text="", media_ref=""), adapter)
    ai.run_workflow.assert_not_awaited()
    assert "空消息" in adapter.sent[0][1].text


async def test_bot_image_url_uses_remote_url_no_download():
    proc, ai, wechat, media, conv = _make_processor()
    dedup = InMemoryDedupStore()
    adapter = _FakeAdapter(dedup)
    inbound = _bot_inbound(
        msg_type="image", text="",
        media_ref="https://cdn/x.jpg", media_kind="url",
    )
    await proc.process(inbound, adapter)
    wechat.download_media.assert_not_awaited()  # url 模式不下载
    sent_input = ai.run_workflow.await_args.args[0]
    assert sent_input["file_image_url"] == "https://cdn/x.jpg"
    assert "file_image_id" not in sent_input
    # bot image 无文本时给 hint
    assert sent_input["text"] == "[用户发了一张图片]"


async def test_bot_image_media_id_downloads_and_uploads_via_client():
    proc, ai, wechat, media, conv = _make_processor()
    # bot 用 ai.client.upload_file (不是 ai.upload_file)
    client = MagicMock()
    client.upload_file = AsyncMock(return_value="dify-img-uuid")
    ai.client = client
    dedup = InMemoryDedupStore()
    adapter = _FakeAdapter(dedup)
    inbound = _bot_inbound(
        msg_type="image", text="",
        media_ref="img_mid_1", media_kind="media_id",
    )
    await proc.process(inbound, adapter)
    wechat.download_media.assert_awaited_once_with("img_mid_1")
    client.upload_file.assert_awaited_once()
    sent_input = ai.run_workflow.await_args.args[0]
    assert sent_input["file_image_id"] == "dify-img-uuid"
    # 文件名带 wechat_image_ 前缀
    fname = client.upload_file.await_args.kwargs["filename"]
    assert fname.startswith("wechat_image_") and fname.endswith(".jpg")


async def test_bot_empty_ai_reply_uses_fallback():
    proc, ai, wechat, media, conv = _make_processor()
    ai.run_workflow = AsyncMock(return_value={"content": "", "text": ""})
    dedup = InMemoryDedupStore()
    adapter = _FakeAdapter(dedup)
    await proc.process(_bot_inbound(text="hi"), adapter)
    assert adapter.sent[0][1].text == "（AI 未返回内容）"


async def test_bot_does_not_notify_chatwoot():
    proc, ai, wechat, media, conv = _make_processor()
    dedup = InMemoryDedupStore()
    adapter = _FakeAdapter(dedup)
    with patch(
        "app.services.chatwoot_sync_service.ChatwootSyncService"
    ) as cw_cls:
        sync_mock = MagicMock()
        sync_mock.notify_incoming = AsyncMock()
        sync_mock.aclose = AsyncMock()
        cw_cls.return_value = sync_mock
        await proc.process(_bot_inbound(text="hi"), adapter)
    sync_mock.notify_incoming.assert_not_awaited()


# ---------------------------------------------------------------------------
# Chatwoot handoff (Phase 6)
# ---------------------------------------------------------------------------


async def test_handoff_skips_ai_when_human_takes_over(monkeypatch):
    """CHATWOOT_ENABLED + handoff=True → 跳过 AI, 不消耗 conversation_id。"""
    from app.core.config import settings

    monkeypatch.setattr(settings.chatwoot, "enabled", True)
    proc, ai, wechat, media, conv = _make_processor()
    ai.run_workflow = AsyncMock(
        return_value={"content": "r", "text": "r", "conversation_id": "conv-x"}
    )
    dedup = InMemoryDedupStore()
    adapter = _FakeAdapter(dedup)

    sync_mock = MagicMock()
    sync_mock.check_handoff = AsyncMock(return_value={"handoff": True})
    sync_mock.aclose = AsyncMock()
    with patch(
        "app.services.chatwoot_sync_service.ChatwootSyncService",
        return_value=sync_mock,
    ):
        await proc.process(_inbound(text="你好"), adapter)

    ai.run_workflow.assert_not_awaited()  # AI 被跳过
    assert adapter.sent == []  # 不发送回复 (人工接管)
    # conversation_id 不被消耗 (store 仍空)
    assert await conv.get("ext_u", "kf_1") is None
    sync_mock.check_handoff.assert_awaited_once_with("kf_1", "ext_u")


async def test_handoff_false_proceeds_to_ai(monkeypatch):
    """handoff=False → 正常调 AI。"""
    from app.core.config import settings

    monkeypatch.setattr(settings.chatwoot, "enabled", True)
    proc, ai, wechat, media, conv = _make_processor()
    dedup = InMemoryDedupStore()
    adapter = _FakeAdapter(dedup)

    sync_mock = MagicMock()
    sync_mock.check_handoff = AsyncMock(return_value={"handoff": False})
    sync_mock.aclose = AsyncMock()
    with patch(
        "app.services.chatwoot_sync_service.ChatwootSyncService",
        return_value=sync_mock,
    ):
        await proc.process(_inbound(text="你好"), adapter)

    ai.run_workflow.assert_awaited_once()
    assert len(adapter.sent) == 1


async def test_handoff_not_checked_when_chatwoot_disabled(monkeypatch):
    """CHATWOOT_ENABLED=false → 不调 check_handoff, 直接 AI。"""
    from app.core.config import settings

    monkeypatch.setattr(settings.chatwoot, "enabled", False)
    proc, ai, wechat, media, conv = _make_processor()
    dedup = InMemoryDedupStore()
    adapter = _FakeAdapter(dedup)

    sync_mock = MagicMock()
    sync_mock.check_handoff = AsyncMock(return_value={"handoff": True})
    sync_mock.notify_incoming = AsyncMock()
    sync_mock.aclose = AsyncMock()
    with patch(
        "app.services.chatwoot_sync_service.ChatwootSyncService",
        return_value=sync_mock,
    ):
        await proc.process(_inbound(text="你好"), adapter)
    # enabled=False → check_handoff 不应被调用
    sync_mock.check_handoff.assert_not_awaited()
    ai.run_workflow.assert_awaited_once()


async def test_handoff_not_checked_for_bot(monkeypatch):
    """bot 路径无 open_kfid, 不检查 handoff。"""
    from app.core.config import settings

    monkeypatch.setattr(settings.chatwoot, "enabled", True)
    proc, ai, wechat, media, conv = _make_processor()
    dedup = InMemoryDedupStore()
    adapter = _FakeAdapter(dedup)

    with patch(
        "app.services.chatwoot_sync_service.ChatwootSyncService"
    ) as cw_cls:
        await proc.process(_bot_inbound(text="你好"), adapter)
    cw_cls.assert_not_called()
    ai.run_workflow.assert_awaited_once()


async def test_handoff_check_failure_fails_open_to_ai(monkeypatch):
    """check_handoff 抛异常 → 默认不接管, 继续调 AI。"""
    from app.core.config import settings

    monkeypatch.setattr(settings.chatwoot, "enabled", True)
    proc, ai, wechat, media, conv = _make_processor()
    dedup = InMemoryDedupStore()
    adapter = _FakeAdapter(dedup)

    sync_mock = MagicMock()
    sync_mock.check_handoff = AsyncMock(side_effect=RuntimeError("chatwoot down"))
    sync_mock.aclose = AsyncMock()
    with patch(
        "app.services.chatwoot_sync_service.ChatwootSyncService",
        return_value=sync_mock,
    ):
        await proc.process(_inbound(text="你好"), adapter)

    ai.run_workflow.assert_awaited_once()
    # B2: fail-open 仍调 AI, 但应有 HANDOFF_FAIL_OPEN 告警日志 (可观测性)


# ---------------------------------------------------------------------------
# A1/B3 修复回归 (dedup 时序 + AI 异常脱敏)
# ---------------------------------------------------------------------------


async def test_send_failure_allows_retry():
    """A1: adapter.send 失败 → 不 mark_done → 微信重试可重新 acquire → AI 重跑。

    旧版 mark_done 在 AI 之前就调用, send 失败后重试会被 _processed 挡住, 丢消息。
    """
    proc, ai, wechat, media, conv = _make_processor()
    dedup = InMemoryDedupStore()
    adapter = _FakeAdapter(dedup)
    adapter.send_ok = False  # 第一次发送失败

    await proc.process(_inbound(msgid="retry1", text="你好"), adapter)
    # send 失败 → 没有 mark_done, msgid 应可重新 acquire
    assert await dedup.acquire("retry1", 300) is True


async def test_crash_during_processing_keeps_retryable():
    """A1/A2: 处理中抛未捕获异常 (非媒体失败) → msgid 不卡 _processed → 可重试。

    媒体失败被 _prepare_kf_input 捕获转 PreparedInput() (设计如此: 微信临时
    媒体过期, 重试无意义, 走 mark_done 丢弃)。本测试用 conversation_store 抛
    异常模拟 _prepare_input 之外的崩溃, 验证 except → release_processing 路径。
    """
    proc, ai, wechat, media, conv = _make_processor()
    conv.get = AsyncMock(side_effect=RuntimeError("conv store down"))
    dedup = InMemoryDedupStore()
    adapter = _FakeAdapter(dedup)

    await proc.process(_inbound(msgid="crash1", text="你好"), adapter)
    # 异常后未 mark_done → 重试可重新 acquire
    assert await dedup.acquire("crash1", 300) is True


async def test_cancelled_releases_processing():
    """A3: CancelledError (BaseException) 也释放 _processing, 允许重试。"""
    import asyncio as _asyncio

    proc, ai, wechat, media, conv = _make_processor()
    ai.run_workflow = AsyncMock(side_effect=_asyncio.CancelledError())
    dedup = InMemoryDedupStore()
    adapter = _FakeAdapter(dedup)

    with pytest.raises(_asyncio.CancelledError):
        await proc.process(_inbound(msgid="cancel1", text="你好"), adapter)

    # CancelledError 被重抛但 finally 已 release → 重试可 acquire
    assert await dedup.acquire("cancel1", 300) is True


async def test_processing_ttl_prevents_permanent_leak():
    """A4: _processing 带 TTL, acquire 时清理过期项 (不再永久泄漏)。"""
    import time as _time

    dedup = InMemoryDedupStore()
    # 模拟一个卡死的 _processing (手工塞入)
    async with dedup._lock:
        dedup._processing["stuck"] = _time.time() - 999  # 远过期
    # acquire 同一 msgid 应能成功 (过期 _processing 被清)
    assert await dedup.acquire("stuck", 300) is True


async def test_ai_exception_reply_is_sanitized():
    """B3: AI 失败时回复固定脱敏文案, 不含异常细节。"""
    proc, ai, wechat, media, conv = _make_processor()
    ai.run_workflow = AsyncMock(
        side_effect=RuntimeError("Dify workflow 500: outputs={secret:'x'}")
    )
    dedup = InMemoryDedupStore()
    adapter = _FakeAdapter(dedup)

    await proc.process(_inbound(msgid="err1", text="你好"), adapter)

    assert len(adapter.sent) == 1
    reply = adapter.sent[0][1].text
    assert "secret" not in reply  # 不泄露内部 outputs
    assert "500" not in reply  # 不泄露错误细节
    assert "不可用" in reply or "重试" in reply  # 脱敏兜底文案


async def test_bot_ai_exception_reply_is_sanitized():
    """B3: bot 路径 AI 失败同样脱敏。"""
    proc, ai, wechat, media, conv = _make_processor()
    ai.run_workflow = AsyncMock(side_effect=RuntimeError("internal: outputs={db_conn}"))
    dedup = InMemoryDedupStore()
    adapter = _FakeAdapter(dedup)

    await proc.process(_bot_inbound(msgid="berr1", text="你好"), adapter)

    assert len(adapter.sent) == 1
    reply = adapter.sent[0][1].text
    assert "db_conn" not in reply
    assert "不可用" in reply
