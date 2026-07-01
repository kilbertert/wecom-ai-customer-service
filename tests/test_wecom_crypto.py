"""WeComCrypto 纯函数加解密 + 签名 round-trip 测试。"""

from __future__ import annotations

import time

import pytest

from app.crypto import wecom_crypto

# 43 字符的标准 EncodingAESKey (base64 可解码为 32 字节)
_AES_KEY = "abcdefghijklmnopqrstuvwxyz0123456789ABCDEFG"
_TOKEN = "test_token_value"
_CORP_ID = "wx_corp_id_123"
_BOT_RECEIVE_ID = ""  # 企业自建智能机器人 receive_id 为空串


def test_signature_is_sha1_of_sorted_params():
    sig = wecom_crypto.compute_signature(_TOKEN, "1577800000", "nonce123", "encryptB64")
    # 与手算一致: SHA1(sort([token, ts, nonce, encrypt]))
    import hashlib

    params = sorted([_TOKEN, "1577800000", "nonce123", "encryptB64"])
    expected = hashlib.sha1("".join(params).encode("utf-8")).hexdigest()
    assert sig == expected


def test_verify_signature_matches():
    sig = wecom_crypto.compute_signature(_TOKEN, "ts", "nc", "enc")
    assert wecom_crypto.verify_signature(_TOKEN, "ts", "nc", "enc", sig) is True


def test_verify_signature_rejects_tamper():
    sig = wecom_crypto.compute_signature(_TOKEN, "ts", "nc", "enc")
    assert wecom_crypto.verify_signature(_TOKEN, "ts", "nc", "TAMPERED", sig) is False
    assert wecom_crypto.verify_signature(_TOKEN, "ts", "nc", "enc", "0" * 40) is False


def test_encrypt_decrypt_roundtrip_kf_xml():
    """KF 场景: receive_id = corp_id, 明文是 XML。"""
    plaintext = "<xml><Content><![CDATA[hello]]></Content></xml>"
    ts = str(int(time.time()))
    nonce = "nonce_kf"

    envelope = wecom_crypto.encrypt_message(plaintext, _AES_KEY, _CORP_ID, ts, nonce, _TOKEN)
    assert "<Encrypt>" in envelope and "<MsgSignature>" in envelope

    # 从 envelope 取出 Encrypt 字段
    import xml.etree.ElementTree as ET

    encrypt = ET.fromstring(envelope).findtext("Encrypt")
    assert encrypt

    decrypted = wecom_crypto.decrypt_message(encrypt, _AES_KEY, _CORP_ID)
    assert decrypted == plaintext


def test_encrypt_decrypt_roundtrip_bot_empty_receive_id():
    """智能机器人场景: receive_id = "" (企业自建), 明文是 JSON。"""
    plaintext = '{"msgtype":"text","text":{"content":"hi"}}'
    ts = str(int(time.time()))
    nonce = "nonce_bot"

    envelope = wecom_crypto.encrypt_message(plaintext, _AES_KEY, _BOT_RECEIVE_ID, ts, nonce, _TOKEN)
    import xml.etree.ElementTree as ET

    encrypt = ET.fromstring(envelope).findtext("Encrypt")
    decrypted = wecom_crypto.decrypt_message(encrypt, _AES_KEY, _BOT_RECEIVE_ID)
    assert decrypted == plaintext


def test_decrypt_receive_id_mismatch_still_returns_content():
    """历史行为: receive_id 不匹配时仍返回消息内容 (兼容企业自建机器人)。"""
    plaintext = "<xml><Content>msg</Content></xml>"
    envelope = wecom_crypto.encrypt_message(plaintext, _AES_KEY, _CORP_ID, "1", "n", _TOKEN)
    import xml.etree.ElementTree as ET

    encrypt = ET.fromstring(envelope).findtext("Encrypt")
    # 用错误的 receive_id 解密, 仍应返回明文
    decrypted = wecom_crypto.decrypt_message(encrypt, _AES_KEY, "WRONG_CORP_ID")
    assert decrypted == plaintext


def test_invalid_aes_key_length_raises():
    with pytest.raises(ValueError):
        wecom_crypto.encrypt_message("x", "too_short", _CORP_ID, "1", "n", _TOKEN)
    with pytest.raises(ValueError):
        wecom_crypto.decrypt_message("enc", "too_short", _CORP_ID)


def test_wechat_service_delegates_to_crypto():
    """WeChatService 的静态方法应薄委托到 wecom_crypto (过渡期兼容)。"""
    from app.services.wechat import WeChatService

    plaintext = "<xml><X>delegate test</X></xml>"
    envelope = WeChatService.encrypt_message_custom(plaintext, _AES_KEY, _CORP_ID, "1", "n", _TOKEN)
    import xml.etree.ElementTree as ET

    encrypt = ET.fromstring(envelope).findtext("Encrypt")
    assert WeChatService.decrypt_message_custom(encrypt, _AES_KEY, _CORP_ID) == plaintext
