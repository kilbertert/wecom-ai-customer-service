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


async def _safe_process(
    processor, inbound, adapter, max_attempts: int | None = None
) -> None:
    """内存派发兜底: 隔离异常并做有限重试。

    ``MessageProcessor.process`` 会重抛真异常供 Redis 队列 retry/dead-letter。Redis
    不可用或显式 memory 模式下没有持久队列，因此这里按 ``APP_QUEUE_MAX_ATTEMPTS``
    做进程内有限重试；最终失败只记录，不让 Starlette ``BackgroundTasks`` 的首条异常
    中断同一批后续 KF 消息。``CancelledError`` 仍重抛以正确标记 shutdown。
    """
    if max_attempts is None:
        from app.core.config import settings

        max_attempts = int(getattr(settings.app, "queue_max_attempts", 3) or 3)
    max_attempts = max(1, max_attempts)

    for attempt in range(1, max_attempts + 1):
        try:
            await processor.process(inbound, adapter)
            return
        except asyncio.CancelledError:
            logger.info(
                "[MEMORY] 后台处理被取消 (shutdown): msgid=%s", inbound.msgid
            )
            raise
        except Exception as e:
            if attempt >= max_attempts:
                logger.error(
                    "[MEMORY] 后台处理失败且重试耗尽: msgid=%s attempts=%s, %s",
                    inbound.msgid, max_attempts, e, exc_info=True,
                )
                return
            delay = min(2 ** (attempt - 1), 5)
            logger.warning(
                "[MEMORY] 后台处理失败, %.1fs 后重试 %s/%s: msgid=%s, %s",
                delay, attempt, max_attempts, inbound.msgid, e,
            )
            await asyncio.sleep(delay)


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
    queue = getattr(request.app.state, "message_queue", None)

    try:
        inbound_list = await adapter.receive(request)
        for inbound in inbound_list:
            if queue is not None and await queue.enqueue(inbound, "kf"):
                logger.info(f"[KF] 入持久队列: msgid={inbound.msgid}")
            else:
                # queue is None (memory 模式) 或入队失败 (Redis 宕机) -> 内存派发兜底,
                # 避免微信已 ACK 却丢消息 (审查 P1 #2)。
                logger.info(f"[KF] 入后台处理: msgid={inbound.msgid}")
                background_tasks.add_task(_safe_process, processor, inbound, adapter)
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
    queue = getattr(request.app.state, "message_queue", None)

    try:
        inbound_list = await adapter.receive(request)
        if not inbound_list:
            return Response(status_code=400, content="missing/invalid encrypt")

        inbound = inbound_list[0]

        # 去重由 MessageProcessor.process 统一负责 (与 KF 路径一致); route 层不再
        # 重复 acquire —— 否则 process() 的 acquire 必然返回 False, 整条消息被跳过。

        if queue is not None and await queue.enqueue(inbound, "bot"):
            # 持久队列模式 (#15): 入队后立即返回占位 envelope, worker 异步消费。
            logger.info(f"[BOT] msgid={inbound.msgid} 入持久队列, 返回占位 envelope")
        else:
            # queue is None (memory 模式) 或入队失败 (Redis 宕机) -> 内存派发兜底
            # (审查 P1 #2)。_safe_process 兜底日志: process 现重抛异常 (审查 P1 #3),
            # 需捕获避免 asyncio un-retrieved-exception 警告。
            asyncio.create_task(
                _safe_process(processor, inbound, adapter),
                name=f"bot-{inbound.msgid[:8] if inbound.msgid else 'noid'}",
            )
            logger.info(f"[BOT] msgid={inbound.msgid} 已创建后台任务, 返回占位 envelope")

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
