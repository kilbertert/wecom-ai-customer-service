"""微信回调路由"""

import asyncio
import logging
import time
import xml.etree.ElementTree as ET
from typing import Any, Dict

from fastapi import APIRouter, BackgroundTasks, Query, Request, Response
from fastapi.responses import PlainTextResponse

from app.core.config import settings
from app.services import get_ai_service
from app.services.bot_trace import (
    BotTrace,
    format_knowledge_lines,
    format_thinking_lines,
)
from app.services.multimodal import compose_multimodal_markdown
from app.services.wechat import WeChatService

# 智能机器人后台任务 dedup: 同一 msgid 在 ttl 秒内不重复触发
_bot_processed_msgs: Dict[str, float] = {}
_bot_dedup_lock = asyncio.Lock()
_BOT_MSG_TTL = 600  # 10 分钟内不重复处理同一 msgid (防止 WeChat 重试风暴)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/wechat", tags=["wechat"])


@router.get("/kf/callback")
async def wechat_callback_verify(
    request: Request,
    msg_signature: str = Query(..., description="消息签名"),
    timestamp: str = Query(..., description="时间戳"),
    nonce: str = Query(..., description="随机数"),
    echostr: str = Query(..., description="加密的验证字符串"),
):
    """微信回调URL验证 (委托 KfAdapter.verify_url)。"""
    adapter = request.app.state.kf_adapter
    try:
        plaintext = adapter.verify_url(msg_signature, timestamp, nonce, echostr)
        if plaintext is not None:
            return PlainTextResponse(content=plaintext, media_type="text/plain")
        # 验签或解密失败: 回退返回原始 echostr (与历史行为一致)
        logger.warning("[KF VERIFY] 失败, 回退返回原始 echostr")
        return PlainTextResponse(content=echostr, media_type="text/plain")
    except Exception as e:
        logger.error(f"[KF VERIFY] 异常: {e}", exc_info=True)
        return PlainTextResponse(
            content="verification error", media_type="text/plain"
        )


@router.post("/kf/callback")
async def wechat_callback_handler(
    request: Request,
    background_tasks: BackgroundTasks,
    msg_signature: str = Query(..., description="消息签名"),
    timestamp: str = Query(..., description="时间戳"),
    nonce: str = Query(..., description="随机数"),
):
    """微信回调消息处理 (瘦分发器: 验签/解密/同步全委托 KfAdapter, 处理入后台)。

    任何内部错误都回 ``success`` 给微信, 防止重试风暴; 真实业务在后台任务跑。
    """
    # User-Agent 过滤 (仅放行微信流量)
    user_agent = request.headers.get("User-Agent")
    allowed_user_agents = ["WeChat", "Mozilla/4.0"]
    if not any(agent in (user_agent or "") for agent in allowed_user_agents):
        logger.warning(f"无效的请求来源，User-Agent: {user_agent}")
        return PlainTextResponse(content="success", media_type="text/plain")

    adapter = request.app.state.kf_adapter
    processor = request.app.state.message_processor

    try:
        inbound_list = await adapter.receive(request)
        for inbound in inbound_list:
            logger.info(f"[KF] 入后台处理: msgid={inbound.msgid}")
            background_tasks.add_task(processor.process, inbound, adapter)
    except Exception as e:
        logger.error(f"[KF] 回调处理异常: {e}", exc_info=True)

    # 恒返回 success (后台任务跑完前先 ACK, 防 5s 超时)
    return PlainTextResponse(content="success", media_type="text/plain")


@router.get("/test")
async def test_endpoint():
    """测试接口"""
    return {"status": "ok", "message": "WeChat callback service is running"}


# ============================================================================
# 微信群机器人回调（区别于客服：明文 JSON 协议，无需 AES 解密）
# ============================================================================


@router.get("/bot/callback")
async def bot_callback_verify(
    msg_signature: str = Query(..., description="消息签名(SHA1)"),
    timestamp: str = Query(..., description="时间戳"),
    nonce: str = Query(..., description="随机数"),
    echostr: str = Query(..., description="回显字符串(加密)"),
):
    """企业微信智能机器人 URL 验证

    与客服不同: 智能机器人的 echostr 是 AES 加密字符串
    服务端必须:
      1) SHA1(sort([token, timestamp, nonce, echostr])) 验签
      2) AES 解密 echostr (receive_id="", 企业自建)
      3) 返回解密后的 msg 明文
    """
    logger.info(
        f"[BOT VERIFY] timestamp={timestamp} nonce={nonce} echostr_len={len(echostr)}"
    )
    try:
        svc = WeChatService()
        # 1) 验签（用加密的 echostr 原文参与签名计算）
        if not svc.verify_bot_signature(msg_signature, timestamp, nonce, echostr):
            logger.warning("[BOT VERIFY] 签名验证失败")
            return Response(
                status_code=403,
                content="signature verification failed",
                media_type="text/plain",
                headers={"Content-Type": "text/plain"},
            )

        # 2) AES 解密 echostr —— 企业自建智能机器人 receive_id 传空字符串 ""
        aes_key = svc.config.kf_encoding_aes_key
        try:
            plaintext_msg = WeChatService.decrypt_message_custom(
                echostr, aes_key, ""  # receive_id=""
            )
        except Exception as e:
            logger.error(f"[BOT VERIFY] AES 解密 echostr 失败: {e}")
            return Response(
                status_code=500,
                content=f"decrypt failed: {e}",
                media_type="text/plain",
                headers={"Content-Type": "text/plain"},
            )

        logger.info(f"[BOT VERIFY] 验签+解密通过, 返回明文 (长度 {len(plaintext_msg)})")

        # 3) 返回明文 msg —— 不能加引号/BOM/换行
        return Response(
            status_code=200,
            content=plaintext_msg,
            media_type="text/plain",
            headers={"Content-Type": "text/plain"},
        )
    except Exception as e:
        logger.error(f"[BOT VERIFY] 异常: {e}")
        return Response(
            status_code=500,
            content=f"error: {e}",
            media_type="text/plain",
            headers={"Content-Type": "text/plain"},
        )


@router.post("/bot/callback")
async def bot_callback_handler(
    request: Request,
    msg_signature: str = Query(..., description="消息签名(SHA1)"),
    timestamp: str = Query(..., description="时间戳"),
    nonce: str = Query(..., description="随机数"),
):
    """企业微信智能机器人消息回调 (一期重做: 异步化 + dedup)

    协议关键点（与客服不同）:
      - POST body 是 JSON `{"encrypt": "B64_..."}`（不是 XML）
      - 签名: SHA1(sort([token, timestamp, nonce, encrypt_value]))
      - 解密: AES-256-CBC, receive_id="" (企业自建固定空字符串)
      - 解密后是 JSON: {msgid, aibotid, chattype, from.userid, msgtype, text.content, response_url, ...}
      - 异步响应 (response_url): aibot/response 接口只接受 msgtype="markdown"

    一期改造 (2026-06-24):
      - 解密 + dedup 检查后立即返回占位 envelope, Dify workflow 调用移入后台 task
      - 防止智能机器人 5s callback 超时重试 (Dify 跑 30-50s)
      - 防止同一 msgid 被多次处理 (errcode 60140 重复响应)
    """
    import json as _json

    body_str = (await request.body()).decode("utf-8", errors="ignore")
    logger.info(f"[BOT MSG] ts={timestamp} nonce={nonce} body_len={len(body_str)}")

    try:
        # 1) 解析外部 JSON + 验签
        try:
            data = _json.loads(body_str)
        except _json.JSONDecodeError as e:
            return Response(status_code=400, content=f"invalid json: {e}")
        msg_encrypt = (data.get("encrypt") or "").strip()
        if not msg_encrypt:
            return Response(status_code=400, content="missing encrypt")

        svc = WeChatService()
        if not svc.verify_bot_signature(msg_signature, timestamp, nonce, msg_encrypt):
            return Response(status_code=403, content="signature verification failed")
        logger.info("[BOT MSG] 签名验证通过")

        # 2) AES 解密 (receive_id="")
        try:
            decrypted = WeChatService.decrypt_message_custom(
                msg_encrypt, svc.config.kf_encoding_aes_key, ""
            )
        except Exception as e:
            logger.error(f"[BOT MSG] AES 解密失败: {e}")
            return Response(status_code=500, content=f"decrypt failed: {e}")

        # 3) 解析内层 JSON 消息
        try:
            msg = _json.loads(decrypted)
        except _json.JSONDecodeError as e:
            logger.error(f"[BOT MSG] 内层 JSON 解析失败: {e}")
            return Response(status_code=500, content=f"invalid decrypted json: {e}")

        from_user = (msg.get("from") or {}).get("userid") or "unknown"
        msg_type = msg.get("msgtype") or ""
        msg_id = msg.get("msgid") or ""
        text_obj = msg.get("text") or {}
        content = (
            text_obj.get("content") if isinstance(text_obj, dict) else text_obj
        ) or ""
        content = str(content).strip()
        response_url = msg.get("response_url") or ""
        # 会话类型: "single" (单聊) | "group" (群聊) — 用于 trace 头部标记
        chattype_raw = (msg.get("chattype") or "single").strip().lower()
        chattype = "group" if chattype_raw == "group" else "single"

        # 一期增强: 识别 image / voice / mixed 消息, 提取媒体定位符
        # 关键差异:
        #   - 智能机器人 image 消息: msg.image.url  (微信 CDN 直链 URL, 直接 httpx.GET)
        #   - 智能机器人 voice 消息: msg.voice.media_id  (走 /cgi-bin/media/get 下载)
        #   - 智能机器人 mixed 消息 (图文混合): msg.mixed 包含 text + image 等子结构
        #   - 客服 kf 两种都有, 但目前 bot 路径只走智能机器人
        # 后台任务会用 httpx 直接 GET url 拿 bytes, 或调 WeChatService.download_media 拿 bytes
        wechat_media_ref = ""
        wechat_media_kind = ""  # "url" | "media_id"
        # effective_media_type 用于后台任务的 media 上传分支判断
        # - msg_type=image  → "image"
        # - msg_type=voice  → "voice"
        # - msg_type=mixed  → mixed 里 image 子项时 "image", voice 时 "voice"
        # - msg_type=text   → ""
        effective_media_type = ""
        if msg_type == "image":
            image_obj = msg.get("image") or {}
            if isinstance(image_obj, dict):
                url_val = (image_obj.get("url") or "").strip()
                mid_val = (image_obj.get("media_id") or "").strip()
                if url_val:
                    wechat_media_ref = url_val
                    wechat_media_kind = "url"
                elif mid_val:
                    wechat_media_ref = mid_val
                    wechat_media_kind = "media_id"
            effective_media_type = "image" if wechat_media_ref else ""
        elif msg_type == "voice":
            voice_obj = msg.get("voice") or {}
            if isinstance(voice_obj, dict):
                mid_val = (voice_obj.get("media_id") or "").strip()
                if mid_val:
                    wechat_media_ref = mid_val
                    wechat_media_kind = "media_id"
            effective_media_type = "voice" if wechat_media_ref else ""
        elif msg_type == "mixed":
            # 智能机器人图文混合消息实际结构 (DIAG 确认):
            #   {msgtype: "mixed", mixed: {msg_item: [{msgtype, ...}, ...]}}
            # msg_item 是数组, 每个元素像单条消息: {msgtype, text:{content}, image:{url}} 等
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
                    elif itype == "image" and not wechat_media_ref:
                        img = item.get("image") or {}
                        if isinstance(img, dict):
                            url_val = (img.get("url") or "").strip()
                            mid_val = (img.get("media_id") or "").strip()
                            if url_val:
                                wechat_media_ref = url_val
                                wechat_media_kind = "url"
                                effective_media_type = "image"
                            elif mid_val:
                                wechat_media_ref = mid_val
                                wechat_media_kind = "media_id"
                                effective_media_type = "image"
                    elif itype == "voice" and not wechat_media_ref:
                        v = item.get("voice") or {}
                        if isinstance(v, dict):
                            mid_val = (v.get("media_id") or "").strip()
                            if mid_val:
                                wechat_media_ref = mid_val
                                wechat_media_kind = "media_id"
                                effective_media_type = "voice"

        logger.info(
            f"[BOT MSG] from={from_user} type={msg_type} "
            f"content_len={len(content)} id={msg_id} "
            f"media_ref={wechat_media_kind}:{bool(wechat_media_ref)}"
        )

        # 4) dedup 检查 (一期新增): 同一 msgid 短时间内不重复处理
        now_ts = time.time()
        async with _bot_dedup_lock:
            # 清理过期记录
            expired = [
                k for k, v in _bot_processed_msgs.items() if now_ts - v > _BOT_MSG_TTL
            ]
            for k in expired:
                _bot_processed_msgs.pop(k, None)
            if msg_id and msg_id in _bot_processed_msgs:
                logger.info(
                    f"[BOT MSG] msgid {msg_id} 已处理过 (dedup), 立即返回占位 envelope"
                )
                placeholder = _build_bot_sync_envelope(
                    svc, "AI 正在处理中...", timestamp, nonce
                )
                return Response(
                    status_code=200, content=placeholder, media_type="application/json"
                )
            # 标记处理中
            if msg_id:
                _bot_processed_msgs[msg_id] = now_ts

        # 5) 创建后台任务异步处理 (Dify 跑 30-50s, 不能同步等)
        asyncio.create_task(
            _process_bot_message_background(
                msg=msg,
                from_user=from_user,
                msg_type=msg_type,
                effective_media_type=effective_media_type,
                content=content,
                msg_id=msg_id,
                response_url=response_url,
                wechat_media_ref=wechat_media_ref,
                wechat_media_kind=wechat_media_kind,
                timestamp=timestamp,
                nonce=nonce,
                encoding_aes_key=svc.config.kf_encoding_aes_key,
                kf_token=svc.config.kf_token,
                chattype=chattype,
            ),
            name=f"bot-{msg_id[:8] if msg_id else 'noid'}",
        )
        logger.info(f"[BOT MSG] msgid={msg_id} 已创建后台任务, 立即返回占位 envelope")

        # 6) 立即返回占位 envelope (避免 WeChat 5s 超时重试)
        placeholder = _build_bot_sync_envelope(
            svc, "AI 正在处理中...", timestamp, nonce
        )
        return Response(
            status_code=200,
            content=placeholder,
            media_type="application/json",
        )
    except Exception as e:
        import traceback

        logger.error(f"[BOT MSG] 处理异常: {e}\n{traceback.format_exc()}")
        return Response(status_code=500, content=f"error: {e}")


# ============================================================================
# 智能机器人后台处理 + dedup (一期重做: 避免 Dify 慢调用导致 5s 超时重试)
# ============================================================================


async def _process_bot_message_background(
    msg: dict,
    from_user: str,
    msg_type: str,
    effective_media_type: str,
    content: str,
    msg_id: str,
    response_url: str,
    wechat_media_ref: str,
    wechat_media_kind: str,
    timestamp: str,
    nonce: str,
    encoding_aes_key: str,
    kf_token: str,
    chattype: str = "single",
):
    """后台异步处理 bot 消息: 调 AI workflow → 拼 markdown → 推 response_url。

    注: Dify 工作流跑 30-50s, 必须异步处理, 否则智能机器人 callback 5s 超时
    会触发 WeChat 重试, 导致同一个 response_url 被推多次 (errcode 60140)。

    媒体编排:
        - wechat_media_kind="url"        → httpx.GET(wechat_media_ref) 拿 bytes
        - wechat_media_kind="media_id"   → WeChatService.download_media 拿 bytes
    """
    import json as _json

    import httpx as _httpx

    # 决策日志 trace (可拔插, 默认 off): 记录本次消息经过的关键阶段
    # 渲染/推送由 settings.app.bot_trace_mode 决定
    trace = BotTrace(chat_type=chattype, msg_type=msg_type)
    trace.event(
        "receive",
        "ok",
        f"from={from_user} id={(msg_id or '')[:12]}",
    )
    # dedup 在 bot_callback_handler 已通过 (否则不会进 bg 任务), 此处记 ok 作 7 阶段标记
    trace.event("dedup", "ok", "首次处理")
    try:
        # 0) 一期增强: image / voice 媒体编排
        # 智能机器人 image (url 路径): 直接用微信 CDN URL 走 Dify remote_url 模式 (跳过上传)
        # 客服 kf image / 智能机器人 voice (media_id 路径): 下载后上传 Dify 走 local_file 模式
        # dify.py 优先 file_image_url > file_image_id
        dify_file_image_url = ""
        dify_file_image_id = ""
        dify_file_voice_id = ""
        if wechat_media_ref:
            try:
                media_bytes: bytes = b""
                if wechat_media_kind == "url":
                    # 智能机器人 image: 不下载!直接用 CDN URL 喂 Dify remote_url 模式
                    # 仅 voice 需要先下载(转码可能要考虑), 暂不在 url 路径做 voice
                    if effective_media_type == "image":
                        dify_file_image_url = wechat_media_ref
                        trace.event(
                            "media",
                            "ok",
                            f"image remote_url len={len(wechat_media_ref)}",
                        )
                        logger.info(
                            f"[BOT BG] image 走 remote_url 模式: msgid={msg_id}, "
                            f"url={wechat_media_ref[:60]}..."
                        )
                    else:
                        # voice url 路径暂未用, 走通用下载
                        async with _httpx.AsyncClient(timeout=30.0) as ac:
                            r = await ac.get(wechat_media_ref)
                            r.raise_for_status()
                            media_bytes = r.content
                elif wechat_media_kind == "media_id":
                    # 客服 kf / voice: 调 WeChatService.download_media
                    svc = WeChatService()
                    media_bytes = await svc.download_media(wechat_media_ref)
                else:
                    raise RuntimeError(f"未知的 wechat_media_kind: {wechat_media_kind}")

                if effective_media_type == "image" and not dify_file_image_url:
                    # 仅 media_id 路径需要上传 (url 路径已用 remote_url)
                    dify_file_image_id = await _upload_to_dify_file_store(
                        media_bytes,
                        wechat_media_ref,
                        "image",
                    )
                    trace.event(
                        "media",
                        "ok",
                        f"image uploaded size={len(media_bytes)}B",
                    )
                    logger.info(
                        f"[BOT BG] image 上传 Dify 成功: msgid={msg_id}, "
                        f"kind={wechat_media_kind}, dify_file_id={dify_file_image_id}, "
                        f"size={len(media_bytes)}B"
                    )
                elif effective_media_type == "voice":
                    # voice 当前只支持 media_id 路径
                    dify_file_voice_id = await _upload_to_dify_file_store(
                        media_bytes,
                        wechat_media_ref,
                        "audio",
                    )
                    trace.event(
                        "media",
                        "ok",
                        f"voice uploaded size={len(media_bytes)}B",
                    )
                    logger.info(
                        f"[BOT BG] voice 上传 Dify 成功: msgid={msg_id}, "
                        f"dify_file_id={dify_file_voice_id}, size={len(media_bytes)}B"
                    )
            except Exception as e:
                logger.error(f"[BOT BG] 媒体编排失败: msgid={msg_id}, {e}")
                trace.event("media", "fail", str(e)[:80])
        else:
            trace.event("media", "skip", "无媒体")

        # 1) 调 AI workflow
        # 支持的 msgtype: text / image / voice / mixed (图文混合, content + media_ref 都有)
        if msg_type not in ("text", "image", "voice", "mixed") or (
            msg_type in ("text", "mixed") and not content and not wechat_media_ref
        ):
            reply_text = (
                "收到不支持的消息类型"
                if msg_type not in ("text", "image", "voice", "mixed")
                else "收到空消息"
            )
            trace.event("prefilter", "fail", f"{msg_type} 不支持/空")
            trace.event("knowledge", "skip", "无知识库检索")
            trace.event("thinking", "skip", "无思考过程")
            trace.event("ai", "skip", "无 AI 调用")
        else:
            # 预过滤通过
            detail_extra = ""
            if content:
                detail_extra = f"text={len(content)}字"
            if wechat_media_ref:
                detail_extra += f" media={effective_media_type or '?'}"
            trace.event("prefilter", "ok", detail_extra.strip() or "ok")
            # 单轮模式: 无历史/无会话
            trace.event("context", "skip", "单轮模式,无历史")

            input_data: Dict[str, Any] = {"user_id": from_user}
            if content:
                input_data["text"] = content
            if dify_file_image_url:
                input_data["file_image_url"] = dify_file_image_url
            elif dify_file_image_id:
                input_data["file_image_id"] = dify_file_image_id
            if dify_file_voice_id:
                input_data["file_voice_id"] = dify_file_voice_id
            if "text" not in input_data:
                # image/voice 没附文本时给个提示, 让 LLM 知道要看图/听音
                input_data["text"] = (
                    "[用户发了一张图片]"
                    if msg_type == "image"
                    else "[用户发了一段语音]" if msg_type == "voice" else ""
                )

            ai = get_ai_service()
            try:
                wf = await ai.run_workflow(input_data, user_id=from_user)
                logger.info(
                    f"[BOT BG] AI 返回, msgid={msg_id}, 键="
                    f"{list(wf.keys()) if isinstance(wf, dict) else type(wf).__name__}"
                )

                # 提取 Dify workflow 原始 outputs (用于思考过程/知识库检索阶段)
                _raw = (wf or {}).get("raw", {}) if isinstance(wf, dict) else {}
                _outputs = ((_raw or {}).get("data") or {}).get("outputs") or {}

                # 知识库检索: 从 Dify outputs 中提取检索结果, 渲染为多行 detail
                _knowledge_data = _extract_knowledge_from_outputs(_outputs)
                if _knowledge_data is not None:
                    _kb_main, _kb_subs = format_knowledge_lines(_knowledge_data)
                    trace.event("knowledge", "ok", _kb_main, sub_lines=_kb_subs)
                else:
                    trace.event("knowledge", "skip", "无知识库检索")

                # 思考过程: 从 Dify outputs 中提取 LLM reasoning/thinking 文本, 拆分为步骤
                _thinking_text = _extract_thinking_from_outputs(_outputs)
                if _thinking_text:
                    _th_main, _th_subs = format_thinking_lines(_thinking_text)
                    trace.event("thinking", "ok", _th_main, sub_lines=_th_subs)
                else:
                    trace.event("thinking", "skip", "无思考过程")

                reply_text = compose_multimodal_markdown(wf)
                # AI 摘要: 文本长度 + 媒体计数
                ai_detail = f"text={len(reply_text)}字"
                if isinstance(wf, dict):
                    media_counts = []
                    for kind in ("images", "videos", "files"):
                        cnt = len(wf.get(kind) or [])
                        if cnt:
                            media_counts.append(f"{kind[0]}{cnt}")
                    if media_counts:
                        ai_detail += " " + " ".join(media_counts)
                trace.event("ai", "ok", ai_detail)
            except Exception as e:
                logger.error(f"[BOT BG] AI 失败: msgid={msg_id}, {e}")
                reply_text = f"AI 处理失败: {e}"
                trace.event("knowledge", "skip", "无知识库检索")
                trace.event("thinking", "skip", "无思考过程")
                trace.event("ai", "fail", str(e)[:80])

        if not reply_text:
            reply_text = "（AI 未返回内容）"
        logger.info(
            f"[BOT BG] msgid={msg_id}, reply_len={len(reply_text)}, "
            f"content[:80]={reply_text[:80]}"
        )

        # 2) 推 response_url (msgtype 必须 markdown)
        # 决策日志模式: off(默认) | inline(拼到主消息) | separate(再推一条)
        trace_mode = (getattr(settings.app, "bot_trace_mode", "off") or "off").lower()
        trace_max_len = getattr(settings.app, "bot_trace_max_len", 1500) or 1500

        if response_url:
            final_reply = reply_text
            if trace_mode == "inline":
                trace_text = trace.render("inline", max_len=trace_max_len)
                if trace_text:
                    final_reply = reply_text + trace_text

            payload = {"msgtype": "markdown", "markdown": {"content": final_reply}}
            try:
                async with _httpx.AsyncClient(timeout=30.0) as ac:
                    r = await ac.post(
                        response_url,
                        json=payload,
                        headers={"Content-Type": "application/json"},
                    )
                try:
                    errcode = _json.loads(r.text).get("errcode")
                except _json.JSONDecodeError:
                    errcode = None
                push_ok = r.status_code == 200 and errcode in (0, None)
                trace.event(
                    "push",
                    "ok" if push_ok else "fail",
                    f"HTTP {r.status_code} errcode={errcode}",
                )
                logger.info(
                    f"[BOT BG] 异步推送: msgid={msg_id}, "
                    f"HTTP {r.status_code} errcode={errcode}"
                )
            except Exception as e:
                logger.error(f"[BOT BG] 异步推送异常: msgid={msg_id}, {e}")
                trace.event("push", "fail", str(e)[:80])

            # separate 模式: 主消息已发出, 第二次 POST 推 trace (失败仅 warning)
            if trace_mode == "separate":
                await _post_bot_trace_separate(
                    response_url=response_url,
                    trace=trace,
                    max_len=trace_max_len,
                    msg_id=msg_id,
                    httpx_module=_httpx,
                )
    except Exception as e:
        import traceback

        logger.error(
            f"[BOT BG] 后台任务异常: msgid={msg_id}, {e}\n{traceback.format_exc()}"
        )


def _build_bot_sync_envelope(svc, reply_text: str, timestamp: str, nonce: str) -> str:
    """构造智能机器人同步响应 envelope (加密 JSON 字符串)。

    智能机器人 aibot/response 接口文档: 加密 JSON 信封 (encrypt + msgsignature + timestamp + nonce),
    内部明文是 {"msgtype": "markdown", "markdown": {"content": ...}}。
    """
    import json as _json

    try:
        envelope_xml = WeChatService.encrypt_message_custom(
            reply_xml=_json.dumps(
                {"msgtype": "markdown", "markdown": {"content": reply_text}},
                ensure_ascii=False,
            ),
            encoding_aes_key=svc.config.kf_encoding_aes_key,
            corp_id="",
            timestamp=timestamp,
            nonce=nonce,
            token=svc.config.kf_token,
        )
        env_root = ET.fromstring(envelope_xml)
        return _json.dumps(
            {
                "encrypt": env_root.findtext("Encrypt"),
                "msgsignature": env_root.findtext("MsgSignature"),
                "timestamp": timestamp,
                "nonce": nonce,
            },
            ensure_ascii=False,
        )
    except Exception as e:
        logger.error(f"[BOT] 同步 envelope 构建失败: {e}")
        return "{}"


async def _upload_to_dify_file_store(
    content: bytes,
    wechat_media_ref: str,
    file_type: str,
) -> str:
    """把微信临时素材字节流上传到 Dify 文件库, 拿到 Dify upload_file_id。

    Dify workflow inputs file 型字段需要:
        [{"type": "image", "transfer_method": "local_file", "upload_file_id": "<dify_file_id>"}]
    dify.py run_workflow 会用 client.file_ref(dify_file_id, "image") 构造上述结构,
    所以这里只负责把微信的临时素材字节流推上 Dify 拿到 file_id 即可。

    Args:
        content: 微信临时素材的 bytes
        wechat_media_ref: 微信 media_id 或 url 字符串 (仅用于生成可读文件名)
        file_type: "image" | "audio" (Dify file_ref 的 type 字段)

    Returns:
        Dify upload_file_id (UUID 字符串), 用于喂给 dify.py run_workflow

    Raises:
        RuntimeError: 当前 AI 后端不是 Dify (无 client.upload_file) 或 Dify 上传失败
    """
    ai = get_ai_service()
    client = getattr(ai, "client", None)
    if client is None or not hasattr(client, "upload_file"):
        raise RuntimeError(
            f"当前 AI 后端 ({type(ai).__name__}) 不支持文件上传, "
            f"image/voice 消息转发无法工作"
        )

    # 文件名: 兼容 media_id (hex) 和 url (https://... 路径段) 两种
    ext_map = {"image": "jpg", "audio": "amr"}
    ext = ext_map.get(file_type, "bin")
    if "://" in wechat_media_ref:
        # URL: 取最后一段路径, 去掉 query/fragment
        from urllib.parse import urlparse

        path = urlparse(wechat_media_ref).path
        slug = path.rsplit("/", 1)[-1] or "file"
        slug = slug[:20]  # 截短避免文件名过长
        filename = f"wechat_{file_type}_{slug}.{ext}"
    else:
        # media_id: 看起来是 hex 串
        filename = f"wechat_{file_type}_{wechat_media_ref[:12]}.{ext}"
    mime_map = {
        "image": "image/jpeg",
        "audio": "audio/amr",
    }
    content_type = mime_map.get(file_type, "application/octet-stream")

    file_id = await client.upload_file(
        filename=filename,
        content=content,
        content_type=content_type,
    )
    return file_id


async def _post_bot_trace_separate(
    response_url: str,
    trace: "BotTrace",
    max_len: int,
    msg_id: str,
    httpx_module,
) -> None:
    """separate 模式: 主消息已发出后, 再单独 POST 一次 trace 消息。

    这是 best-effort, 失败仅日志 warning, 不影响主消息。
    依赖企业微信允许多次 aibot/response 推送; 文档未明确, 实测为准。
    """
    import json as _json

    try:
        trace_text = trace.render("separate", max_len=max_len)
        if not trace_text:
            return
        payload = {"msgtype": "markdown", "markdown": {"content": trace_text}}
        async with httpx_module.AsyncClient(timeout=15.0) as ac:
            r = await ac.post(
                response_url,
                json=payload,
                headers={"Content-Type": "application/json"},
            )
        try:
            errcode = _json.loads(r.text).get("errcode")
        except _json.JSONDecodeError:
            errcode = None
        logger.info(
            f"[BOT BG] trace 推送: msgid={msg_id}, "
            f"HTTP {r.status_code} errcode={errcode}"
        )
    except Exception as e:
        logger.warning(f"[BOT BG] trace 推送失败 (不影响主消息): msgid={msg_id}, {e}")


def _extract_knowledge_from_outputs(outputs: dict) -> Any:
    """从 Dify workflow outputs 中提取知识库检索结果。

    按优先级尝试常见变量名:
        - knowledge / retrieved_chunks / retrieval_result / context / knowledge_result

    Returns:
        检索结果 (list / str / dict), 未找到返回 None。
    """
    if not isinstance(outputs, dict):
        return None
    _knowledge_keys = [
        "knowledge", "retrieved_chunks", "retrieval_result",
        "context", "knowledge_result", "kb_result",
    ]
    for key in _knowledge_keys:
        val = outputs.get(key)
        if val is not None and val != "" and val != []:
            return val
    return None


def _extract_thinking_from_outputs(outputs: dict) -> str:
    """从 Dify workflow outputs 中提取 LLM 思考过程文本。

    按优先级尝试常见变量名:
        - reasoning_content / thinking / reasoning / thought_process / thought

    Returns:
        思考文本 (已 strip), 未找到返回空字符串。
    """
    if not isinstance(outputs, dict):
        return ""
    _thinking_keys = [
        "reasoning_content", "thinking", "reasoning",
        "thought_process", "thought", "cot",
    ]
    for key in _thinking_keys:
        val = outputs.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return ""
