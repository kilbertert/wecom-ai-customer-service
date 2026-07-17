"""企业微信智能机器人 (bot) 协议适配器。

把 bot 协议特有的 ``JSON 解密 + 验签 + image/voice/mixed 提取 + response_url
推送`` 从 route 层剥离, 让 ``MessageProcessor`` 只消费协议无关的
``InboundMessage``。

bot 协议要点 (与 KF 不同):
    - POST body 是 JSON ``{"encrypt": "B64..."}`` (不是 XML)
    - 签名: SHA1(sort([token, timestamp, nonce, encrypt]))
    - 解密: AES-256-CBC, receive_id = "" (企业自建固定空串)
    - 解密后是 JSON: {msgid, msgtype, from.userid, text.content, response_url, ...}
    - image: ``msg.image.url`` (微信 CDN 直链) 或 ``media_id``
    - voice: ``msg.voice.media_id`` (走 /media/get 下载)
    - mixed: ``msg.mixed.msg_item[]`` 数组 (图文混合)
    - 出站: POST ``response_url``, msgtype 必须 ``markdown``
    - 同步 ACK: 返回加密 JSON envelope (含占位 markdown), 防 5s 超时重试
"""

from __future__ import annotations

import json
import logging
import xml.etree.ElementTree as ET
from typing import Any, List, Optional

from app.core.config import settings
from app.protocols.base import (
    DedupStore,
    InboundMessage,
    OutboundReply,
    ProtocolAdapter,
)
from app.services.wechat import WeChatService

logger = logging.getLogger(__name__)

# bot 消息去重窗口 (与历史 route _BOT_MSG_TTL 一致: 10 分钟)
BOT_DEDUP_TTL = 600


class BotAdapter(ProtocolAdapter):
    """智能机器人协议适配器。

    持有共享 :class:`WeChatService` (验签/解密/download_media) 与共享
    :class:`DedupStore`。回复投递走 ``response_url`` (一次性 markdown POST)。
    """

    def __init__(self, wechat_service: WeChatService, dedup_store: DedupStore) -> None:
        self._svc = wechat_service
        self._dedup = dedup_store

    # ------------------------------------------------------------------
    # ProtocolAdapter
    # ------------------------------------------------------------------
    @property
    def dedup(self) -> DedupStore:
        return self._dedup

    #: 去重窗口 (秒), MessageProcessor 读取
    dedup_ttl: int = BOT_DEDUP_TTL

    async def receive(self, request: Any) -> List[InboundMessage]:
        """解析 bot 回调, 返回单元素列表 (或空列表)。

        流程: 取 body → 解析外层 JSON → 验签 → 解密 → 解析内层 JSON →
        提取 text/image/voice/mixed → 归一为 ``InboundMessage``。

        任何环节失败返回空列表 (route 层回 4xx/500)。
        """
        try:
            body = await request.body()
        except Exception as e:  # pragma: no cover - 防御性
            logger.warning("[BOT] 读取 request body 失败: %s", e)
            return []

        body_str = body.decode("utf-8", errors="ignore")
        query = getattr(request, "query_params", {}) or {}
        msg_signature = _q(query, "msg_signature")
        timestamp = _q(query, "timestamp")
        nonce = _q(query, "nonce")

        # 1) 外层 JSON
        try:
            data = json.loads(body_str)
        except json.JSONDecodeError as e:
            logger.warning("[BOT] 外层 JSON 解析失败: %s", e)
            return []

        msg_encrypt = (data.get("encrypt") or "").strip()
        if not msg_encrypt:
            logger.warning("[BOT] 缺少 encrypt 字段")
            return []

        # 2) 验签
        if not self._svc.verify_bot_signature(
            msg_signature, timestamp, nonce, msg_encrypt
        ):
            logger.warning("[BOT] 签名验证失败")
            return []

        # 3) AES 解密 (receive_id="")
        try:
            decrypted = self._svc.decrypt_message_custom(
                msg_encrypt, self._svc.config.kf_encoding_aes_key, ""
            )
        except Exception as e:
            logger.error("[BOT] AES 解密失败: %s", e)
            return []

        # 4) 内层 JSON
        try:
            msg = json.loads(decrypted)
        except json.JSONDecodeError as e:
            logger.error("[BOT] 内层 JSON 解析失败: %s", e)
            return []

        inbound = self._to_inbound(msg)
        logger.info(
            "[BOT] from=%s type=%s content_len=%d id=%s media=%s:%s",
            inbound.user_id, inbound.msg_type, len(inbound.text), inbound.msgid,
            inbound.media_kind, bool(inbound.media_ref),
        )
        return [inbound]

    async def send(
        self,
        inbound: InboundMessage,
        reply: OutboundReply,
        trace: Any = None,
    ) -> bool:
        """POST ``response_url`` (msgtype=markdown)。

        trace 模式 (仅 bot 有):
            - inline: 把 trace 渲染拼到主回复末尾, 单次 POST
            - separate: 主消息发出后, 再单独 POST 一次 trace
            - off: 不输出 trace
        """
        import httpx as _httpx

        if not inbound.response_url:
            logger.warning("[BOT] inbound 缺少 response_url, 无法推送")
            return False

        trace_mode = (getattr(settings.app, "bot_trace_mode", "off") or "off").lower()
        trace_max_len = getattr(settings.app, "bot_trace_max_len", 1500) or 1500

        final_reply = reply.text
        if trace is not None and trace_mode == "inline":
            trace_text = trace.render("inline", max_len=trace_max_len)
            if trace_text:
                final_reply = reply.text + trace_text

        payload = {"msgtype": "markdown", "markdown": {"content": final_reply}}
        push_ok = False
        try:
            async with _httpx.AsyncClient(timeout=30.0) as ac:
                r = await ac.post(
                    inbound.response_url,
                    json=payload,
                    headers={"Content-Type": "application/json"},
                )
            try:
                errcode = json.loads(r.text).get("errcode")
            except json.JSONDecodeError:
                errcode = None
            push_ok = r.status_code == 200 and errcode in (0, None)
            if trace is not None:
                trace.event(
                    "push",
                    "ok" if push_ok else "fail",
                    f"HTTP {r.status_code} errcode={errcode}",
                )
            logger.info(
                "[BOT] 异步推送: msgid=%s, HTTP %s errcode=%s",
                inbound.msgid, r.status_code, errcode,
            )
        except Exception as e:
            logger.error("[BOT] 异步推送异常: msgid=%s, %s", inbound.msgid, e)
            if trace is not None:
                trace.event("push", "fail", str(e)[:80])
            return False

        # separate 模式: 主消息已发出, 第二次 POST 推 trace (best-effort)
        if trace is not None and trace_mode == "separate":
            await _post_trace_separate(
                response_url=inbound.response_url,
                trace=trace,
                max_len=trace_max_len,
                msg_id=inbound.msgid,
            )

        return push_ok

    def build_sync_ack(self, timestamp: str, nonce: str, text: str = "") -> str:
        """构造智能机器人同步响应 envelope (加密 JSON 字符串)。

        智能机器人 aibot/response 接口: 加密 JSON 信封 (encrypt + msgsignature +
        timestamp + nonce), 内部明文是 ``{"msgtype":"markdown","markdown":{...}}``。
        """
        reply_text = text or "AI 正在处理中..."
        try:
            envelope_xml = WeChatService.encrypt_message_custom(
                reply_xml=json.dumps(
                    {"msgtype": "markdown", "markdown": {"content": reply_text}},
                    ensure_ascii=False,
                ),
                encoding_aes_key=self._svc.config.kf_encoding_aes_key,
                corp_id="",
                timestamp=timestamp,
                nonce=nonce,
                token=self._svc.config.kf_token,
            )
            env_root = ET.fromstring(envelope_xml)
            return json.dumps(
                {
                    "encrypt": env_root.findtext("Encrypt"),
                    "msgsignature": env_root.findtext("MsgSignature"),
                    "timestamp": timestamp,
                    "nonce": nonce,
                },
                ensure_ascii=False,
            )
        except Exception as e:
            logger.error("[BOT] 同步 envelope 构建失败: %s", e)
            return "{}"

    # ------------------------------------------------------------------
    # URL 验证 (GET /bot/callback)
    # ------------------------------------------------------------------
    def verify_url(
        self,
        msg_signature: str,
        timestamp: str,
        nonce: str,
        echostr: str,
    ) -> Optional[str]:
        """bot 回调 URL 验证: 验签 + 解密 echostr (receive_id="")。"""
        from app.crypto import wecom_crypto

        token = self._svc.config.kf_token
        if not wecom_crypto.verify_signature(
            token, timestamp, nonce, echostr, msg_signature
        ):
            logger.warning("[BOT VERIFY] 签名验证失败")
            return None
        try:
            plaintext = wecom_crypto.decrypt_message(
                echostr, self._svc.config.kf_encoding_aes_key, ""
            )
            logger.info(
                "[BOT VERIFY] 验签+解密通过, 返回明文 (长度 %d)", len(plaintext)
            )
            return plaintext
        except Exception as e:
            logger.error("[BOT VERIFY] echostr 解密失败: %s", e)
            return None

    # ------------------------------------------------------------------
    # 内部: 解密后 JSON → InboundMessage
    # ------------------------------------------------------------------
    @staticmethod
    def _to_inbound(msg: dict) -> InboundMessage:
        """把解密后的 bot JSON 归一为 ``InboundMessage``。

        识别 text / image / voice / mixed, 提取首个媒体定位符 (url 或 media_id)。
        """
        from_user = (msg.get("from") or {}).get("userid") or "unknown"
        msg_type = msg.get("msgtype") or ""
        msg_id = msg.get("msgid") or ""
        text_obj = msg.get("text") or {}
        content = (
            text_obj.get("content") if isinstance(text_obj, dict) else text_obj
        ) or ""
        content = str(content).strip()
        response_url = msg.get("response_url") or ""
        chattype_raw = (msg.get("chattype") or "single").strip().lower()
        chat_type = "group" if chattype_raw == "group" else "single"

        media_ref = ""
        media_kind = ""  # "url" | "media_id"
        media_type = ""  # "image" | "voice" | "" (实际媒体类型, mixed 时由首个媒体决定)
        img_aeskey = ""  # 企微AI机器人图片AES解密密钥
        # effective_media_type: image/voice/"" (mixed 时由子项决定)
        if msg_type == "image":
            media_type = "image"
            image_obj = msg.get("image") or {}
            if isinstance(image_obj, dict):
                url_val = (image_obj.get("url") or "").strip()
                if url_val:
                    media_ref, media_kind = url_val, "url"
                    img_aeskey = (image_obj.get("aeskey") or "").strip()
                else:
                    # media_id 回退 (无 url 的图片, 走 /media/get 下载)
                    mid_val = (image_obj.get("media_id") or "").strip()
                    if mid_val:
                        media_ref, media_kind = mid_val, "media_id"
        elif msg_type == "voice":
            media_type = "voice"
            voice_obj = msg.get("voice") or {}
            if isinstance(voice_obj, dict):
                mid_val = (voice_obj.get("media_id") or "").strip()
                if mid_val:
                    media_ref, media_kind = mid_val, "media_id"
        elif msg_type == "mixed":
            mixed_obj = msg.get("mixed") or {}
            items = []
            if isinstance(mixed_obj, dict):
                items = mixed_obj.get("msg_item") or mixed_obj.get("items") or []
            if isinstance(items, list):
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    itype = item.get("msgtype") or ""
                    if not content and itype == "text":
                        t = item.get("text") or {}
                        if isinstance(t, dict):
                            content = (t.get("content") or "").strip()
                    elif itype in ("image", "voice") and not media_ref:
                        if itype == "image":
                            media_type = "image"
                            img = item.get("image") or {}
                            if isinstance(img, dict):
                                url_val = (img.get("url") or "").strip()
                                if url_val:
                                    media_ref, media_kind = url_val, "url"
                                    img_aeskey = (img.get("aeskey") or "").strip()
                        else:  # voice
                            media_type = "voice"
                            v = item.get("voice") or {}
                            if isinstance(v, dict):
                                mid_val = (v.get("media_id") or "").strip()
                                if mid_val:
                                    media_ref, media_kind = mid_val, "media_id"

        # mixed 归一 msg_type 仍保留 "mixed" (MessageProcessor 据此走图文混合分支)
        return InboundMessage(
            protocol="bot",
            msgid=msg_id,
            msg_type=msg_type or "unknown",
            text=content,
            media_ref=media_ref,
            media_kind=media_kind,
            media_type=media_type,
            aeskey=img_aeskey,
            user_id=from_user,
            open_kfid="",
            response_url=response_url,
            chat_type=chat_type,
            raw={"message": msg},
        )


def _q(query: Any, key: str) -> str:
    if hasattr(query, "get"):
        val = query.get(key, "")
    else:  # pragma: no cover - 防御性
        val = ""
    return val or ""


async def _post_trace_separate(
    response_url: str,
    trace: Any,
    max_len: int,
    msg_id: str,
) -> None:
    """separate 模式: 主消息已发出后, 再单独 POST 一次 trace (best-effort)。"""
    import httpx as _httpx

    try:
        trace_text = trace.render("separate", max_len=max_len)
        if not trace_text:
            return
        payload = {"msgtype": "markdown", "markdown": {"content": trace_text}}
        async with _httpx.AsyncClient(timeout=15.0) as ac:
            r = await ac.post(
                response_url,
                json=payload,
                headers={"Content-Type": "application/json"},
            )
        try:
            errcode = json.loads(r.text).get("errcode")
        except json.JSONDecodeError:
            errcode = None
        logger.info(
            "[BOT] trace 推送: msgid=%s, HTTP %s errcode=%s",
            msg_id, r.status_code, errcode,
        )
    except Exception as e:
        logger.warning(
            "[BOT] trace 推送失败 (不影响主消息): msgid=%s, %s", msg_id, e
        )


__all__ = ["BotAdapter", "BOT_DEDUP_TTL"]
