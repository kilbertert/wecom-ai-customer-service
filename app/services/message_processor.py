"""协议无关的消息编排器 (MessageProcessor)。

把 KF (历史 ``WeChatService.process_single_message``) 与智能机器人 (历史 route
``_process_bot_message_background``) 两条编排主干统一到一个协议无关的流程::

    dedup → 媒体下载/上传 → ConversationStore.get(conversation_id)
           → ai.run_workflow(conversation_id=...) → ConversationStore.save
           → compose_multimodal_markdown → adapter.send → (KF) Chatwoot notify

协议特定动作 (验签/解密/拉取/投递/同步 ACK) 委托 :class:`ProtocolAdapter`。
智能机器人独有的 9 阶段决策日志 (BotTrace) 在本类内按 ``inbound.protocol=="bot"``
门控发射 —— 这是计划中明确保留的"adapter/trace 差异"。

设计约束 (与 CLAUDE.md 一致):
    - 不引入会话历史存储; ``ConversationStore`` 仅存 conversation_id 字符串。
    - 去重委托共享 :class:`DedupStore`, 默认 InMemory (单 worker)。
    - 不在编排器内关闭共享服务实例 (wechat/ai/media 由 lifespan 管理)。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional

from app.protocols.base import InboundMessage, OutboundReply, ProtocolAdapter
from app.core.config import settings
from app.services.bot_trace import (
    BotTrace,
    format_knowledge_lines,
    format_thinking_lines,
)
from app.services.conversation_store import ConversationStore
from app.services.multimodal import compose_multimodal_markdown
from app.services.trace_extract import extract_knowledge, extract_thinking

logger = logging.getLogger(__name__)

# bot 支持的消息类型
_BOT_SUPPORTED_TYPES = ("text", "image", "voice", "mixed")


@dataclass(frozen=True)
class PreparedInput:
    """``_prepare_input`` 的返回。

    - ``input_data`` 非空 → 正常调 AI 工作流
    - ``canned_reply`` 非空 → 跳过 AI, 直接回该文案 (bot 不支持/空消息)
    - 两者皆空 → 跳过 (KF 空内容, 不回执)
    """

    input_data: Optional[dict] = None
    canned_reply: str = ""


class MessageProcessor:
    """协议无关的消息编排器 (KF + 智能机器人)。"""

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
        is_bot = inbound.protocol == "bot"
        logger.info(
            "[PROC] 开始处理: protocol=%s msgid=%s type=%s",
            inbound.protocol, msgid, inbound.msg_type,
        )

        # bot 决策日志 (可拔插, 默认 off — 渲染由 adapter.send 按 trace_mode 决定)
        trace: Optional[BotTrace] = None
        if is_bot:
            trace = BotTrace(
                chat_type=inbound.chat_type, msg_type=inbound.msg_type
            )
            trace.event(
                "receive", "ok", f"from={inbound.user_id} id={msgid[:12]}"
            )

        dedup = adapter.dedup
        ttl = getattr(adapter, "dedup_ttl", 300)

        # 1) 去重: 进入 _processing (处理中, 可重试)。mark_done 仅在回复成功发送后
        # 才调 (见 step 7) —— 此前若崩溃/取消, release_processing 清 _processing,
        # 微信重试可重新 acquire, 不会丢消息 (修复 A1/A2/A3)。
        acquired = await dedup.acquire(msgid, ttl)
        if not acquired:
            logger.info("[PROC] msgid=%s 已处理过/处理中, 跳过", msgid)
            return
        if trace is not None:
            trace.event("dedup", "ok", "首次处理")

        # 记录是否已完成 (mark_done 已调), 供 finally 决定是否 release。
        # 用 list 包一层供闭包写入 (Python 闭包不能直接赋值外层不可变)。
        done_flag = [False]
        try:
            # 2) 媒体编排 + 预过滤 → input_data 或 canned_reply
            prepared = await self._prepare_input(inbound, trace)
            if not prepared.input_data and not prepared.canned_reply:
                logger.info("[PROC] msgid=%s 无有效内容, 跳过", msgid)
                # 无内容: 不发送, 直接 mark_done (无回复可丢, 也不必让重试再跑一遍)
                await dedup.mark_done(msgid)
                done_flag[0] = True
                return

            reply_text = ""
            if prepared.canned_reply:
                # bot 不支持/空消息: 跳过 AI, 直接回 canned (trace skip 已发射)
                reply_text = prepared.canned_reply
            else:
                # 3) Chatwoot handoff: 人工接管时跳过 AI (不消耗 conversation 轮次)
                if await self._is_handoff(inbound):
                    logger.info(
                        "[PROC] handoff=True, 人工接管, 跳过 AI: msgid=%s", msgid
                    )
                    # 人工接管: 不发 AI 回复 (人工经 Chatwoot 另一条路径回复)。
                    # mark_done 防重试; 不消耗 conversation_id。
                    await dedup.mark_done(msgid)
                    done_flag[0] = True
                    return

                # 4) conversation_id 续接 (薄映射, Dify chatflow 多轮)
                scope = (
                    "bot" if is_bot else (inbound.open_kfid or "kf")
                )
                conversation_id = await self._conv.get(
                    inbound.user_id, scope
                )
                # context trace: 反映真实多轮状态 (替换 _prepare_input 里过时的
                # "单轮模式,无历史" 文案 —— chatflow 透传 conversation_id 续接多轮)。
                if trace is not None:
                    if conversation_id:
                        trace.event(
                            "context", "ok",
                            f"续接多轮 conv={conversation_id[:12]}…",
                        )
                    else:
                        trace.event("context", "ok", "首次会话, 新建多轮")

                # 5) 调 AI 工作流
                try:
                    wf = await self._ai.run_workflow(
                        prepared.input_data,
                        user_id=inbound.user_id,
                        conversation_id=conversation_id,
                    )
                except Exception as e:
                    # B3: 对用户只回固定脱敏文案, 详细错误只进日志 (不发内部异常细节)
                    logger.error(
                        "[PROC] AI 工作流失败: msgid=%s, %s", msgid, e,
                        exc_info=True,
                    )
                    if trace is not None:
                        trace.event("knowledge", "skip", "无知识库检索")
                        trace.event("thinking", "skip", "无思考过程")
                        trace.event("ai", "fail", str(e)[:80])
                    reply_text = (
                        "抱歉，AI 服务暂时不可用，请稍后重试。"
                        if not is_bot
                        else "AI 服务暂时不可用，请稍后重试"
                    )

                else:
                    # 持久化新 conversation_id
                    if isinstance(wf, dict):
                        new_conv = wf.get("conversation_id") or ""
                        if new_conv:
                            await self._conv.save(
                                inbound.user_id, scope, new_conv
                            )
                    # bot: 知识库 / 思考阶段 trace
                    if trace is not None and isinstance(wf, dict):
                        self._trace_kb_thinking(trace, wf)
                        ai_detail = self._ai_detail(wf)
                        trace.event("ai", "ok", ai_detail)

                    reply_text = (
                        compose_multimodal_markdown(wf)
                        if isinstance(wf, dict)
                        else ""
                    )

            # 6) 空回复兜底 (B1): KF 与 bot 都给用户一个兜底文案, 不再静默丢弃
            #    (旧版 KF 只记日志+return, 用户什么都收不到; Dify data.status=failed
            #    经 compose 变空串时尤甚)。兜底后正常走 step 7 投递 + mark_done。
            if not reply_text or not reply_text.strip():
                reply_text = (
                    "抱歉，我暂时无法处理该消息，请稍后重试。"
                    if not is_bot
                    else "（AI 未返回内容）"
                )
                logger.warning(
                    "[PROC] msgid=%s 工作流返回空内容, 使用兜底文案", msgid
                )

            # 7) 发送去重 + 投递
            if not await dedup.mark_sent(msgid):
                logger.info("[PROC] msgid=%s 回复已发送过, 跳过", msgid)
                return

            sent_ok = await adapter.send(
                inbound, OutboundReply(text=reply_text), trace=trace
            )
            if not sent_ok:
                logger.warning("[PROC] msgid=%s 回复投递失败", msgid)
                # 发送失败: 释放 _processing 允许微信重试 (不 mark_done)
                return

            # 发送成功后才 mark_done (修复 A1): 此前任何崩溃/取消 → release → 可重试
            await dedup.mark_done(msgid)
            done_flag[0] = True

            # 8) Chatwoot 同步 (仅 KF: 把 AI 回复同步到人工侧)
            if not is_bot and inbound.open_kfid:
                await self._chatwoot_notify(inbound, reply_text)

            logger.info("[PROC] 完成: msgid=%s", msgid)

        except Exception as e:
            logger.error(
                "[PROC] 编排异常: msgid=%s, %s", msgid, e, exc_info=True
            )
        except BaseException as e:
            # A3: CancelledError 等 BaseException 也要 release, 否则 msgid 卡
            # _processing (且 _processing 旧版无 TTL 清理 → 永久泄漏)。记日志后重抛。
            logger.warning(
                "[PROC] 编排被中断 (BaseException): msgid=%s, %s", msgid,
                type(e).__name__,
            )
            raise
        finally:
            # 未完成 (未走 mark_done) → 释放 _processing, 允许微信重试。
            # 已 mark_done 的 msgid 已不在 _processing, release 是 no-op, 安全。
            if not done_flag[0]:
                await dedup.release_processing(msgid)

    # ------------------------------------------------------------------
    # 媒体编排 + 预过滤: InboundMessage → PreparedInput
    # ------------------------------------------------------------------
    async def _prepare_input(
        self, inbound: InboundMessage, trace: Optional[BotTrace]
    ) -> PreparedInput:
        if inbound.protocol == "bot":
            return await self._prepare_bot_input(inbound, trace)
        return await self._prepare_kf_input(inbound)

    # ---- KF ----
    async def _prepare_kf_input(self, inbound: InboundMessage) -> PreparedInput:
        """KF 媒体编排 (历史 process_single_message 行为)。

        text: 直接取 text
        image: download_media → ai.upload_file → file_image_id
        voice: MediaService 转码 → ai.upload_file → file_voice_id
        """
        input_data: dict = {"user_id": inbound.user_id}
        mt = inbound.msg_type

        if mt == "text":
            if not inbound.text:
                logger.warning("[PROC] KF 文本消息内容为空")
                return PreparedInput()
            input_data["text"] = inbound.text
            return PreparedInput(input_data=input_data)

        if mt == "image":
            if not inbound.media_ref:
                logger.warning("[PROC] KF 图片消息缺少 media_ref")
                return PreparedInput()
            try:
                content = await self._wechat.download_media(inbound.media_ref)
                file_name = f"wechat_image_{inbound.media_ref}.jpg"
                file_id = await self._ai.upload_file(content, file_name)
                input_data["file_image_id"] = file_id
                logger.info(
                    "[PROC] KF 图片上传: media_id=%s → %s",
                    inbound.media_ref, file_id,
                )
                return PreparedInput(input_data=input_data)
            except Exception as e:
                logger.error("[PROC] KF 图片处理失败: %s", e, exc_info=True)
                return PreparedInput()

        if mt == "voice":
            if not inbound.media_ref:
                logger.warning("[PROC] KF 语音消息缺少 media_ref")
                return PreparedInput()
            try:
                voice_content = await self._wechat.download_media(
                    inbound.media_ref
                )
                media_info = await self._media.download_and_process_media(
                    inbound.media_ref, "voice"
                )
                if media_info.get("error"):
                    logger.warning("[PROC] KF 语音转码失败, 使用原始 AMR")
                    file_name = f"wechat_voice_{inbound.media_ref}.amr"
                    file_id = await self._ai.upload_file(
                        voice_content, file_name
                    )
                elif media_info.get("converted") and media_info.get("wav_path"):
                    import aiofiles
                    async with aiofiles.open(media_info["wav_path"], "rb") as f:
                        wav_content = await f.read()
                    file_name = f"wechat_voice_{inbound.media_ref}.wav"
                    file_id = await self._ai.upload_file(wav_content, file_name)
                else:
                    file_name = f"wechat_voice_{inbound.media_ref}.amr"
                    file_id = await self._ai.upload_file(
                        voice_content, file_name
                    )
                input_data["file_voice_id"] = file_id
                logger.info(
                    "[PROC] KF 语音上传: media_id=%s → %s",
                    inbound.media_ref, file_id,
                )
                return PreparedInput(input_data=input_data)
            except Exception as e:
                logger.error("[PROC] KF 语音处理失败: %s", e, exc_info=True)
                return PreparedInput()

        logger.warning("[PROC] KF 不支持的消息类型: %s", mt)
        return PreparedInput()

    # ---- bot ----
    async def _prepare_bot_input(
        self, inbound: InboundMessage, trace: Optional[BotTrace]
    ) -> PreparedInput:
        """智能机器人媒体编排 + 预过滤 (历史 _process_bot_message_background 行为)。

        image url  → dify_file_image_url (remote_url, 不下载)
        image id   → download_media + upload → dify_file_image_id
        voice id   → download_media + upload → dify_file_voice_id (不转码)
        """
        import httpx as _httpx

        # 0) 媒体编排 (先于 prefilter, 与历史 trace 顺序一致)
        dify_file_image_url = ""
        dify_file_image_id = ""
        dify_file_voice_id = ""
        if inbound.media_ref:
            try:
                media_bytes: bytes = b""
                if inbound.media_kind == "url":
                    # bot image url: 不下载, 直接喂 Dify remote_url
                    if inbound.msg_type in ("image", "mixed"):
                        dify_file_image_url = inbound.media_ref
                        if trace is not None:
                            trace.event(
                                "media", "ok",
                                f"image remote_url len={len(inbound.media_ref)}",
                            )
                        logger.info(
                            "[PROC] bot image remote_url: msgid=%s url=%s...",
                            inbound.msgid, inbound.media_ref[:60],
                        )
                    else:
                        # voice url (实测未触发, 保留通用下载)
                        async with _httpx.AsyncClient(timeout=30.0) as ac:
                            r = await ac.get(inbound.media_ref)
                            r.raise_for_status()
                            media_bytes = r.content
                elif inbound.media_kind == "media_id":
                    media_bytes = await self._wechat.download_media(
                        inbound.media_ref
                    )
                else:
                    raise RuntimeError(
                        f"未知的 media_kind: {inbound.media_kind}"
                    )

                # image (media_id 路径) 上传
                if inbound.msg_type in ("image", "mixed") and not dify_file_image_url and media_bytes:
                    dify_file_image_id = await _upload_to_dify_file_store(
                        self._ai, media_bytes, inbound.media_ref, "image"
                    )
                    if trace is not None:
                        trace.event(
                            "media", "ok",
                            f"image uploaded size={len(media_bytes)}B",
                        )
                    logger.info(
                        "[PROC] bot image 上传: msgid=%s dify_file_id=%s size=%dB",
                        inbound.msgid, dify_file_image_id, len(media_bytes),
                    )
                # voice 上传 (media_id 路径)
                elif inbound.msg_type in ("voice", "mixed") and media_bytes and not dify_file_image_id and not dify_file_image_url:
                    dify_file_voice_id = await _upload_to_dify_file_store(
                        self._ai, media_bytes, inbound.media_ref, "audio"
                    )
                    if trace is not None:
                        trace.event(
                            "media", "ok",
                            f"voice uploaded size={len(media_bytes)}B",
                        )
                    logger.info(
                        "[PROC] bot voice 上传: msgid=%s dify_file_id=%s size=%dB",
                        inbound.msgid, dify_file_voice_id, len(media_bytes),
                    )
            except Exception as e:
                logger.error(
                    "[PROC] bot 媒体编排失败: msgid=%s, %s", inbound.msgid, e
                )
                if trace is not None:
                    trace.event("media", "fail", str(e)[:80])
        else:
            if trace is not None:
                trace.event("media", "skip", "无媒体")

        # 1) 预过滤
        is_unsupported = inbound.msg_type not in _BOT_SUPPORTED_TYPES
        is_empty = inbound.msg_type in ("text", "mixed") and not inbound.text and not inbound.media_ref
        if is_unsupported or is_empty:
            canned = (
                "收到不支持的消息类型" if is_unsupported else "收到空消息"
            )
            if trace is not None:
                trace.event("prefilter", "fail", f"{inbound.msg_type} 不支持/空")
                trace.event("knowledge", "skip", "无知识库检索")
                trace.event("thinking", "skip", "无思考过程")
                trace.event("ai", "skip", "无 AI 调用")
            return PreparedInput(canned_reply=canned)

        # prefilter ok
        if trace is not None:
            detail = ""
            if inbound.text:
                detail = f"text={len(inbound.text)}字"
            if inbound.media_ref:
                detail += f" media={inbound.msg_type}"
            trace.event("prefilter", "ok", detail.strip() or "ok")
            # context trace 移到 process() 里 ConversationStore.get 之后发射,
            # 以反映真实多轮状态 (续接/首次), 而非过时的"单轮模式,无历史"。

        # 2) input_data
        input_data: dict = {"user_id": inbound.user_id}
        if inbound.text:
            input_data["text"] = inbound.text
        if dify_file_image_url:
            input_data["file_image_url"] = dify_file_image_url
        elif dify_file_image_id:
            input_data["file_image_id"] = dify_file_image_id
        if dify_file_voice_id:
            input_data["file_voice_id"] = dify_file_voice_id
        if "text" not in input_data:
            input_data["text"] = (
                "[用户发了一张图片]"
                if inbound.msg_type == "image"
                else "[用户发了一段语音]"
                if inbound.msg_type == "voice"
                else ""
            )
        return PreparedInput(input_data=input_data)

    # ------------------------------------------------------------------
    # trace 辅助
    # ------------------------------------------------------------------
    @staticmethod
    def _trace_kb_thinking(trace: BotTrace, wf: dict) -> None:
        """从 Dify outputs 提取知识库/思考, 发射对应 trace 阶段。"""
        raw = (wf or {}).get("raw", {}) if isinstance(wf, dict) else {}
        outputs = ((raw or {}).get("data") or {}).get("outputs") or {}

        kb = extract_knowledge(outputs)
        if kb is not None:
            main, subs = format_knowledge_lines(kb)
            trace.event("knowledge", "ok", main, sub_lines=subs)
        else:
            trace.event("knowledge", "skip", "无知识库检索")

        thinking = extract_thinking(outputs)
        if thinking:
            main, subs = format_thinking_lines(thinking)
            trace.event("thinking", "ok", main, sub_lines=subs)
        else:
            trace.event("thinking", "skip", "无思考过程")

    @staticmethod
    def _ai_detail(wf: dict) -> str:
        """AI 阶段 trace detail: 文本长度 + 媒体计数。"""
        reply_text = compose_multimodal_markdown(wf)
        detail = f"text={len(reply_text)}字"
        for kind in ("images", "videos", "files"):
            cnt = len(wf.get(kind) or [])
            if cnt:
                detail += f" {kind[0]}{cnt}"
        return detail

    # ------------------------------------------------------------------
    # Chatwoot handoff / 同步 (仅 KF)
    # ------------------------------------------------------------------
    async def _is_handoff(self, inbound: InboundMessage) -> bool:
        """Chatwoot handoff 检查: 人工已接管时返回 True (跳过 AI, 不消耗会话轮次)。

        仅当 ``CHATWOOT_ENABLED=true`` 且 inbound 有 open_kfid (KF 路径) 时生效;
        bot 路径无 open_kfid, 不检查。检查失败默认不接管 (fail-open, 不阻塞 AI)。
        """
        if not getattr(settings.chatwoot, "enabled", False):
            return False
        if not inbound.open_kfid or not inbound.user_id:
            return False
        try:
            from app.services.chatwoot_sync_service import ChatwootSyncService

            sync = ChatwootSyncService()
            try:
                result = await sync.check_handoff(
                    inbound.open_kfid, inbound.user_id
                )
            finally:
                await sync.aclose()
        except Exception as e:
            # B2: fail-open —— Chatwoot 异常时默认不接管, 继续调 AI。
            # 风险: 若人工实际已接管, AI 会抢答。保留 fail-open (避免 Chatwoot 抖动
            # 时 AI 全面停摆), 但用 HANDOFF_FAIL_OPEN 标记提升可观测性, 便于告警/统计。
            logger.warning(
                "[PROC] HANDOFF_FAIL_OPEN handoff 检查异常, 默认不接管 (AI 可能抢答): "
                "msgid=%s, open_kfid=%s, %s",
                inbound.msgid, inbound.open_kfid, e,
            )
            return False
        handoff = bool((result or {}).get("handoff"))
        if handoff:
            logger.info(
                "[PROC] Chatwoot handoff=True (人工接管): msgid=%s",
                inbound.msgid,
            )
        return handoff

    async def _chatwoot_notify(
        self, inbound: InboundMessage, reply_text: str
    ) -> None:
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


async def _upload_to_dify_file_store(
    ai_service: Any, content: bytes, wechat_media_ref: str, file_type: str
) -> str:
    """把微信临时素材字节流上传到 Dify 文件库, 拿 upload_file_id。

    bot 路径专用 (与 KF 的 ``ai.upload_file`` 不同: 这里直接调
    ``client.upload_file`` 并显式指定 filename/content_type, 与历史行为一致)。

    Args:
        ai_service: AI 后端实例 (需有 ``client.upload_file``)
        content: 微信临时素材 bytes
        wechat_media_ref: media_id 或 url (仅用于生成可读文件名)
        file_type: "image" | "audio"
    """
    client = getattr(ai_service, "client", None)
    if client is None or not hasattr(client, "upload_file"):
        raise RuntimeError(
            f"当前 AI 后端 ({type(ai_service).__name__}) 不支持文件上传, "
            f"image/voice 消息转发无法工作"
        )

    ext_map = {"image": "jpg", "audio": "amr"}
    ext = ext_map.get(file_type, "bin")
    if "://" in wechat_media_ref:
        from urllib.parse import urlparse

        path = urlparse(wechat_media_ref).path
        slug = path.rsplit("/", 1)[-1] or "file"
        slug = slug[:20]
        filename = f"wechat_{file_type}_{slug}.{ext}"
    else:
        filename = f"wechat_{file_type}_{wechat_media_ref[:12]}.{ext}"
    mime_map = {"image": "image/jpeg", "audio": "audio/amr"}
    content_type = mime_map.get(file_type, "application/octet-stream")

    return await client.upload_file(
        filename=filename,
        content=content,
        content_type=content_type,
    )


__all__ = ["MessageProcessor", "PreparedInput"]
