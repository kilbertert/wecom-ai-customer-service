"""协议无关的消息编排器 (MessageProcessor)。

从历史 ``WeChatService.process_single_message`` 与 route 层
``_process_bot_message_background`` 提取出与协议无关的编排主干::

    dedup → 媒体下载/上传 → ConversationStore.get(conversation_id)
           → ai.run_workflow(conversation_id=...) → ConversationStore.save
           → compose_multimodal_markdown → adapter.send → Chatwoot notify

协议特定动作 (验签/解密/拉取/投递/同步 ACK) 全部委托给 :class:`ProtocolAdapter`,
本类只消费 ``InboundMessage`` / ``OutboundReply``, 新增协议无需改编排器。

设计约束 (与 CLAUDE.md 一致):
    - 不引入会话历史存储; ``ConversationStore`` 仅存一个 conversation_id 字符串
      供 Dify chatflow 续接, 记忆由 Dify / Chatwoot 侧持有。
    - 去重委托共享 :class:`DedupStore`, 默认 InMemory (单 worker)。
    - 不在编排器内关闭共享服务实例 (wechat/ai/media 由 lifespan 管理)。
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from app.protocols.base import InboundMessage, OutboundReply, ProtocolAdapter
from app.services.conversation_store import ConversationStore
from app.services.multimodal import compose_multimodal_markdown

logger = logging.getLogger(__name__)


class MessageProcessor:
    """协议无关的消息编排器。

    Args:
        wechat_service: 共享 WeChatService (用于 download_media 等 KF 媒体下载)
        media_service:  共享 MediaService (语音 AMR→WAV 转码)
        ai_service:     共享 AI 后端 (Coze / Dify), 实现 upload_file / run_workflow
        conversation_store: 薄 conversation_id 映射
    """

    def __init__(
        self,
        wechat_service: Any,
        media_service: Any,
        ai_service: Any,
        conversation_store: ConversationStore,
    ) -> None:
        self._wechat = wechat_service
        self._media = media_service
        self._ai = ai_service
        self._conv = conversation_store

    async def process(
        self, inbound: InboundMessage, adapter: ProtocolAdapter
    ) -> None:
        """处理一条入站消息 (作为后台任务调用)。

        任何异常都被捕获并记录, 不抛出 (后台任务无人 await)。
        """
        msgid = inbound.msgid or "unknown"
        logger.info("[PROC] 开始处理: protocol=%s msgid=%s type=%s",
                    inbound.protocol, msgid, inbound.msg_type)

        dedup = adapter.dedup
        ttl = getattr(adapter, "dedup_ttl", 300)

        # 1) 去重: 占有处理权
        acquired = await dedup.acquire(msgid, ttl)
        if not acquired:
            logger.info("[PROC] msgid=%s 已处理过/处理中, 跳过", msgid)
            return

        try:
            # 2) 媒体编排 → input_data
            input_data = await self._prepare_input(inbound)
            if not input_data:
                logger.info("[PROC] msgid=%s 无有效内容, 跳过工作流", msgid)
                # 标记完成, 防止重试风暴 (与历史行为一致)
                await dedup.mark_done(msgid)
                return

            # 内容准备成功 → 标记已处理 (即使后续工作流失败也不重试, 避免风暴)
            await dedup.mark_done(msgid)

            # 3) conversation_id 续接 (薄映射, Dify chatflow 多轮)
            scope = inbound.open_kfid or ("bot" if inbound.protocol == "bot" else "kf")
            conversation_id = await self._conv.get(inbound.user_id, scope)

            # 4) 调 AI 工作流
            try:
                wf = await self._ai.run_workflow(
                    input_data,
                    user_id=inbound.user_id,
                    conversation_id=conversation_id,
                )
            except Exception as e:
                logger.error("[PROC] AI 工作流失败: msgid=%s, %s", msgid, e)
                return

            # 持久化新 conversation_id (Dify chatflow 返回)
            if isinstance(wf, dict):
                new_conv = wf.get("conversation_id") or ""
                if new_conv:
                    await self._conv.save(inbound.user_id, scope, new_conv)

            # 5) 组装回复 markdown
            reply_text = compose_multimodal_markdown(wf) if isinstance(wf, dict) else ""
            if not reply_text or not reply_text.strip():
                logger.warning("[PROC] msgid=%s 工作流返回空内容", msgid)
                return

            # 6) 发送去重 + 投递
            if not await dedup.mark_sent(msgid):
                logger.info("[PROC] msgid=%s 回复已发送过, 跳过", msgid)
                return

            sent_ok = await adapter.send(inbound, OutboundReply(text=reply_text))
            if not sent_ok:
                logger.warning("[PROC] msgid=%s 回复投递失败", msgid)
                return

            # 7) Chatwoot 同步 (KF 路径, 把 AI 回复同步到人工侧)
            if inbound.protocol == "kf" and inbound.open_kfid:
                await self._chatwoot_notify(inbound, reply_text)

            logger.info("[PROC] 完成: msgid=%s", msgid)

        except Exception as e:
            logger.error("[PROC] 编排异常: msgid=%s, %s", msgid, e, exc_info=True)
            # 失败时释放"处理中"标记, 允许重试 (mark_done 未到则不影响)
            await dedup.release_processing(msgid)

    # ------------------------------------------------------------------
    # 媒体编排: InboundMessage → AI input_data
    # ------------------------------------------------------------------
    async def _prepare_input(self, inbound: InboundMessage) -> Optional[dict]:
        """把入站消息转成 AI 工作流输入。

        text: 直接取 text
        image: download_media → ai.upload_file → file_image_id
        voice: MediaService 转码 → ai.upload_file → file_voice_id

        不支持/空内容返回 None。
        """
        input_data: dict = {"user_id": inbound.user_id}
        mt = inbound.msg_type

        if mt == "text":
            if not inbound.text:
                logger.warning("[PROC] 文本消息内容为空")
                return None
            input_data["text"] = inbound.text
            return input_data

        if mt == "image":
            if not inbound.media_ref:
                logger.warning("[PROC] 图片消息缺少 media_ref")
                return None
            try:
                content = await self._wechat.download_media(inbound.media_ref)
                file_name = f"wechat_image_{inbound.media_ref}.jpg"
                file_id = await self._ai.upload_file(content, file_name)
                input_data["file_image_id"] = file_id
                logger.info("[PROC] 图片上传成功: media_id=%s → %s",
                            inbound.media_ref, file_id)
                return input_data
            except Exception as e:
                logger.error("[PROC] 图片处理失败: %s", e, exc_info=True)
                return None

        if mt == "voice":
            if not inbound.media_ref:
                logger.warning("[PROC] 语音消息缺少 media_ref")
                return None
            try:
                # 原始语音字节 (转码失败时兜底用)
                voice_content = await self._wechat.download_media(inbound.media_ref)
                media_info = await self._media.download_and_process_media(
                    inbound.media_ref, "voice"
                )

                if media_info.get("error"):
                    logger.warning("[PROC] 语音转码失败, 使用原始 AMR")
                    file_name = f"wechat_voice_{inbound.media_ref}.amr"
                    file_id = await self._ai.upload_file(voice_content, file_name)
                elif media_info.get("converted") and media_info.get("wav_path"):
                    import aiofiles
                    async with aiofiles.open(media_info["wav_path"], "rb") as f:
                        wav_content = await f.read()
                    file_name = f"wechat_voice_{inbound.media_ref}.wav"
                    file_id = await self._ai.upload_file(wav_content, file_name)
                else:
                    file_name = f"wechat_voice_{inbound.media_ref}.amr"
                    file_id = await self._ai.upload_file(voice_content, file_name)

                input_data["file_voice_id"] = file_id
                logger.info("[PROC] 语音上传成功: media_id=%s → %s",
                            inbound.media_ref, file_id)
                return input_data
            except Exception as e:
                logger.error("[PROC] 语音处理失败: %s", e, exc_info=True)
                return None

        logger.warning("[PROC] 不支持的消息类型: %s", mt)
        return None

    # ------------------------------------------------------------------
    # Chatwoot 同步 (KF 路径)
    # ------------------------------------------------------------------
    async def _chatwoot_notify(
        self, inbound: InboundMessage, reply_text: str
    ) -> None:
        """把 AI 回复同步到 Chatwoot (非致命, 失败仅日志)。"""
        try:
            from app.services.chatwoot_sync_service import ChatwootSyncService

            sync = ChatwootSyncService()
            try:
                await sync.notify_incoming(
                    open_kfid=inbound.open_kfid,
                    external_userid=inbound.user_id,
                    message_data={
                        "msgid": inbound.msgid,
                        "msgtype": "text",
                        "text": {"content": reply_text},
                        "origin": 2,
                    },
                )
            finally:
                await sync.aclose()
        except Exception as e:
            logger.error(
                "[ChatwootSync] 同步失败 (非致命, 不影响 WeCom 流程): %s", e
            )


__all__ = ["MessageProcessor"]
