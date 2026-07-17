"""待办定时器元数据存储 (二阶段超时机制, N15/N18/N19)。

**重要 — 与 CLAUDE.md 无状态约束的关系**:
    这不是"会话历史存储"。它存的是一条**待办定时器的元数据**::

        (user_id, scope) -> {task_id, state, record_id, armed_at, payload}

    性质与已允许的 :class:`ConversationStore` 相同 (后者也存 user_id→id 映射):
    只存一个 id + 少量协调字段, 不存消息内容。会话记忆仍由 Dify chatflow 侧持有。

    它存在的唯一目的: 让后端知道"该用户当前是否有一个 30 分钟倒计时在跑",
    以便用户下次发言时 cancel 它 (N17 同步路径), 或 30 分钟无响应时由 Celery
    task fire 写入缓存表 (N19 异步路径)。

设计:
    - ``arm(user_id, scope, task_id, state, record_id, payload)``: 记录新定时器
      (会覆盖该 user 旧的 pending —— 同一用户同时只应有一个待确认态)。
    - ``get(user_id, scope)``: 取当前 pending 定时器元数据 (用于入站时 cancel)。
    - ``clear(user_id, scope)``: 清除 (cancel 成功 / 状态转 IDLE 后)。
    - 元数据带 TTL=1900s (略大于 30 分钟倒计时 1800s), 防止 worker 漏 fire 时
      残留泄漏: 倒计时早就该 fire 了, 元数据也该过期。
"""

from __future__ import annotations

import asyncio
import json
import logging
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

# 倒计时 1800s (30 分钟), 元数据 TTL 略大, 防 worker 漏 fire 残留。
_TIMER_COUNTDOWN_SEC = 1800
_META_TTL_SEC = 1900


@dataclass
class PendingTimer:
    """一个待办定时器的元数据 (非会话历史)。"""

    task_id: str            # Celery AsyncResult.id, 用于 revoke
    state: str              # cv_flow_state 值 (await_confirm_*)
    record_id: str = ""     # 主表 record_id (N19 关联用, 可空)
    armed_at: float = 0.0   # arm 时刻 (unix ts, 仅观测用)
    payload: Dict[str, Any] = field(default_factory=dict)
    # payload: 写缓存表 N19 所需的半成品内容快照 (cv_feedback_zh / cv_row_summary
    # 的拷贝), fire 时由 Celery task 读出写入缓存表。

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def from_json(cls, raw: str) -> "PendingTimer":
        return cls(**json.loads(raw))


def _key(user_id: str, scope: str) -> str:
    """构造映射键。scope 与 ConversationStore 一致: open_kfid (KF) / "bot"。"""
    return f"{user_id or 'anon'}:{scope or 'default'}"


class PendingTimerStore(ABC):
    """待办定时器元数据存储接口 (非会话历史)。"""

    @abstractmethod
    async def arm(
        self,
        user_id: str,
        scope: str,
        timer: PendingTimer,
    ) -> None:
        """记录一个新定时器 (覆盖该 user 旧的 pending)。"""
        ...

    @abstractmethod
    async def get(self, user_id: str, scope: str) -> Optional[PendingTimer]:
        """取当前 pending 定时器 (入站时判断是否要 cancel)。"""
        ...

    @abstractmethod
    async def clear(self, user_id: str, scope: str) -> Optional[PendingTimer]:
        """清除并返回被清除的定时器 (cancel 成功 / 状态终结)。"""
        ...

    @abstractmethod
    async def clear_if_match(
        self, user_id: str, scope: str, expected_task_id: str
    ) -> bool:
        """CAS 清除: 仅当当前 pending 的 ``task_id == expected_task_id`` 才清除。

        原子 compare-and-delete, 避免旧 (被 revoke 的) 任务延迟 fire 时读到匹配的
        旧 task_id、却在 clear 前被新 arm 覆盖 -> 误删新 timer (审查 P1 #5)。
        返回 True=已清除, False=无 pending 或 task_id 不匹配 (调用方应跳过 fire)。
        """
        ...


class InMemoryPendingTimerStore(PendingTimerStore):
    """进程内映射 (默认, 单 worker)。"""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._map: Dict[str, PendingTimer] = {}

    async def arm(
        self, user_id: str, scope: str, timer: PendingTimer
    ) -> None:
        async with self._lock:
            old = self._map.get(_key(user_id, scope))
            if old is not None:
                logger.info(
                    "[TimerStore] user=%s scope=%s 覆盖旧 pending timer "
                    "(old_state=%s old_task=%s) — 同一用户只保留一个待确认态",
                    user_id, scope, old.state, old.task_id[:8],
                )
            self._map[_key(user_id, scope)] = timer

    async def get(self, user_id: str, scope: str) -> Optional[PendingTimer]:
        async with self._lock:
            return self._map.get(_key(user_id, scope))

    async def clear(
        self, user_id: str, scope: str
    ) -> Optional[PendingTimer]:
        async with self._lock:
            return self._map.pop(_key(user_id, scope), None)

    async def clear_if_match(
        self, user_id: str, scope: str, expected_task_id: str
    ) -> bool:
        async with self._lock:
            t = self._map.get(_key(user_id, scope))
            if t is not None and t.task_id == expected_task_id:
                self._map.pop(_key(user_id, scope), None)
                return True
            return False


class RedisPendingTimerStore(PendingTimerStore):
    """Redis 映射 (多 worker 抗重启, 与 Celery worker 共享)。

    key: wecom:timer:{user_id}:{scope}, TTL=_META_TTL_SEC。
    需 ``settings.redis`` 已配置且 Redis 可达。

    **多 worker 必需**: FastAPI 进程 arm/revoke, Celery worker 进程 fire;
    两者必须看到同一份 pending 元数据 → 必须用 Redis 而非 memory。
    """

    _KEY_PREFIX = "wecom:timer:"

    # CAS 清除 (审查 P1 #5): GET -> cjson 解码比对 task_id -> 匹配才 DEL, 单 EVAL 原子。
    # 避免旧 (被 revoke) 任务延迟 fire 误删新 arm 的 timer (get-then-clear 非原子的竞态)。
    _CLEAR_IF_MATCH_LUA = """
local val = redis.call('GET', KEYS[1])
if val == false then return 0 end
local ok, obj = pcall(cjson.decode, val)
if not ok or obj.task_id ~= ARGV[1] then return 0 end
redis.call('DEL', KEYS[1])
return 1
"""

    def __init__(self, redis_client) -> None:  # type: ignore[no-untyped-def]
        self._redis = redis_client

    def _k(self, user_id: str, scope: str) -> str:
        return f"{self._KEY_PREFIX}{user_id}:{scope}"

    async def arm(
        self, user_id: str, scope: str, timer: PendingTimer
    ) -> None:
        await self._redis.set(
            self._k(user_id, scope),
            timer.to_json(),
            ex=_META_TTL_SEC,
        )

    async def get(self, user_id: str, scope: str) -> Optional[PendingTimer]:
        val = await self._redis.get(self._k(user_id, scope))
        if val is None:
            return None
        if isinstance(val, bytes):
            val = val.decode("utf-8")
        try:
            return PendingTimer.from_json(val)
        except Exception as e:
            logger.warning("[TimerStore] 反序列化失败, 视为无 pending: %s", e)
            return None

    async def clear(
        self, user_id: str, scope: str
    ) -> Optional[PendingTimer]:
        k = self._k(user_id, scope)
        val = await self._redis.get(k)
        await self._redis.delete(k)
        if val is None:
            return None
        if isinstance(val, bytes):
            val = val.decode("utf-8")
        try:
            return PendingTimer.from_json(val)
        except Exception as e:
            logger.warning("[TimerStore] clear 反序列化失败: %s", e)
            return None

    async def clear_if_match(
        self, user_id: str, scope: str, expected_task_id: str
    ) -> bool:
        k = self._k(user_id, scope)
        try:
            res = await self._redis.eval(self._CLEAR_IF_MATCH_LUA, 1, k, expected_task_id)
            return bool(res)
        except Exception as e:
            # Redis 异常: fail-open 不清 (保守, 宁可漏清靠 TTL, 不误清新 timer)。
            logger.warning(
                "[TimerStore] clear_if_match 异常 (未清, 靠 TTL): user=%s %s",
                user_id, e,
            )
            return False


def create_pending_timer_store() -> PendingTimerStore:
    """根据 ``settings.app`` 选择 pending timer store。

    与 ConversationStore 同策略: memory (默认单 worker) / redis (多 worker)。
    注意: Celery worker 与 FastAPI 分进程, 二阶段超时机制要求两者共享 →
    生产应启用 redis 模式。
    """
    from app.core.config import settings

    mode = getattr(settings.app, "conversation_store", "memory") or "memory"
    mode = mode.lower()
    if mode == "redis":
        try:
            import redis.asyncio as aioredis

            client = aioredis.Redis(
                host=settings.redis.host,
                port=settings.redis.port,
                db=settings.redis.db,
                password=(
                    settings.redis.password.get_secret_value()
                    if settings.redis.password
                    else None
                ),
            )
            logger.info("PendingTimerStore: Redis 模式")
            return RedisPendingTimerStore(client)
        except Exception as e:
            logger.warning(
                "Redis PendingTimerStore 初始化失败, 回退 InMemory: %s", e
            )
    return InMemoryPendingTimerStore()


__all__ = [
    "PendingTimer",
    "PendingTimerStore",
    "InMemoryPendingTimerStore",
    "RedisPendingTimerStore",
    "create_pending_timer_store",
    "_TIMER_COUNTDOWN_SEC",
]
