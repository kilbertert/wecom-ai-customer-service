"""二阶段超时定时器协调器 (Dify ↔ Celery 桥接)。

职责:
    1. 从 Dify chatflow 返回的 answer 文本末尾解析 TIMER 握手标记
       (``<!--SYS:TIMER|...-->``), 剥离后供 MessageProcessor 发给用户。
    2. 按 action (arm/cancel) 调度 Celery ``bugtrack_timeout`` 任务:
       - arm: apply_async(countdown=1800) + 写 PendingTimerStore
       - cancel: revoke 旧 task_id + 清 PendingTimerStore
    3. 入站时 (用户又说话了): 主动 cancel 该 user 旧的 pending timer (N17 同步路径)

握手标记格式 (Dify 结束节点 code 追加到 answer 末尾)::

    <!--SYS:TIMER|action=arm|state=await_confirm_new|record_id=RECxxx|payload_zh=...-->
    <!--SYS:TIMER|action=cancel-->

payload 较大时不放标记 (URL/长度风险), 而是放关键字段 (state/record_id) +
精简的 feedback_zh/row_summary 摘要; 完整 payload 由 arm 时拼装。

与 CLAUDE.md 无状态约束: 本模块只协调"待办定时器元数据" (PendingTimerStore),
不触碰会话历史。
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any, Dict, Optional, Tuple

from app.core.config import settings
from app.services.pending_timer_store import (
    PendingTimer,
    PendingTimerStore,
    _TIMER_COUNTDOWN_SEC,
)

logger = logging.getLogger(__name__)

# 匹配 answer 末尾的 TIMER 标记 (可能有多个, 取最后一个有效的)
_TIMER_MARKER_RE = re.compile(
    r"<!--SYS:TIMER\|([^>]*?)-->", re.DOTALL
)


def parse_timer_markers(text: str) -> Tuple[str, list]:
    """从文本中提取所有 TIMER 标记, 返回 (剥离后的文本, [marker_dict, ...])。

    每个 marker_dict 是解析后的 {action, state, record_id, feedback_zh, ...}。
    剥离后用户看到的文本不含任何标记。
    """
    if not text:
        return text or "", []
    markers = []
    for m in _TIMER_MARKER_RE.finditer(text):
        body = m.group(1)
        kv: Dict[str, str] = {}
        for part in body.split("|"):
            if "=" in part:
                k, _, v = part.partition("=")
                kv[k.strip()] = v.strip()
        if kv:
            markers.append(kv)
    stripped = _TIMER_MARKER_RE.sub("", text).rstrip()
    return stripped, markers


def _build_payload(marker: Dict[str, str]) -> Dict[str, Any]:
    """从标记字段构造写缓存表所需的 payload。"""
    return {
        "feedback_zh": marker.get("feedback_zh", ""),
        "row_summary": marker.get("row_summary", ""),
        "module": marker.get("module", ""),
        "record_id": marker.get("record_id", ""),
    }


async def cancel_pending_timer(
    store: PendingTimerStore, user_id: str, scope: str
) -> None:
    """入站时调用: 该 user 若有 pending timer, revoke + 清除。

    (N17 同步路径: 用户在 30 分钟窗口内又说话了, 旧的倒计时作废,
    由 Dify chatflow 靠 cv_ 状态走相关性分发。)
    """
    try:
        pending = await store.get(user_id, scope)
        if pending is None:
            return
        # revoke Celery task
        try:
            from app.tasks.bugtrack_tasks import bugtrack_timeout
            bugtrack_timeout.AsyncResult(pending.task_id).revoke(
                terminate=False
            )
        except Exception as e:
            logger.warning(
                "[TimerCoord] revoke task 失败 (继续清 store): %s", e
            )
        await store.clear(user_id, scope)
        logger.info(
            "[TimerCoord] user=%s scope=%s cancel 旧 pending timer "
            "(state=%s task=%s)", user_id, scope, pending.state,
            pending.task_id[:8],
        )
    except Exception as e:
        logger.warning("[TimerCoord] cancel_pending 异常: %s", e)


async def apply_markers(
    store: PendingTimerStore,
    user_id: str,
    scope: str,
    markers: list,
) -> None:
    """处理 AI answer 里的 TIMER 标记: arm 或 cancel。

    多个标记时, arm 优先 (cancel 通常表示本轮终结, 但若同时有 arm 以 arm 为准)。
    实际上 Dify 一次只应追加一个标记; 这里取最后一个 arm, 或若无 arm 则 cancel。
    """
    if not markers:
        return

    # 找最后一个 arm 标记
    arm_marker = None
    for mk in markers:
        if mk.get("action") == "arm":
            arm_marker = mk
    if arm_marker is not None:
        await _arm_timer(store, user_id, scope, arm_marker)
        return

    # 无 arm, 看是否有 cancel
    if any(mk.get("action") == "cancel" for mk in markers):
        await cancel_pending_timer(store, user_id, scope)


async def _arm_timer(
    store: PendingTimerStore,
    user_id: str,
    scope: str,
    marker: Dict[str, str],
) -> None:
    """arm 一个 30 分钟倒计时: 先 cancel 旧的, 再 apply_async 新的 + 存 store。"""
    state = marker.get("state", "")
    if not state:
        logger.warning("[TimerCoord] arm 标记缺 state, 跳过: %s", marker)
        return

    # 先 cancel 旧 pending (同 user 同时只一个待确认态)
    await cancel_pending_timer(store, user_id, scope)

    payload = _build_payload(marker)
    record_id = marker.get("record_id", "")
    countdown = settings.bugtrack.timeout_seconds or _TIMER_COUNTDOWN_SEC

    try:
        from app.tasks.bugtrack_tasks import bugtrack_timeout
        result = bugtrack_timeout.apply_async(
            args=(user_id, scope, state, record_id, payload),
            countdown=countdown,
            queue="wecom_timers",
        )
        task_id = result.id
    except Exception as e:
        # Celery 不可用时 arm 失败 — 不影响主流程 (Dify 仍正常回复), 仅超时兜底失效
        logger.error(
            "[TimerCoord] arm Celery 任务失败 (超时兜底将失效, 主流程不受影响): "
            "user=%s state=%s, %s", user_id, state, e, exc_info=True,
        )
        return

    timer = PendingTimer(
        task_id=task_id,
        state=state,
        record_id=record_id,
        armed_at=time.time(),
        payload=payload,
    )
    await store.arm(user_id, scope, timer)
    logger.info(
        "[TimerCoord] arm 成功: user=%s scope=%s state=%s record_id=%s "
        "countdown=%ss task=%s",
        user_id, scope, state, record_id or "(none)", countdown,
        task_id[:8],
    )


__all__ = [
    "parse_timer_markers",
    "cancel_pending_timer",
    "apply_markers",
]
