"""KfAdapter 单元测试 (在 WeChatService 边界 mock, 不发真实 HTTP)。

覆盖:
    - receive: XML 解密/验签/kf_msg_or_event/sync → InboundMessage 归一
    - send: 调 send_message_simple
    - build_sync_ack: 恒 "success"
    - verify_url: 验签 + 解密 echostr
    - 各失败分支返回空列表 (route 层统一回 success)
"""

from __future__ import annotations

import time
import xml.etree.ElementTree as ET
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.crypto import wecom_crypto
from app.models.wechat import WeChatMessage, MessageType
from app.protocols.base import InMemoryDedupStore, OutboundReply
from app.protocols.kf_adapter import KfAdapter

_AES_KEY = "abcdefghijklmnopqrstuvwxyz0123456789ABCDEFG"
_TOKEN = "test_token_value"
_CORP_ID = "wx_corp_id_123"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_svc():
    """构造一个 mock WeChatService, config 指向真实 token/aes_key/corp_id。"""
    svc = MagicMock()
    svc.config.kf_token = _TOKEN
    svc.config.kf_encoding_aes_key = _AES_KEY
    svc.config.corp_id = _CORP_ID
    # verify_signature / decrypt 走真实 wecom_crypto 逻辑 (经 WeChatService 委托)
    from app.crypto import wecom_crypto as wc

    svc.verify_signature = lambda sig, ts, nc, enc: wc.verify_signature(
        _TOKEN, ts, nc, enc, sig
    )
    svc.decrypt_message_custom = lambda enc, key, rid: wc.decrypt_message(enc, key, rid)
    svc.is_event_processed = AsyncMock(return_value=False)
    svc.sync_latest_messages = AsyncMock(return_value=[])
    svc.send_message_simple = AsyncMock(return_value={"errcode": 0})
    return svc


def _kf_event_xml(token: str, open_kfid: str) -> str:
    """构造一条解密后的 kf_msg_or_event 事件 XML, 再加密成 envelope。"""
    inner = (
        f"<xml><MsgType><![CDATA[event]]></MsgType>"
        f"<Event><![CDATA[kf_msg_or_event]]></Event>"
        f"<Token><![CDATA[{token}]]></Token>"
        f"<OpenKfId><![CDATA[{open_kfid}]]></OpenKfId></xml>"
    )
    ts = str(int(time.time()))
    envelope = wecom_crypto.encrypt_message(inner, _AES_KEY, _CORP_ID, ts, "n1", _TOKEN)
    return envelope


def _make_request(envelope_xml: str, ts: str, nonce: str):
    """构造一个最小的类 Request 对象 (async body + query_params)。"""

    async def body():
        return envelope_xml.encode("utf-8")

    return SimpleNamespace(body=body, query_params={"msg_signature": "", "timestamp": ts, "nonce": nonce})


def _sig(ts: str, nonce: str, encrypt: str) -> str:
    return wecom_crypto.compute_signature(_TOKEN, ts, nonce, encrypt)


def _msg(msgid="m1", msgtype=MessageType.TEXT, **kw):
    base = dict(
        msgid=msgid,
        msgtype=msgtype,
        send_time=1705254000,
        origin=1,
        external_userid="ext_user_1",
        open_kfid="kf_1",
    )
    base.update(kw)
    return WeChatMessage(**base)


# ---------------------------------------------------------------------------
# receive
# ---------------------------------------------------------------------------


async def test_receive_text_message_to_inbound():
    svc = _make_svc()
    svc.sync_latest_messages = AsyncMock(
        return_value=[_msg(text={"content": "你好"})]
    )
    adapter = KfAdapter(svc, InMemoryDedupStore())

    envelope = _kf_event_xml("sync_token_xyz", "kf_1")
    encrypt = ET.fromstring(envelope).findtext("Encrypt")
    ts, nonce = "1700000000", "nc1"
    sig = _sig(ts, nonce, encrypt)
    req = _make_request(envelope, ts, nonce)
    req.query_params["msg_signature"] = sig

    result = await adapter.receive(req)

    assert len(result) == 1
    inbound = result[0]
    assert inbound.protocol == "kf"
    assert inbound.msgid == "m1"
    assert inbound.msg_type == "text"
    assert inbound.text == "你好"
    assert inbound.user_id == "ext_user_1"
    assert inbound.open_kfid == "kf_1"
    # sync 用 sync_token + open_kfid, clear_cursor=True
    svc.sync_latest_messages.assert_awaited_once()
    kw = svc.sync_latest_messages.await_args.kwargs
    assert kw["sync_token"] == "sync_token_xyz"
    assert kw["open_kfid"] == "kf_1"
    assert kw["clear_cursor"] is True


async def test_receive_image_message_extracts_media_id():
    svc = _make_svc()
    svc.sync_latest_messages = AsyncMock(
        return_value=[_msg(msgtype=MessageType.IMAGE, image={"media_id": "img_mid_1"})]
    )
    adapter = KfAdapter(svc, InMemoryDedupStore())

    envelope = _kf_event_xml("tok", "kf_1")
    encrypt = ET.fromstring(envelope).findtext("Encrypt")
    ts, nonce = "1700000001", "nc2"
    req = _make_request(envelope, ts, nonce)
    req.query_params["msg_signature"] = _sig(ts, nonce, encrypt)

    result = await adapter.receive(req)
    assert result[0].msg_type == "image"
    assert result[0].media_ref == "img_mid_1"
    assert result[0].media_kind == "media_id"
    assert result[0].text == ""


async def test_receive_voice_message_extracts_media_id():
    svc = _make_svc()
    svc.sync_latest_messages = AsyncMock(
        return_value=[_msg(msgtype=MessageType.VOICE, voice={"media_id": "voice_mid_1"})]
    )
    adapter = KfAdapter(svc, InMemoryDedupStore())

    envelope = _kf_event_xml("tok", "kf_1")
    encrypt = ET.fromstring(envelope).findtext("Encrypt")
    ts, nonce = "1700000002", "nc3"
    req = _make_request(envelope, ts, nonce)
    req.query_params["msg_signature"] = _sig(ts, nonce, encrypt)

    result = await adapter.receive(req)
    assert result[0].msg_type == "voice"
    assert result[0].media_ref == "voice_mid_1"


async def test_receive_signature_failure_returns_empty():
    svc = _make_svc()
    adapter = KfAdapter(svc, InMemoryDedupStore())
    envelope = _kf_event_xml("tok", "kf_1")
    req = _make_request(envelope, "ts", "nc")
    req.query_params["msg_signature"] = "bogus_signature"
    assert await adapter.receive(req) == []
    svc.sync_latest_messages.assert_not_awaited()


async def test_receive_non_event_returns_empty():
    svc = _make_svc()
    adapter = KfAdapter(svc, InMemoryDedupStore())
    # 解密后 MsgType=text (非 event)
    inner = "<xml><MsgType><![CDATA[text]]></MsgType><Content><![CDATA[hi]]></Content></xml>"
    ts, nonce = "1700000003", "nc4"
    envelope = wecom_crypto.encrypt_message(inner, _AES_KEY, _CORP_ID, ts, nonce, _TOKEN)
    encrypt = ET.fromstring(envelope).findtext("Encrypt")
    req = _make_request(envelope, ts, nonce)
    req.query_params["msg_signature"] = _sig(ts, nonce, encrypt)
    assert await adapter.receive(req) == []


async def test_receive_no_messages_returns_empty():
    svc = _make_svc()
    svc.sync_latest_messages = AsyncMock(return_value=[])
    adapter = KfAdapter(svc, InMemoryDedupStore())
    envelope = _kf_event_xml("tok", "kf_1")
    encrypt = ET.fromstring(envelope).findtext("Encrypt")
    ts, nonce = "1700000004", "nc5"
    req = _make_request(envelope, ts, nonce)
    req.query_params["msg_signature"] = _sig(ts, nonce, encrypt)
    assert await adapter.receive(req) == []


async def test_receive_multiple_messages_returns_all_in_chronological_order():
    """A5: 一次回调多条客户消息全部派发, 按时间升序 (最旧在前)。

    sync_latest_messages 返回降序 (最新在前); receive 反转为升序, 让多轮
    conversation_id 按时间顺序续接。旧版只取 messages[0] 会丢弃其余。
    """
    svc = _make_svc()
    # 降序: 最新 m_newest (send_time 最大) 在前
    svc.sync_latest_messages = AsyncMock(
        return_value=[
            _msg(msgid="m_newest", text={"content": "第三条"}, send_time=1705254002),
            _msg(msgid="m_middle", text={"content": "第二条"}, send_time=1705254001),
            _msg(msgid="m_oldest", text={"content": "第一条"}, send_time=1705254000),
        ]
    )
    adapter = KfAdapter(svc, InMemoryDedupStore())
    envelope = _kf_event_xml("tok", "kf_1")
    encrypt = ET.fromstring(envelope).findtext("Encrypt")
    ts, nonce = "1700000004", "nc5b"
    req = _make_request(envelope, ts, nonce)
    req.query_params["msg_signature"] = _sig(ts, nonce, encrypt)

    result = await adapter.receive(req)

    assert [m.msgid for m in result] == ["m_oldest", "m_middle", "m_newest"]
    assert [m.text for m in result] == ["第一条", "第二条", "第三条"]


async def test_receive_skips_messages_without_msgid():
    """A5: 无 msgid 的脏数据无法 dedup, 跳过 (不混入返回列表)。"""
    svc = _make_svc()
    svc.sync_latest_messages = AsyncMock(
        return_value=[
            _msg(msgid="m1", text={"content": "ok"}, send_time=1705254001),
            _msg(msgid="", text={"content": "脏数据"}, send_time=1705254000),
        ]
    )
    adapter = KfAdapter(svc, InMemoryDedupStore())
    envelope = _kf_event_xml("tok", "kf_1")
    encrypt = ET.fromstring(envelope).findtext("Encrypt")
    ts, nonce = "1700000004", "nc5c"
    req = _make_request(envelope, ts, nonce)
    req.query_params["msg_signature"] = _sig(ts, nonce, encrypt)

    result = await adapter.receive(req)
    assert [m.msgid for m in result] == ["m1"]


async def test_receive_skips_non_allowed_kfid(monkeypatch):
    """settings.wechat.allowed_open_kfid 命中时跳过其他客服。"""
    from app.core.config import settings

    monkeypatch.setattr(settings.wechat, "allowed_open_kfid", "kf_allowed")
    svc = _make_svc()
    adapter = KfAdapter(svc, InMemoryDedupStore())
    envelope = _kf_event_xml("tok", "kf_OTHER")
    encrypt = ET.fromstring(envelope).findtext("Encrypt")
    ts, nonce = "1700000005", "nc6"
    req = _make_request(envelope, ts, nonce)
    req.query_params["msg_signature"] = _sig(ts, nonce, encrypt)
    assert await adapter.receive(req) == []
    svc.sync_latest_messages.assert_not_awaited()


# ---------------------------------------------------------------------------
# send / build_sync_ack
# ---------------------------------------------------------------------------


async def test_send_calls_send_message_simple():
    svc = _make_svc()
    adapter = KfAdapter(svc, InMemoryDedupStore())
    from app.protocols.base import InboundMessage

    inbound = InboundMessage(
        protocol="kf", msgid="m1", msg_type="text", text="x",
        user_id="ext_u", open_kfid="kf_1",
    )
    ok = await adapter.send(inbound, OutboundReply(text="回复内容"))
    assert ok is True
    svc.send_message_simple.assert_awaited_once_with("ext_u", "kf_1", "回复内容")


async def test_send_without_open_kfid_returns_false():
    svc = _make_svc()
    adapter = KfAdapter(svc, InMemoryDedupStore())
    from app.protocols.base import InboundMessage

    inbound = InboundMessage(protocol="kf", msgid="m1", msg_type="text", user_id="u")
    ok = await adapter.send(inbound, OutboundReply(text="x"))
    assert ok is False
    svc.send_message_simple.assert_not_awaited()


def test_build_sync_ack_returns_success():
    adapter = KfAdapter(_make_svc(), InMemoryDedupStore())
    assert adapter.build_sync_ack("ts", "nc", "ignored") == "success"


def test_dedup_property_returns_shared_store():
    store = InMemoryDedupStore()
    adapter = KfAdapter(_make_svc(), store)
    assert adapter.dedup is store


# ---------------------------------------------------------------------------
# verify_url
# ---------------------------------------------------------------------------


def test_verify_url_decrypts_echostr():
    svc = _make_svc()
    adapter = KfAdapter(svc, InMemoryDedupStore())
    plaintext_msg = "plain_echo_12345"
    ts, nonce = "1700000010", "ncv"
    envelope = wecom_crypto.encrypt_message(plaintext_msg, _AES_KEY, _CORP_ID, ts, nonce, _TOKEN)
    encrypt = ET.fromstring(envelope).findtext("Encrypt")
    sig = _sig(ts, nonce, encrypt)
    assert adapter.verify_url(sig, ts, nonce, encrypt) == plaintext_msg


def test_verify_url_bad_signature_returns_none():
    svc = _make_svc()
    adapter = KfAdapter(svc, InMemoryDedupStore())
    assert adapter.verify_url("bad", "ts", "nc", "enc") is None
