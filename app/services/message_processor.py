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
from app.services.pending_timer_store import PendingTimerStore
from app.services.timer_coordinator import (
    apply_markers,
    cancel_pending_timer,
    parse_timer_markers,
    parse_switch_markers,
    SWITCH_TO_BUG,
    SWITCH_TO_KB_REENTRY,
    SWITCH_TO_KB_DONE,
)
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
        pending_timer_store: Optional[PendingTimerStore] = None,
    ) -> None:
        self._wechat = wechat_service
        self._media = media_service
        self._ai = ai_service
        self._conv = conversation_store
        # 二阶段: 待办定时器存储 (非会话历史)。None 时跳过 timer 协调 (向后兼容)。
        self._timers = pending_timer_store

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

        # 二阶段: 用户在 30 分钟窗口内又说话了 → cancel 旧的待确认倒计时 (N17 同步路径)。
        # Dify chatflow 会靠 cv_flow_state 自行做相关性分发, 后端只需让旧 timer 作废。
        if self._timers is not None:
            scope = "bot" if is_bot else (inbound.open_kfid or "kf")
            await cancel_pending_timer(self._timers, inbound.user_id, scope)

        # 记录是否已完成 (mark_done 已调), 供 finally 决定是否 release。
        # 用 list 包一层供闭包写入 (Python 闭包不能直接赋值外层不可变)。
        done_flag = [False]
        try:
            # 2) 媒体编排 + 预过滤 → input_data 或 canned_reply
            scope = ("bot" if is_bot else (inbound.open_kfid or "kf"))
            state = await self._conv.get_state(inbound.user_id, scope)
            dual = getattr(self._ai, "dual_app", False)
            active = state.get("active", "A") if dual else "A"
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
                # 3) Chatwoot: 先幂等同步客户入站消息 (origin=1), 再查 handoff。
                #    旧版只在末尾同步 AI 回复 (origin=2), 接管时客户原消息没进 Chatwoot,
                #    人工看不到触发消息 (审查 P1)。msgid 用 inbound.msgid, 与出站 _out 区分。
                if not is_bot and inbound.open_kfid:
                    await self._chatwoot_notify_inbound(inbound)
                if await self._is_handoff(inbound):
                    logger.info(
                        "[PROC] handoff=True, 人工接管, 跳过 AI: msgid=%s", msgid
                    )
                    # 人工接管: 不发 AI 回复 (人工经 Chatwoot 另一条路径回复)。
                    # mark_done 防重试; 不消耗 conversation_id。
                    await dedup.mark_done(msgid)
                    done_flag[0] = True
                    return

                conv_id = (state.get("conv_a") if active == "A"
                           else state.get("conv_b")) or None
                # context trace: 反映真实多轮状态 (替换 _prepare_input 里过时的
                # "单轮模式,无历史" 文案 —— chatflow 透传 conversation_id 续接多轮)。
                if trace is not None:
                    if conv_id:
                        trace.event(
                            "context", "ok",
                            f"续接多轮 conv={conv_id[:12]}… app={active}",
                        )
                    else:
                        trace.event("context", "ok", "首次会话, 新建多轮")

                # 5) 调 AI 工作流 (active app)
                try:
                    wf = await self._ai.run_workflow(
                        prepared.input_data,
                        user_id=inbound.user_id,
                        conversation_id=conv_id,
                        app=active,
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
                    # 取 active app 回复 + 新 conv_id
                    reply_text = (
                        compose_multimodal_markdown(wf)
                        if isinstance(wf, dict) else ""
                    )
                    new_conv = (wf.get("conversation_id") or ""
                                if isinstance(wf, dict) else "")
                    if new_conv:
                        state["conv_a" if active == "A" else "conv_b"] = new_conv
                    final_wf = wf

                    # 双 app 标记驱动路由: 循环改投直到无 SWITCH 标记或达上限。
                    # 防 A↔B ping-pong (B→A REENTRY 后 A 又判 bug → SWITCH_TO_BUG → B 采集)。
                    # 上限 MAX_ROUTES 次改投; 超出则剥离残留标记用最后回复。
                    if dual:
                        MAX_ROUTES = 3
                        for _ in range(MAX_ROUTES):
                            reply_text, switches = parse_switch_markers(reply_text)
                            re_route = None  # None=不改投/仅切active; "A"/"B"=同条改投
                            if SWITCH_TO_BUG in switches and active == "A":
                                state["active"] = "B"; re_route = "B"
                            elif SWITCH_TO_KB_REENTRY in switches and active == "B":
                                state["active"] = "A"; re_route = "A"
                            elif SWITCH_TO_KB_DONE in switches and active == "B":
                                state["active"] = "A"  # 发 B 话术, 下条→A, 不改投
                            if not re_route:
                                break
                            try:
                                # 改投到 re_route app: input_data 携带 app 无关的图片
                                # 字节, _run_chatflow 会在发送时按目标 app 上传, 文件
                                # 归属自动正确, 无需在此特殊处理 (根除跨 app file_id 失效)。
                                wf2 = await self._ai.run_workflow(
                                    prepared.input_data,
                                    user_id=inbound.user_id,
                                    conversation_id=(state.get("conv_b")
                                                     if re_route == "B"
                                                     else state.get("conv_a")) or None,
                                    app=re_route,
                                )
                            except Exception as e2:
                                logger.error(
                                    "[PROC] 改投 %s 失败 msgid=%s: %s",
                                    re_route, msgid, e2, exc_info=True,
                                )
                                break
                            rt2 = (compose_multimodal_markdown(wf2)
                                   if isinstance(wf2, dict) else "")
                            nc2 = (wf2.get("conversation_id") or ""
                                   if isinstance(wf2, dict) else "")
                            if nc2:
                                state["conv_b" if re_route == "B"
                                      else "conv_a"] = nc2
                            reply_text = rt2  # 下轮解析其 SWITCH 标记
                            final_wf = wf2
                            active = state.get("active", "A")
                        # 最终剥离残留 SWITCH 标记 (达上限仍带标记时)
                        reply_text, _ = parse_switch_markers(reply_text)

                    # 持久化路由状态
                    await self._conv.save_state(inbound.user_id, scope, state)

                    # bot: 知识库 / 思考阶段 trace (用最终 wf)
                    if trace is not None and isinstance(final_wf, dict):
                        self._trace_kb_thinking(trace, final_wf)
                        trace.event("ai", "ok", self._ai_detail(final_wf))


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

            # 二阶段: 从 AI 回复末尾解析 TIMER 握手标记, 剥离后用户不可见。
            # markers 收集起来, send 成功后再 arm/cancel (不阻塞回复投递)。
            timer_markers: list = []
            if self._timers is not None:
                reply_text, timer_markers = parse_timer_markers(reply_text)

            # 7) 投递 (发送去重由 acquire 保证; mark_done 在成功投递后原子提交)
            #    不在 send 前 mark_sent: 旧版 mark_sent 在 send 前入 _sent, send 失败
            #    不清 -> 重试被 _sent 永久拦截 (审查 P1)。现改为仅 send 成功后 mark_done。
            sent_ok = await adapter.send(
                inbound, OutboundReply(text=reply_text), trace=trace
            )
            if not sent_ok:
                logger.warning("[PROC] msgid=%s 回复投递失败", msgid)
                # 发送失败: 释放 _processing 允许微信重试 (不 mark_done, 不入 _sent)
                return

            # 发送成功后才 mark_done (修复 A1): 此前任何崩溃/取消 -> release -> 可重试
            await dedup.mark_done(msgid)
            done_flag[0] = True

            # 二阶段: 回复已投递, 处理 TIMER 标记 (arm/cancel 30 分钟倒计时)。
            # 放在 send 成功后: 若回复投递失败, 不应 arm 定时器 (用户没收到待确认问题)。
            if self._timers is not None and timer_markers:
                scope = "bot" if is_bot else (inbound.open_kfid or "kf")
                await apply_markers(
                    self._timers, inbound.user_id, scope, timer_markers
                )

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
        # 媒体上传不在 prepare 阶段做 (目标 app 可能因改投而变); 携带原始字节,
        # 由 _run_chatflow 在发送时按目标 app 上传 (Dify 文件库按 app 隔离)。
        if inbound.protocol == "bot":
            return await self._prepare_bot_input(inbound, trace)
        return await self._prepare_kf_input(inbound)

    # ---- KF ----
    async def _prepare_kf_input(self, inbound: InboundMessage) -> PreparedInput:
        """KF 媒体编排 (历史 process_single_message 行为)。

        text: 直接取 text
        image: download_media → file_image_bytes (携带原始字节, 上传延后到
               _run_chatflow 发送时按目标 app 进行; Dify 文件库按 app 隔离)
        voice: MediaService 转码 → ASR → text
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
                # 携带原始字节, 不在此上传: 目标 app 可能因改投而变, 上传须在发送时
                # (_run_chatflow) 按目标 app 进行 (Dify 文件库按 app 隔离)。
                input_data["file_image_bytes"] = content
                input_data["file_image_name"] = file_name
                input_data["text"] = "[image]"
                logger.info(
                    "[PROC] KF 图片待传: media_id=%s size=%dB",
                    inbound.media_ref, len(content),
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
                # 下载 + AMR->WAV 转码 (media_service 内部下载+转码)
                media_info = await self._media.download_and_process_media(
                    inbound.media_ref, "voice"
                )
                if media_info.get("error"):
                    logger.warning(
                        "[PROC] KF 语音转码失败: %s", media_info["error"]
                    )
                    input_data["text"] = "[用户发了一段语音,识别失败]"
                    return PreparedInput(input_data=input_data)
                # ASR 转写: 优先 WAV 转码结果, 回退原始文件路径
                # Dify chatflow 无 ASR 节点, 语音在 wecom 侧转文本作为 query (见 asr.py)
                wav_path = media_info.get("wav_path") or media_info.get("file_path")
                transcript = ""
                if wav_path:
                    from app.services.asr import transcribe as asr_transcribe
                    transcript = await asr_transcribe(wav_path)
                input_data["text"] = transcript or "[用户发了一段语音,识别失败]"
                logger.info(
                    "[PROC] KF 语音 ASR: media_id=%s -> %s",
                    inbound.media_ref, input_data["text"][:50],
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

        image → download_media (+AES解密+PIL转码) → file_image_bytes (携带字节,
               上传延后到 _run_chatflow 发送时按目标 app 进行)
        voice  → download_media + ASR → text
        """
        import httpx as _httpx

        # 0) 媒体编排 (先于 prefilter, 与历史 trace 顺序一致)
        # 图片携带原始字节 (app 无关), 上传延后到 _run_chatflow 发送时按目标 app 进行。
        dify_file_image_bytes: bytes = b""
        dify_file_image_name = ""
        bot_voice_transcript = ""
        if inbound.media_ref:
            try:
                media_bytes: bytes = b""
                if inbound.media_kind == "url":
                    # bot image url: 下载后走 local_file (remote_url 喂 Dify vision 会
                    # "Invalid base64 image_url" - Dify 下载企微 COS url 失败; 改下载+upload)
                    if inbound.msg_type in ("image", "mixed"):
                        async with _httpx.AsyncClient(timeout=30.0) as ac:
                            r = await ac.get(inbound.media_ref)
                            r.raise_for_status()
                            media_bytes = r.content
                        # 企微AI机器人图片url是AES-256-CBC加密, 用aeskey解密
                        # aeskey 可能空(HTTP回调模式不返回), 回退到全局 EncodingAESKey
                        _aeskey = inbound.aeskey or ""
                        if not _aeskey:
                            try:
                                _aeskey = settings.wechat.encoding_aes_key.get_secret_value()
                            except Exception:
                                _aeskey = ""
                        if _aeskey:
                            try:
                                import base64 as _b64
                                from Crypto.Cipher import AES as _AES
                                _ak = _aeskey
                                _key = _b64.b64decode(_ak + "=") if len(_ak) == 43 else _b64.b64decode(_ak)
                                _iv = _key[:16]
                                _cipher = _AES.new(_key, _AES.MODE_CBC, _iv)
                                _dec = _cipher.decrypt(media_bytes)
                                _pad = _dec[-1]
                                if 1 <= _pad <= 32:
                                    media_bytes = _dec[:-_pad]
                                else:
                                    media_bytes = _dec
                                logger.info("[PROC] bot image AES解密: msgid=%s dec_size=%dB magic=%s", inbound.msgid, len(media_bytes), media_bytes[:8].hex())
                                # PIL 打开+转标准JPEG (避免解密后PNG损坏/特殊格式 qwen打不开)
                                try:
                                    from PIL import Image
                                    import io as _io
                                    _img = Image.open(_io.BytesIO(media_bytes))
                                    if _img.mode != "RGB":
                                        _img = _img.convert("RGB")
                                    _buf = _io.BytesIO()
                                    _img.save(_buf, format="PNG")
                                    media_bytes = _buf.getvalue()
                                    logger.info("[PROC] bot image PIL转PNG: msgid=%s size=%dB magic=%s", inbound.msgid, len(media_bytes), media_bytes[:8].hex())
                                except Exception as _pe:
                                    logger.error("[PROC] bot image PIL转码失败: %s", _pe, exc_info=True)
                            except Exception as e:
                                logger.error("[PROC] bot image AES解密失败: %s", e, exc_info=True)
                        if trace is not None:
                            trace.event(
                                "media", "ok",
                                f"image url downloaded size={len(media_bytes)}B",
                            )
                        logger.info(
                            "[PROC] bot image url 下载: msgid=%s size=%dB magic=%s",
                            inbound.msgid, len(media_bytes), media_bytes[:8].hex(),
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

                # image: 携带原始字节 (已下载+AES解密+PIL转码), 不在此上传。
                # 上传在 _run_chatflow 发送时按目标 app 进行 (Dify 文件库按 app 隔离,
                # 改投 A->B 时文件归属自动正确)。
                if inbound.media_type == "image" and media_bytes:
                    dify_file_image_bytes = media_bytes
                    _slug = (inbound.msgid or "img")[-8:]
                    dify_file_image_name = f"wechat_image_{_slug}.png"
                    if trace is not None:
                        trace.event(
                            "media", "ok",
                            f"image ready size={len(media_bytes)}B",
                        )
                    logger.info(
                        "[PROC] bot image 待传: msgid=%s size=%dB",
                        inbound.msgid, len(media_bytes),
                    )
                # voice ASR (media_id 路径): 下载+AMR->WAV+ASR -> transcript
                # Dify 无 ASR 节点, 语音在 wecom 侧转文本 (见 asr.py)
                elif inbound.media_type == "voice" and media_bytes:
                    v_info = await self._media.download_and_process_media(
                        inbound.media_ref, "voice"
                    )
                    v_wav = (None if v_info.get("error")
                             else v_info.get("wav_path") or v_info.get("file_path"))
                    if v_wav:
                        from app.services.asr import transcribe as asr_transcribe
                        bot_voice_transcript = await asr_transcribe(v_wav)
                    if trace is not None:
                        trace.event(
                            "media", "ok",
                            f"voice ASR: {bot_voice_transcript[:40]}",
                        )
                    logger.info(
                        "[PROC] bot voice ASR: msgid=%s -> %s",
                        inbound.msgid, bot_voice_transcript[:50],
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
        if dify_file_image_bytes:
            input_data["file_image_bytes"] = dify_file_image_bytes
            input_data["file_image_name"] = dify_file_image_name
        if bot_voice_transcript:
            input_data["text"] = bot_voice_transcript
        if "text" not in input_data:
            input_data["text"] = (
                "[image]"
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

    async def _chatwoot_notify_inbound(self, inbound: InboundMessage) -> None:
        """幂等同步客户入站消息到 Chatwoot (origin=1)。

        在 handoff 检查前调用, 确保人工接管时也能看到触发消息。msgid 用
        ``inbound.msgid`` (与出站 ``_out`` 区分, 避免 Chatwoot source_id 唯一约束冲突)。
        """
        try:
            from app.services.chatwoot_sync_service import ChatwootSyncService

            sync = ChatwootSyncService()
            try:
                content = inbound.text or (
                    "[图片]" if inbound.media_type == "image"
                    else "[语音]" if inbound.media_type == "voice"
                    else ""
                )
                await sync.notify_incoming(
                    open_kfid=inbound.open_kfid,
                    external_userid=inbound.user_id,
                    message_data={
                        "msgid": inbound.msgid,
                        "msgtype": inbound.msg_type,
                        "text": {"content": content},
                        "origin": 1,
                    },
                )
            finally:
                await sync.aclose()
        except Exception as e:
            logger.error(
                "[ChatwootSync] 入站同步失败 (非致命, 不影响 WeCom 流程): %s", e
            )

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
                        # origin=2 出站 (AI 回复); msgid 加 _out 后缀, 与入站 (origin=1)
                        # inbound.msgid 区分, 避免 Chatwoot source_id 唯一约束冲突 (E7)。
                        "msgid": f"{inbound.msgid}_out",
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


__all__ = ["MessageProcessor", "PreparedInput"]
