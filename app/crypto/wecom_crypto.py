"""企业微信加解密与签名工具 (纯函数, 无 I/O, 无副作用)。

从 ``WeChatService`` 的 ``decrypt_message_custom`` / ``encrypt_message_custom``
/ 签名验证逻辑提取而来, 消除 3 处 SHA1 签名复制, 并去掉 ``print`` 调试输出。

协议要点 (企业微信客服 KF 与智能机器人共用同一套 AES-CBC + SHA1 算法):
    - AES key = base64decode(encoding_aes_key + "=")  # 43 字符 → 32 字节
    - IV      = key[:16]
    - 明文结构: 16 字节随机串 + 4 字节大端消息长度 + 消息内容 + receive_id(corp_id)
    - PKCS7 填充到 32 字节边界
    - 签名: SHA1( sort([token, timestamp, nonce, encrypt_b64]) )

KF 与智能机器人的唯一差异是 receive_id: KF 用 corp_id, 企业自建智能机器人用空串 ""。
本模块对 receive_id 不做校验拦截 (历史实现: 不匹配时仍返回内容用于调试), 仅记录 debug 日志。
"""

from __future__ import annotations

import base64
import hashlib
import logging
import struct
import traceback

from Crypto.Cipher import AES

logger = logging.getLogger(__name__)

# PKCS7 填充块大小 (企业微信固定 32)
_PKCS7_BLOCK = 32


def compute_signature(token: str, timestamp: str, nonce: str, encrypt: str) -> str:
    """计算企业微信回调签名: SHA1(sort([token, timestamp, nonce, encrypt]))。

    对 KF URL 验证 (encrypt=echostr)、KF 消息 (encrypt=Encrypt 字段)、
    智能机器人消息 (encrypt=encrypt 字段) 三种场景通用。
    """
    params = sorted([token, timestamp, nonce, encrypt])
    tmp_str = "".join(params)
    return hashlib.sha1(tmp_str.encode("utf-8")).hexdigest()


def verify_signature(
    token: str, timestamp: str, nonce: str, encrypt: str, signature: str
) -> bool:
    """验证企业微信回调签名。"""
    try:
        return compute_signature(token, timestamp, nonce, encrypt) == signature
    except Exception as e:
        logger.error("签名验证异常: %s", e)
        return False


def decrypt_message(encrypted_msg: str, encoding_aes_key: str, receive_id: str) -> str:
    """AES-256-CBC 解密企业微信消息体, 返回明文字符串。

    Args:
        encrypted_msg: base64 编码的密文
        encoding_aes_key: 43 字符的 EncodingAESKey
        receive_id: KF 传 corp_id; 企业自建智能机器人传空串 ""

    Returns:
        解密后的明文 (KF 通常是 XML, 智能机器人通常是 JSON, URL 验证时是随机串)。

    与历史 ``WeChatService.decrypt_message_custom`` 行为一致:
        - receive_id 不匹配时仍返回消息内容 (仅 debug 日志), 兼容企业自建机器人空串。
        - 结构化解析失败时回退到 "直接查找 <xml> 片段"。
    """
    logger.debug("开始解密, 加密消息长度=%d", len(encrypted_msg))

    # 1. 准备 AES Key
    if len(encoding_aes_key) != 43:
        raise ValueError(
            f"EncodingAESKey应该是43位，实际是{len(encoding_aes_key)}位"
        )
    key = base64.b64decode(encoding_aes_key + "=")
    iv = key[:16]

    # 2. Base64 解码
    encrypted_data = base64.b64decode(encrypted_msg)

    # 3. AES-256-CBC 解密
    cipher = AES.new(key, AES.MODE_CBC, iv)
    decrypted = cipher.decrypt(encrypted_data)

    # 4. PKCS7 去填充 (pad 字节无效时保留原文, 走回退路径)
    try:
        pad = decrypted[-1]
        logger.debug("最后字节（可能为填充）: %s", pad)
        if 1 <= pad <= 32:
            result = decrypted[:-pad]
            logger.debug("去除%d字节填充", pad)
        else:
            logger.debug("填充字节无效，保留原文走回退")
            result = decrypted
    except Exception as e:
        logger.debug("去填充失败: %s", e)
        result = decrypted

    # 5. 解析企业微信结构: 16 随机 + 4 长度 + 消息 + receive_id
    try:
        if len(result) >= 20:
            msg_len = struct.unpack(">I", result[16:20])[0]
            logger.debug("消息长度字段: %d字节", msg_len)

            if 20 + msg_len <= len(result):
                msg_content = result[20 : 20 + msg_len]
                receive_id_from_msg = result[20 + msg_len :]
                logger.debug(
                    "提取的消息长度: %d字节, receive_id长度: %d字节",
                    len(msg_content),
                    len(receive_id_from_msg),
                )
                try:
                    msg_str = msg_content.decode("utf-8")
                    receive_id_str = receive_id_from_msg.decode("utf-8")
                    logger.debug("提取的receive_id: %s", receive_id_str)

                    if receive_id_str != receive_id:
                        # 历史行为: 不匹配仍返回内容 (兼容企业自建机器人 receive_id="",
                        # 以及 KF 场景下可能的填充差异)
                        logger.debug(
                            "receive_id不匹配: 期望=%s, 实际=%s",
                            receive_id,
                            receive_id_str,
                        )
                    return msg_str
                except UnicodeDecodeError:
                    logger.debug("UTF-8解码失败，尝试其他编码")
            else:
                logger.debug(
                    "数据长度不足: 需要%d字节，实际%d字节",
                    20 + msg_len,
                    len(result),
                )
    except Exception as e:
        logger.debug("结构化解析失败: %s", e)

    # 6. 回退: 直接查找 <xml> 片段
    logger.debug("尝试直接查找XML内容...")
    try:
        content = result.decode("utf-8", errors="ignore")
    except Exception:
        content = str(result)

    xml_start = content.find("<xml>")
    xml_end = content.find("</xml>")
    if xml_start != -1 and xml_end != -1:
        logger.debug("成功提取XML片段")
        return content[xml_start : xml_end + 6]  # +6 = len("</xml>")

    logger.debug("未找到XML标签，返回原始内容")
    return content[:500]


def encrypt_message(
    reply_xml: str,
    encoding_aes_key: str,
    receive_id: str,
    timestamp: str,
    nonce: str,
    token: str,
) -> str:
    """加密回复并返回 XML 信封。

    企业微信智能机器人/客服的回复格式::

        <xml>
           <Encrypt><![CDATA[B64_...]]></Encrypt>
           <MsgSignature>SHA1(sort([token, timestamp, nonce, encrypt]))</MsgSignature>
           <TimeStamp>...</TimeStamp>
           <Nonce>...</Nonce>
        </xml>

    encrypt = base64( AES-256-CBC( pkcs7( 16随机 + 4字节len + reply_xml + receive_id ) ) )
    """
    if len(encoding_aes_key) != 43:
        raise ValueError(
            f"EncodingAESKey 必须是 43 位，实际 {len(encoding_aes_key)}"
        )

    import os

    key = base64.b64decode(encoding_aes_key + "=")
    iv = key[:16]

    rand16 = os.urandom(16)
    msg_bytes = reply_xml.encode("utf-8")
    msg_len = struct.pack(">I", len(msg_bytes))
    receive_id_bytes = receive_id.encode("utf-8")
    body = rand16 + msg_len + msg_bytes + receive_id_bytes

    # PKCS7 填充到 32 字节边界
    pad_len = _PKCS7_BLOCK - (len(body) % _PKCS7_BLOCK)
    if pad_len == 0:
        pad_len = _PKCS7_BLOCK
    body = body + bytes([pad_len] * pad_len)

    cipher = AES.new(key, AES.MODE_CBC, iv)
    encrypt_b64 = base64.b64encode(cipher.encrypt(body)).decode("utf-8")

    msg_signature = compute_signature(token, timestamp, nonce, encrypt_b64)

    return (
        "<xml>"
        f"<Encrypt><![CDATA[{encrypt_b64}]]></Encrypt>"
        f"<MsgSignature><![CDATA[{msg_signature}]]></MsgSignature>"
        f"<TimeStamp>{timestamp}</TimeStamp>"
        f"<Nonce><![CDATA[{nonce}]]></Nonce>"
        f"</xml>"
    )


__all__ = [
    "compute_signature",
    "verify_signature",
    "decrypt_message",
    "encrypt_message",
]
