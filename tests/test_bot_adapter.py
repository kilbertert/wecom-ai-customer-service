"""BotAdapter 单元测试 (在 WeChatService 边界 mock, 不发真实 HTTP)。

覆盖:
    - receive: JSON 解密/验签 → text/image/voice/mixed 提取
    - send: POST response_url (markdown) + trace off/inline/separate
    - build_sync_ack: 加密 envelope
    - verify_url: 验签 + 解密 (receive_id="")
"""

from __future__ import annotations

import json
import time
import xml.etree.ElementTree as ET
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.crypto import wecom_crypto
from app.protocols.base import InMemoryDedupStore, OutboundReply
from app.protocols.bot_adapter import BotAdapter

_AES_KEY = "abcdefghijklmnopqrstuvwxyz0123456789ABCDEFG"
_TOKEN = "test_token_value"
_BOT_RECEIVE_ID = ""  # 企业自建


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_svc():
    svc = MagicMock()
    svc.config.kf_token = _TOKEN
    svc.config.kf_encoding_aes_key = _AES_KEY
    svc.config.corp_id = "ignored"
    svc.verify_bot_signature = lambda sig, ts, nc, body: wecom_crypto.verify_signature(
        _TOKEN, ts, nc, body, sig
    )
    svc.decrypt_message_custom = lambda enc, key, rid: wecom_crypto.decrypt_message(
        enc, key, rid
    )
    svc.encrypt_message_custom = staticmethod(
        lambda reply_xml, encoding_aes_key, corp_id, timestamp, nonce, token: (
            wecom_crypto.encrypt_message(
                reply_xml, encoding_aes_key, _BOT_RECEIVE_ID, timestamp, nonce, token
            )
        )
    )
    svc.download_media = AsyncMock(return_value=b"\x89PNG fake image bytes")
    return svc


def _encrypt_bot_msg(plaintext_json: str) -> str:
    """加密一条 bot 内层 JSON, 返回外层 {"encrypt": ...} 字符串。"""
    ts = str(int(time.time()))
    envelope = wecom_crypto.encrypt_message(
        plaintext_json, _AES_KEY, _BOT_RECEIVE_ID, ts, "n1", _TOKEN
    )
    encrypt = ET.fromstring(envelope).findtext("Encrypt")
    return json.dumps({"encrypt": encrypt})


def _make_request(outer_json: str, ts: str, nonce: str, sig: str = ""):
    async def body():
        return outer_json.encode("utf-8")

    return SimpleNamespace(
        body=body,
        query_params={"msg_signature": sig, "timestamp": ts, "nonce": nonce},
    )


def _sig(ts: str, nonce: str, encrypt: str) -> str:
    return wecom_crypto.compute_signature(_TOKEN, ts, nonce, encrypt)


def _post_body(outer_json: str, ts="1700000000", nonce="nc1"):
    encrypt = json.loads(outer_json)["encrypt"]
    return _make_request(outer_json, ts, nonce, sig=_sig(ts, nonce, encrypt))


# ---------------------------------------------------------------------------
# receive
# ---------------------------------------------------------------------------


async def test_receive_text_message():
    svc = _make_svc()
    adapter = BotAdapter(svc, InMemoryDedupStore())
    inner = json.dumps({
        "msgid": "bot-msg-1",
        "msgtype": "text",
        "from": {"userid": "bot_user_1"},
        "text": {"content": "你好"},
        "response_url": "https://qyapi.weixin.qq.com/cgi-bin/aibot/response?r=1",
        "chattype": "single",
    })
    result = await adapter.receive(_post_body(_encrypt_bot_msg(inner)))
    assert len(result) == 1
    ib = result[0]
    assert ib.protocol == "bot"
    assert ib.msgid == "bot-msg-1"
    assert ib.msg_type == "text"
    assert ib.text == "你好"
    assert ib.user_id == "bot_user_1"
    assert ib.response_url.startswith("https://")
    assert ib.chat_type == "single"
    assert ib.media_ref == ""


async def test_receive_image_url():
    svc = _make_svc()
    adapter = BotAdapter(svc, InMemoryDedupStore())
    inner = json.dumps({
        "msgid": "m2", "msgtype": "image",
        "from": {"userid": "u2"},
        "image": {"url": "https://cdn.weixin.qq.com/x.jpg"},
        "response_url": "https://r/x",
    })
    ib = (await adapter.receive(_post_body(_encrypt_bot_msg(inner))))[0]
    assert ib.msg_type == "image"
    assert ib.media_ref == "https://cdn.weixin.qq.com/x.jpg"
    assert ib.media_kind == "url"


async def test_receive_image_media_id():
    svc = _make_svc()
    adapter = BotAdapter(svc, InMemoryDedupStore())
    inner = json.dumps({
        "msgid": "m3", "msgtype": "image",
        "from": {"userid": "u3"},
        "image": {"media_id": "img_mid_99"},
        "response_url": "https://r/x",
    })
    ib = (await adapter.receive(_post_body(_encrypt_bot_msg(inner))))[0]
    assert ib.media_ref == "img_mid_99"
    assert ib.media_kind == "media_id"


async def test_receive_voice_media_id():
    svc = _make_svc()
    adapter = BotAdapter(svc, InMemoryDedupStore())
    inner = json.dumps({
        "msgid": "m4", "msgtype": "voice",
        "from": {"userid": "u4"},
        "voice": {"media_id": "voice_mid_99"},
        "response_url": "https://r/x",
    })
    ib = (await adapter.receive(_post_body(_encrypt_bot_msg(inner))))[0]
    assert ib.msg_type == "voice"
    assert ib.media_ref == "voice_mid_99"
    assert ib.media_kind == "media_id"


async def test_receive_mixed_text_plus_image():
    svc = _make_svc()
    adapter = BotAdapter(svc, InMemoryDedupStore())
    inner = json.dumps({
        "msgid": "m5", "msgtype": "mixed",
        "from": {"userid": "u5"},
        "mixed": {"msg_item": [
            {"msgtype": "text", "text": {"content": "看这张"}},
            {"msgtype": "image", "image": {"url": "https://cdn/x.png"}},
        ]},
        "response_url": "https://r/x",
    })
    ib = (await adapter.receive(_post_body(_encrypt_bot_msg(inner))))[0]
    assert ib.msg_type == "mixed"
    assert ib.text == "看这张"
    assert ib.media_ref == "https://cdn/x.png"
    assert ib.media_kind == "url"


async def test_receive_group_chat_type():
    svc = _make_svc()
    adapter = BotAdapter(svc, InMemoryDedupStore())
    inner = json.dumps({
        "msgid": "m6", "msgtype": "text", "chattype": "group",
        "from": {"userid": "u6"}, "text": {"content": "hi"},
        "response_url": "https://r/x",
    })
    ib = (await adapter.receive(_post_body(_encrypt_bot_msg(inner))))[0]
    assert ib.chat_type == "group"


async def test_receive_bad_signature_returns_empty():
    svc = _make_svc()
    adapter = BotAdapter(svc, InMemoryDedupStore())
    outer = _encrypt_bot_msg(json.dumps({
        "msgid": "x", "msgtype": "text",
        "from": {"userid": "u"}, "text": {"content": "hi"},
        "response_url": "https://r/x",
    }))
    req = _make_request(outer, "ts", "nc", sig="bogus")
    assert await adapter.receive(req) == []


async def test_receive_bad_json_returns_empty():
    svc = _make_svc()
    adapter = BotAdapter(svc, InMemoryDedupStore())
    req = _make_request("not json{", "ts", "nc", sig="x")
    assert await adapter.receive(req) == []


# ---------------------------------------------------------------------------
# build_sync_ack
# ---------------------------------------------------------------------------


def test_build_sync_ack_returns_encrypted_envelope():
    svc = _make_svc()
    adapter = BotAdapter(svc, InMemoryDedupStore())
    envelope = adapter.build_sync_ack("1700000123", "nonce_abc")
    obj = json.loads(envelope)
    assert "encrypt" in obj and obj["encrypt"]
    assert obj["timestamp"] == "1700000123"
    assert obj["nonce"] == "nonce_abc"
    assert "msgsignature" in obj
    # 解密 encrypt 应得到 markdown 信封
    plaintext = wecom_crypto.decrypt_message(
        obj["encrypt"], _AES_KEY, _BOT_RECEIVE_ID
    )
    inner = json.loads(plaintext)
    assert inner["msgtype"] == "markdown"
    assert "content" in inner["markdown"]


def test_build_sync_ack_custom_text():
    svc = _make_svc()
    adapter = BotAdapter(svc, InMemoryDedupStore())
    envelope = adapter.build_sync_ack("1", "n", text="自定义占位")
    obj = json.loads(envelope)
    plaintext = wecom_crypto.decrypt_message(obj["encrypt"], _AES_KEY, _BOT_RECEIVE_ID)
    assert json.loads(plaintext)["markdown"]["content"] == "自定义占位"


# ---------------------------------------------------------------------------
# send + trace 模式回归
# ---------------------------------------------------------------------------


async def _mock_post_ok():
    """httpx.AsyncClient.post 的 mock, 返回 errcode=0。"""
    resp = MagicMock()
    resp.status_code = 200
    resp.text = '{"errcode":0}'
    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    client.post = AsyncMock(return_value=resp)
    return client


async def test_send_posts_markdown_to_response_url():
    svc = _make_svc()
    adapter = BotAdapter(svc, InMemoryDedupStore())
    from app.protocols.base import InboundMessage

    inbound = InboundMessage(
        protocol="bot", msgid="m", msg_type="text", text="x",
        user_id="u", response_url="https://r/x",
    )
    client = await _mock_post_ok()
    with patch("httpx.AsyncClient", return_value=client):
        ok = await adapter.send(inbound, OutboundReply(text="回复"))
    assert ok is True
    payload = client.post.await_args.kwargs["json"]
    assert payload["msgtype"] == "markdown"
    assert payload["markdown"]["content"] == "回复"


async def test_send_without_response_url_returns_false():
    svc = _make_svc()
    adapter = BotAdapter(svc, InMemoryDedupStore())
    from app.protocols.base import InboundMessage

    inbound = InboundMessage(protocol="bot", msgid="m", msg_type="text", user_id="u")
    ok = await adapter.send(inbound, OutboundReply(text="x"))
    assert ok is False


async def test_send_trace_off_does_not_append():
    svc = _make_svc()
    adapter = BotAdapter(svc, InMemoryDedupStore())
    from app.protocols.base import InboundMessage
    from app.services.bot_trace import BotTrace

    trace = BotTrace(chat_type="single", msg_type="text")
    trace.event("receive", "ok", "from=u")
    inbound = InboundMessage(
        protocol="bot", msgid="m", msg_type="text", text="x",
        user_id="u", response_url="https://r/x",
    )
    client = await _mock_post_ok()
    with patch("httpx.AsyncClient", return_value=client):
        await adapter.send(inbound, OutboundReply(text="主回复"), trace=trace)
    content = client.post.await_args.kwargs["json"]["markdown"]["content"]
    assert content == "主回复"  # trace_mode=off, 不附加


async def test_send_trace_inline_appends_to_content(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings.app, "bot_trace_mode", "inline")
    svc = _make_svc()
    adapter = BotAdapter(svc, InMemoryDedupStore())
    from app.protocols.base import InboundMessage
    from app.services.bot_trace import BotTrace

    trace = BotTrace(chat_type="single", msg_type="text")
    trace.event("receive", "ok", "from=u")
    trace.event("ai", "ok", "text=3字")
    inbound = InboundMessage(
        protocol="bot", msgid="m", msg_type="text", text="x",
        user_id="u", response_url="https://r/x",
    )
    client = await _mock_post_ok()
    with patch("httpx.AsyncClient", return_value=client):
        await adapter.send(inbound, OutboundReply(text="主回复"), trace=trace)
    content = client.post.await_args.kwargs["json"]["markdown"]["content"]
    assert content.startswith("主回复")
    assert "接收" in content and "AI" in content  # trace 块已附加
    # inline 模式只 POST 一次
    assert client.post.await_count == 1


async def test_send_trace_separate_posts_twice(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings.app, "bot_trace_mode", "separate")
    svc = _make_svc()
    adapter = BotAdapter(svc, InMemoryDedupStore())
    from app.protocols.base import InboundMessage
    from app.services.bot_trace import BotTrace

    trace = BotTrace(chat_type="single", msg_type="text")
    trace.event("receive", "ok", "from=u")
    trace.event("push", "ok", "HTTP 200")
    inbound = InboundMessage(
        protocol="bot", msgid="m", msg_type="text", text="x",
        user_id="u", response_url="https://r/x",
    )

    resp = MagicMock()
    resp.status_code = 200
    resp.text = '{"errcode":0}'
    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    client.post = AsyncMock(return_value=resp)
    with patch("httpx.AsyncClient", return_value=client):
        await adapter.send(inbound, OutboundReply(text="主回复"), trace=trace)

    # 主消息 + trace 各一次
    assert client.post.await_count == 2
    first = client.post.await_args_list[0].kwargs["json"]["markdown"]["content"]
    assert first == "主回复"  # separate 模式主消息不含 trace
    second = client.post.await_args_list[1].kwargs["json"]["markdown"]["content"]
    assert "接收" in second  # 第二次是 trace


# ---------------------------------------------------------------------------
# verify_url
# ---------------------------------------------------------------------------


def test_verify_url_decrypts_echostr():
    svc = _make_svc()
    adapter = BotAdapter(svc, InMemoryDedupStore())
    plaintext_msg = "bot_echo_plain_42"
    ts, nonce = "1700000200", "ncv"
    envelope = wecom_crypto.encrypt_message(
        plaintext_msg, _AES_KEY, _BOT_RECEIVE_ID, ts, nonce, _TOKEN
    )
    encrypt = ET.fromstring(envelope).findtext("Encrypt")
    sig = _sig(ts, nonce, encrypt)
    assert adapter.verify_url(sig, ts, nonce, encrypt) == plaintext_msg


def test_verify_url_bad_signature_returns_none():
    svc = _make_svc()
    adapter = BotAdapter(svc, InMemoryDedupStore())
    assert adapter.verify_url("bad", "ts", "nc", "enc") is None


def test_dedup_property_returns_shared_store():
    store = InMemoryDedupStore()
    adapter = BotAdapter(_make_svc(), store)
    assert adapter.dedup is store
    assert adapter.dedup_ttl == 600
