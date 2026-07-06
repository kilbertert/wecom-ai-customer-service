"""二阶段超时定时器 Celery 任务 (N18/N19)。

当用户进入待确认态 (AWAIT_*) 时, MessageProcessor 会 arm 一个 30 分钟
(1800s) 倒计时任务 ``bugtrack_timeout``。若用户在此窗口内未再发言,
本任务 fire, 把半成品反馈内容写入**缓存表** (N19), 不污染主表。

写表走 **飞书多维表格** (见 :mod:`feishu_bitable`), 替代企微 MCP/webhook
(企微查表是死路)。缓存表 = 主表同结构 (飞书用字段名中文标题作 key)。

设计要点:
    - 本任务在 Celery worker 进程内执行 (与 FastAPI 分进程), 用同步 httpx。
    - payload 由 MessageProcessor arm 时序列化传入, 含写缓存表所需的半成品
      内容快照 (cv_feedback_zh / cv_row_summary 等) + state + record_id。
    - fire 时先从 PendingTimerStore (Redis) 读元数据确认仍 pending (防重复 fire /
      用户其实已在新窗口内回应但 task 未及时 revoke)。若已清除, 跳过。
    - 写缓存表失败仅记日志 + 重试 (max_retries=2), 不抛 (超时写入是兜底,
      失败不该阻塞 worker)。
    - ⚠️ 飞书同一表不支持并发写, 但 Celery worker concurrency=1 天然串行。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from app.core.celery_app import celery_app
from app.services.feishu_bitable import FeishuBitableError, add_record as _feishu_add_record

logger = logging.getLogger(__name__)

# 飞书写表用字段名 (中文标题作 key)。缓存表 = 主表同结构 (按 Excel schema)
_F_MODULE = "模块/功能点"
_F_OP_DESC = "操作描述"
_F_TYPE = "类型"          # 单选: bug/优化
_F_STATUS = "问题状态"    # 单选
_F_REMARK = "产品备注"    # 文本, 承载沟通摘要/超时标注
_F_REL_MAIN = "关联主表record_id"


def _build_cache_values(payload: Dict[str, Any], state: str) -> Dict[str, Any]:
    """根据 payload 半成品内容构造飞书写入 fields (字段名 keyed)。

    飞书单选字段值 = 选项名字符串 (传新值自动建选项), 与企微 MCP 的 [{"text":..}] 不同。

    payload 字段 (由 Dify chatflow 6244-timer 拼 TIMER 时填入, 4 字段来自 6250-parse):
        - mokuai: 模块/功能点
        - caozuomiaoshu: 操作描述
        - huanjing: 环境 (后台/管家端/用户端)
        - leixing: 类型 (bug/优化)
        - feedback_zh: 兼容旧字段 (cv_feedback_zh)
    """
    fields: Dict[str, Any] = {}

    mokuai = payload.get("mokuai") or payload.get("module") or ""
    if mokuai:
        fields[_F_MODULE] = mokuai

    op_desc = payload.get("caozuomiaoshu") or payload.get("feedback_zh") or payload.get("row_summary") or ""
    if op_desc:
        fields[_F_OP_DESC] = op_desc

    # 类型: 优先 leixing, 默认 bug
    leixing = payload.get("leixing") or "bug"
    fields[_F_TYPE] = leixing

    # 环境: 优先 huanjing (单选字段), 写 caozuomiaoshu 已覆盖
    # (环境字段 fIE9oJ 在 Excel schema 中, 这里暂不写入, 保持简化的 bug 反馈缓存)

    fields[_F_STATUS] = "问题未查阅"

    # 产品备注: 标注超时状态 + 原内容
    summary_parts = [f"[超时未确认 state={state}]"]
    if op_desc:
        summary_parts.append(op_desc[:500])
    fields[_F_REMARK] = "\n".join(summary_parts)

    # 关联主表 record_id
    main_rid = payload.get("record_id") or ""
    if main_rid:
        fields[_F_REL_MAIN] = main_rid

    return fields


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
    """30 分钟倒计时 fire: 把未完成的反馈写入缓存表 (N19)。

    Args:
        user_id: 用户标识 (WeChat external_userid)
        scope: 会话 scope (open_kfid / "bot") — 用于查 PendingTimerStore
        state: arm 时的 cv_flow_state (await_confirm_*)
        record_id: 主表 record_id (新增超时为空串)
        payload: 写缓存表所需的半成品内容快照
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
                "(用户已在窗口内回应), 跳过缓存表写入", user_id,
            )
            return {"status": "skipped", "reason": "no_pending"}
        async def _clear_pending():
            store = create_pending_timer_store()
            return await store.clear(user_id, scope)
        if not (loop and loop.is_running()):
            asyncio.run(_clear_pending())
    except Exception as e:
        logger.warning(
            "[bugtrack_timeout] pending 检查异常 (继续写表): %s", e
        )

    # 2) 写缓存表 (N19) via 飞书多维表格
    try:
        fields = _build_cache_values(payload, state)
        cache_record_id = _feishu_add_record(fields)
        logger.info(
            "[bugtrack_timeout] 缓存表写入成功: user=%s cache_record_id=%s "
            "关联主表=%s", user_id, cache_record_id, record_id or "(新增超时)",
        )
        return {
            "status": "cached",
            "cache_record_id": cache_record_id,
            "main_record_id": record_id,
            "state": state,
        }
    except FeishuBitableError as e:
        logger.error(
            "[bugtrack_timeout] 缓存表写入失败 (将重试): user=%s, %s",
            user_id, e, exc_info=True,
        )
        raise self.retry(exc=e)
    except Exception as e:
        logger.error(
            "[bugtrack_timeout] 缓存表写入意外异常: user=%s, %s",
            user_id, e, exc_info=True,
        )
        return {"status": "failed", "error": str(e)}


__all__ = ["bugtrack_timeout"]

