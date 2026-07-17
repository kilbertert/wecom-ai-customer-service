"""微信客服 (KF) 协议适配器。

把 KF 协议特有的 ``XML 解密 + 验签 + sync_msg 拉取 + send_kf 回复`` 从 route/service
层剥离, 让 ``MessageProcessor`` 只消费协议无关的 ``InboundMessage``。

KF 协议要点:
    - POST body 是加密 XML (``<Encrypt>...</Encrypt>``)
    - 签名: SHA1(sort([token, timestamp, nonce, encrypt]))
    - 解密: AES-256-CBC, receive_id = corp_id
    - 入站: ``kf_msg_or_event`` 事件触发 pull 式 ``sync_msg``, 取最新一条客户消息
    - 出站: ``send_kf_msg`` (touser + open_kfid + msgtype=text)
    - 同步 ACK: 返回明文 ``"success"`` (后台任务跑完前先回, 防 5s 超时)
"""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from typing import Any, List, Optional

from app.core.config import settings
from app.protocols.base import (
    DedupStore,
    InboundMessage,
    OutboundReply,
    ProtocolAdapter,
    to_serializable,
)

logger = logging.getLogger(__name__)

# KF 消息去重窗口。须 >= 最坏处理耗时 (队列模式下 4 轮 Dify ≈ 4×120=480s,
# 见 APP_QUEUE_LOCK_TTL=600), 否则处理中 _processing key 提前过期 -> 微信重试
# 可能重复处理 (污染 Dify 上下文)。600s 覆盖绝大多数场景 (审查 P1 #4)。
KF_DEDUP_TTL = 600


class KfAdapter(ProtocolAdapter):
    """微信客服协议适配器。

    持有一个共享的 :class:`WeChatService` (用于 access_token / sync_msg / send_kf /
    download_media) 与一个共享的 :class:`DedupStore`。
    """

    def __init__(self, wechat_service: Any, dedup_store: DedupStore) -> None:
        self._svc = wechat_service
        self._dedup = dedup_store

    # ------------------------------------------------------------------
    # ProtocolAdapter
    # ------------------------------------------------------------------
    @property
    def dedup(self) -> DedupStore:
        return self._dedup

    #: 去重窗口 (秒), MessageProcessor 读取
    dedup_ttl: int = KF_DEDUP_TTL

    async def receive(self, request: Any) -> List[InboundMessage]:
        """解析 KF 回调, 返回本次同步的全部客户消息 (按时间升序)。

        流程: 取 body → 预解析 Encrypt → 验签 → 解密 → 找 ``kf_msg_or_event``
        事件 → ``sync_latest_messages`` 拉最新 → 全部归一为 ``InboundMessage``。

        ``sync_latest_messages`` 返回按 ``send_time`` **降序** (最新在前); 这里
        反转为**升序** (最旧在前) 返回, 让 route 层 BackgroundTasks 按时间顺序
        串行处理, Dify chatflow 多轮 ``conversation_id`` 才能正确续接。
        旧版只取 ``messages[0]`` (最新一条), 一次回调内多条客户消息静默丢弃
        (修复 A5)。每条独立 dedup, 已处理过的由 ``MessageProcessor`` 跳过。

        任何环节失败 (验签不过 / 解密失败 / 非目标事件 / 同步无消息) 返回空列表,
        route 层统一回 ``"success"`` 给微信。
        """
        try:
            body = await request.body()
        except Exception as e:  # pragma: no cover - 防御性
            logger.warning("[KF] 读取 request body 失败: %s", e)
            return []

        xml_data = body.decode("utf-8", errors="ignore")
        query = getattr(request, "query_params", {}) or {}
        msg_signature = _q(query, "msg_signature")
        timestamp = _q(query, "timestamp")
        nonce = _q(query, "nonce")

        # 预解析 Encrypt
        msg_encrypt = ""
        try:
            root = ET.fromstring(xml_data)
            enc = root.find("Encrypt")
            if enc is not None and enc.text:
                msg_encrypt = enc.text
        except Exception as e:
            logger.warning("[KF] 预解析 XML 失败: %s", e)
            return []

        if not msg_encrypt:
            logger.info("[KF] 无 Encrypt 字段, 跳过 (非加密消息)")
            return []

        # 验签
        if not self._svc.verify_signature(msg_signature, timestamp, nonce, msg_encrypt):
            logger.error("[KF] 签名验证失败")
            return []

        # 解密
        try:
            decrypted_xml = self._svc.decrypt_message_custom(
                msg_encrypt,
                self._svc.config.kf_encoding_aes_key,
                self._svc.config.corp_id,
            )
        except Exception as e:
            logger.error("[KF] 消息解密失败: %s", e)
            return []

        try:
            droot = ET.fromstring(decrypted_xml)
        except Exception as e:
            logger.error("[KF] 解密后 XML 解析失败: %s", e)
            return []

        msg_type = droot.findtext("MsgType") or ""
        if msg_type != "event":
            logger.info("[KF] 非事件消息 (msg_type=%s), 跳过", msg_type)
            return []

        event = droot.findtext("Event") or ""
        if event != "kf_msg_or_event":
            logger.info("[KF] 收到其他事件: %s, 跳过", event)
            return []

        sync_token = droot.findtext("Token") or ""
        open_kfid = droot.findtext("OpenKfId") or ""
        if not sync_token:
            logger.error("[KF] XML 中未找到 Token, 无法同步消息")
            return []

        # 只处理指定客服 (可选)
        allowed = getattr(settings.wechat, "allowed_open_kfid", None)
        if allowed and open_kfid and open_kfid != allowed:
            logger.info("[KF] 跳过非指定客服消息: %s (只处理 %s)", open_kfid, allowed)
            return []

        # 事件去重 (软检查: 即使已处理也仍尝试同步最新消息, 与历史行为一致)
        try:
            await self._svc.is_event_processed(sync_token)
        except Exception as e:  # pragma: no cover - 防御性
            logger.warning("[KF] is_event_processed 异常 (忽略): %s", e)

        # 拉取最新客户消息
        try:
            messages = await self._svc.sync_latest_messages(
                sync_token=sync_token,
                open_kfid=open_kfid,
                max_attempts=3,
                clear_cursor=True,
            )
        except Exception as e:
            logger.error("[KF] 同步消息失败: %s", e)
            return []

        if not messages:
            logger.info("[KF] 未找到有效的客户消息")
            return []

        # A5: 派发本次同步的全部客户消息 (旧版只取 messages[0]=最新, 其余丢弃)。
        # messages 按 send_time 降序 (最新在前) → 反转为升序 (最旧在前), 让多轮
        # conversation_id 按时间顺序续接。无 msgid 的脏数据无法 dedup, 跳过。
        inbound_list: List[InboundMessage] = []
        for msg in reversed(messages):
            inbound = self._to_inbound(msg)
            if not inbound.msgid:
                logger.warning("[KF] 跳过无 msgid 的消息: type=%s", inbound.msg_type)
                continue
            inbound_list.append(inbound)
        if not inbound_list:
            logger.info("[KF] 同步到的消息均无 msgid, 无可派发")
            return []
        logger.info(
            "[KF] 选中 %d 条客户消息 (按时间升序派发): %s",
            len(inbound_list),
            [m.msgid for m in inbound_list],
        )
        return inbound_list

    async def send(
        self,
        inbound: InboundMessage,
        reply: OutboundReply,
        trace: Any = None,
    ) -> bool:
        """通过 ``send_kf_msg`` 把回复文本发回客户。

        KF 当前用 ``msgtype=text`` 发送 markdown 文本 (含内嵌图片 URL)。
        ``trace`` 参数仅为与 ``BotAdapter.send`` 同形, KF 路径忽略。
        """
        if not inbound.open_kfid:
            logger.warning("[KF] inbound 缺少 open_kfid, 无法发送回复")
            return False
        try:
            await self._svc.send_message_simple(
                inbound.user_id, inbound.open_kfid, reply.text
            )
            logger.info("[KF] 回复已发送: msgid=%s", inbound.msgid)
            return True
        except Exception as e:
            logger.error("[KF] 发送回复失败: msgid=%s, %s", inbound.msgid, e)
            return False

    def build_sync_ack(self, timestamp: str, nonce: str, text: str = "") -> str:
        """KF 同步 ACK: 恒返回 ``"success"``。"""
        return "success"

    # ------------------------------------------------------------------
    # URL 验证 (GET /kf/callback)
    # ------------------------------------------------------------------
    def verify_url(
        self,
        msg_signature: str,
        timestamp: str,
        nonce: str,
        echostr: str,
    ) -> Optional[str]:
        """KF 回调 URL 验证: 验签 + 解密 echostr, 返回明文。

        验签或解密失败返回 None (route 层回退返回原始 echostr)。
        """
        from app.crypto import wecom_crypto

        token = self._svc.config.kf_token
        if not wecom_crypto.verify_signature(
            token, timestamp, nonce, echostr, msg_signature
        ):
            logger.warning("[KF VERIFY] 签名验证失败")
            return None

        try:
            plaintext = wecom_crypto.decrypt_message(
                echostr,
                self._svc.config.kf_encoding_aes_key,
                self._svc.config.corp_id,
            )
            logger.info("[KF VERIFY] 验签+解密通过, 返回明文 (长度 %d)", len(plaintext))
            return plaintext
        except Exception as e:
            logger.error("[KF VERIFY] echostr 解密失败: %s", e)
            return None

    # ------------------------------------------------------------------
    # 内部: WeChatMessage → InboundMessage
    # ------------------------------------------------------------------
    @staticmethod
    def _to_inbound(message: Any) -> InboundMessage:
        """把 ``WeChatMessage`` 归一为协议无关的 ``InboundMessage``。"""
        msgtype = getattr(message, "msgtype", None)
        if msgtype is not None and hasattr(msgtype, "value"):
            mt_val = msgtype.value
        elif msgtype is not None:
            mt_val = str(msgtype)
        else:
            mt_val = "unknown"

        text = ""
        media_ref = ""
        media_kind = ""
        media_type = ""  # "image" | "voice" | ""

        if mt_val == "text":
            td = getattr(message, "text", None)
            if isinstance(td, dict):
                text = td.get("content", "") or ""
        elif mt_val == "image":
            media_type = "image"
            img = getattr(message, "image", None) or {}
            if isinstance(img, dict):
                media_ref = img.get("media_id", "") or ""
                media_kind = "media_id" if media_ref else ""
        elif mt_val == "voice":
            media_type = "voice"
            v = getattr(message, "voice", None) or {}
            if isinstance(v, dict):
                media_ref = v.get("media_id", "") or ""
                media_kind = "media_id" if media_ref else ""

        return InboundMessage(
            protocol="kf",
            msgid=getattr(message, "msgid", "") or "",
            msg_type=mt_val,
            text=text,
            media_ref=media_ref,
            media_kind=media_kind,
            media_type=media_type,
            user_id=getattr(message, "external_userid", "") or "wechat_user",
            open_kfid=getattr(message, "open_kfid", "") or "",
            response_url="",
            chat_type="single",
            raw={"message": to_serializable(message)},
        )


def _q(query: Any, key: str) -> str:
    """从 query_params (dict / QueryParams) 取值, 兼容测试 mock。"""
    if hasattr(query, "get"):
        val = query.get(key, "")
    else:  # pragma: no cover - 防御性
        val = ""
    return val or ""


__all__ = ["KfAdapter", "KF_DEDUP_TTL"]
