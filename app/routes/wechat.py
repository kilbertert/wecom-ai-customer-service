"""微信回调路由 (薄分发器)。

业务逻辑全部委托给协议适配器 (KfAdapter / BotAdapter) 与编排器
(MessageProcessor), 路由层只负责: HTTP 入参解析 → adapter.receive →
后台分发 processor.process → 返回同步 ACK。

协议:
    - ``/wechat/kf/callback``  : 微信客服 (XML + sync_msg 拉取, BackgroundTasks)
    - ``/wechat/bot/callback`` : 智能机器人 (JSON envelope + response_url 推送,
                                 asyncio.create_task 防 5s 超时)
"""

import asyncio
import logging

from fastapi import APIRouter, BackgroundTasks, Query, Request, Response
from fastapi.responses import PlainTextResponse

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/wechat", tags=["wechat"])


# ============================================================================
# 微信客服 (KF) 回调
# ============================================================================


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


# ============================================================================
# 智能机器人 (bot) 回调
# ============================================================================


@router.get("/bot/callback")
async def bot_callback_verify(
    request: Request,
    msg_signature: str = Query(..., description="消息签名(SHA1)"),
    timestamp: str = Query(..., description="时间戳"),
    nonce: str = Query(..., description="随机数"),
    echostr: str = Query(..., description="回显字符串(加密)"),
):
    """智能机器人 URL 验证 (委托 BotAdapter.verify_url, receive_id="")。"""
    adapter = request.app.state.bot_adapter
    plaintext = adapter.verify_url(msg_signature, timestamp, nonce, echostr)
    if plaintext is None:
        return Response(
            status_code=403,
            content="signature verification failed",
            media_type="text/plain",
            headers={"Content-Type": "text/plain"},
        )
    return Response(
        status_code=200,
        content=plaintext,
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
    """智能机器人消息回调 (瘦分发器)。

    协议: POST body 是 JSON ``{"encrypt": "B64..."}``; 解密 + dedup 后立即返回
    加密 envelope (占位 markdown), Dify workflow 调用移入后台 task, 防 5s 超时
    重试与 errcode 60140 重复响应。
    """
    adapter = request.app.state.bot_adapter
    processor = request.app.state.message_processor

    try:
        inbound_list = await adapter.receive(request)
        if not inbound_list:
            return Response(status_code=400, content="missing/invalid encrypt")

        inbound = inbound_list[0]

        # dedup: 同一 msgid 在 ttl 内不重复处理 (立即回占位 envelope)
        dedup = adapter.dedup
        if not await dedup.acquire(inbound.msgid, adapter.dedup_ttl):
            logger.info(
                f"[BOT] msgid={inbound.msgid} 已处理过 (dedup), 返回占位 envelope"
            )
            placeholder = adapter.build_sync_ack(timestamp, nonce)
            return Response(
                status_code=200, content=placeholder, media_type="application/json"
            )

        # 后台异步处理 (Dify 跑 30-50s, 不能同步等)
        asyncio.create_task(
            processor.process(inbound, adapter),
            name=f"bot-{inbound.msgid[:8] if inbound.msgid else 'noid'}",
        )
        logger.info(
            f"[BOT] msgid={inbound.msgid} 已创建后台任务, 返回占位 envelope"
        )

        placeholder = adapter.build_sync_ack(timestamp, nonce)
        return Response(
            status_code=200, content=placeholder, media_type="application/json"
        )
    except Exception as e:
        logger.error(f"[BOT] 处理异常: {e}", exc_info=True)
        return Response(status_code=500, content=f"error: {e}")


# ============================================================================
# 测试
# ============================================================================


@router.get("/test")
async def test_endpoint():
    """测试接口"""
    return {"status": "ok", "message": "WeChat callback service is running"}
