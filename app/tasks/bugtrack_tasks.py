"""二阶段超时定时器 Celery 任务 (N18/N19)。

当用户进入待确认态 (AWAIT_*) 时, MessageProcessor 会 arm 一个 30 分钟
(1800s) 倒计时任务 ``bugtrack_timeout``。若用户在此窗口内未再发言, 本任务 fire。

**当前行为 (2026-07 调整): fire 时不写任何表。**
旧版会把半成品反馈写入缓存表 (N19), 但会污染主表 (产生大量 "[超时未确认]"
垃圾记录)。现改为: fire 时仅清 PendingTimerStore + 记日志, 半成品内容丢弃
(用户未在 30 分钟窗口内确认, 可接受)。

设计要点:
    - 本任务在 Celery worker 进程内执行 (与 FastAPI 分进程), 用同步 httpx。
    - fire 时先从 PendingTimerStore (Redis) 读元数据确认仍 pending (防重复 fire /
      用户其实已在新窗口内回应但 task 未及时 revoke)。若已清除, 跳过。
    - payload (arm 时序列化传入的半成品内容快照) 当前仅用于日志观测, 不落表。
    - TODO: 后续若需保留半成品, 建独立缓存表 (非主表) 再写。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from app.core.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(
    bind=True,
    name="app.tasks.bugtrack_timeout",
    max_retries=2,
    default_retry_delay=30,
)
def bugtrack_timeout(
    self,
    user_id: str,
    scope: str,
    state: str,
    record_id: str,
    payload: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """30 分钟倒计时 fire: 清 pending + 记日志 (不写表)。

    Args:
        user_id: 用户标识 (WeChat external_userid)
        scope: 会话 scope (open_kfid / "bot") - 用于查 PendingTimerStore
        state: arm 时的 cv_flow_state (await_confirm_*)
        record_id: 主表 record_id (新增超时为空串)
        payload: arm 时的半成品内容快照 (当前仅日志用, 不落表)
    """
    payload = payload or {}
    logger.info(
        "[bugtrack_timeout] fire: user=%s scope=%s state=%s record_id=%s",
        user_id, scope, state, record_id or "(none)",
    )

    # 1) 防重复 fire: 确认 PendingTimerStore 里该 user 仍 pending。
    try:
        from app.services.pending_timer_store import (
            create_pending_timer_store,
        )
        import asyncio
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        async def _check_pending():
            store = create_pending_timer_store()
            return await store.get(user_id, scope)

        if loop and loop.is_running():
            pending = None
        else:
            pending = asyncio.run(_check_pending())

        if pending is None:
            logger.info(
                "[bugtrack_timeout] user=%s 已无 pending timer "
                "(用户已在窗口内回应), 跳过", user_id,
            )
            return {"status": "skipped", "reason": "no_pending"}

        # compare-and-delete: 只清自己 arm 的 timer。若 pending.task_id != 本任务 id,
        # 说明这是被 revoke 的旧任务延迟 fire, 而 store 里已是新 timer -> 不清, 跳过,
        # 防旧任务删新任务 (审查 P1)。注: 完全原子需 RedisPendingTimerStore CAS clear。
        if pending.task_id != self.request.id:
            logger.warning(
                "[bugtrack_timeout] task_id 不匹配 (stored=%s self=%s) "
                "-> 旧任务误 fire, 不清新 timer, 跳过",
                (pending.task_id or "")[:8], (self.request.id or "")[:8],
            )
            return {"status": "skipped", "reason": "task_id_mismatch"}

        async def _clear_pending():
            store = create_pending_timer_store()
            return await store.clear(user_id, scope)
        if not (loop and loop.is_running()):
            asyncio.run(_clear_pending())
    except Exception as e:
        logger.warning(
            "[bugtrack_timeout] pending 检查异常 (继续): %s", e
        )

    # 2) 超时处理: 不写表 (避免 [超时未确认] 垃圾记录污染主表)。
    #    半成品内容丢失 (用户未在 30 分钟窗口内确认, 可接受); 仅记日志。
    op_desc = (
        payload.get("caozuomiaoshu")
        or payload.get("feedback_zh")
        or payload.get("row_summary")
        or ""
    )
    logger.info(
        "[bugtrack_timeout] 超时不写表 (避免污染主表): user=%s state=%s "
        "record_id=%s op_desc_len=%d",
        user_id, state, record_id or "(none)", len(op_desc),
    )
    return {
        "status": "skipped_no_write",
        "main_record_id": record_id,
        "state": state,
    }


__all__ = ["bugtrack_timeout"]
