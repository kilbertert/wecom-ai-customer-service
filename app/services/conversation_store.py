"""薄 conversation_id 映射存储 (非 SessionService)。

**重要**: 这不是会话历史存储, 不存任何消息内容。它只维护一个字符串映射::

    (user_id, scope) -> dify_conversation_id

让 Dify chatflow (`/v1/chat-messages`) 能跨多轮续接上下文。会话记忆本身由 Dify
chatflow 在其侧持有, 人工侧记忆由 Chatwoot 持有 —— 本服务保持无状态。

与 CLAUDE.md 约定的"不引入 Redis-backed 历史会话存储"不冲突: 这里只存一个 id,
不存历史。多 worker 部署用 RedisConversationStore; 单 worker 用 InMemory。
"""

from __future__ import annotations

import asyncio
import logging
import time
from abc import ABC, abstractmethod
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)


def _key(user_id: str, scope: str) -> Tuple[str, str]:
    """构造映射键。scope 通常用 open_kfid (KF) 或 "bot" (智能机器人)。"""
    return (user_id or "anon", scope or "default")


class ConversationStore(ABC):
    """conversation_id 映射存储接口。"""

    @abstractmethod
    async def get_state(self, user_id: str, scope: str) -> Dict[str, Any]:
        """取兼容状态；M4 运行态固定 active=A 且 conv_b 为空。"""
        ...

    @abstractmethod
    async def save_state(self, user_id: str, scope: str, state: Dict[str, Any]) -> None:
        """保存状态。"""
        ...

    # —— 向后兼容: 旧 get/save 只读写 A 的 conv_id ——
    async def get(self, user_id: str, scope: str) -> Optional[str]:
        st = await self.get_state(user_id, scope)
        return st.get("conv_a") or None

    async def save(self, user_id: str, scope: str, conversation_id: str) -> None:
        if not conversation_id:
            return
        st = await self.get_state(user_id, scope)
        st["conv_a"] = conversation_id
        await self.save_state(user_id, scope, st)

    @staticmethod
    def normalize_state(state: Dict[str, Any]) -> Dict[str, Any]:
        """M4 runtime shape; retain only A conversation fields.

        ``active``/``conv_b`` remain in the serialized schema for old callers
        and database rows, but historical B values must never become live state.
        """
        normalized = dict(state or {})
        normalized["active"] = "A"
        normalized["conv_a"] = str(normalized.get("conv_a") or "")
        normalized["conv_b"] = ""
        normalized["bug_v2_active"] = bool(normalized.get("bug_v2_active"))
        return normalized


class InMemoryConversationStore(ConversationStore):
    """进程内映射 (默认, 单 worker)。

    与 RedisConversationStore 行为对齐: 带 ``conversation_ttl`` 滑动 TTL
    (默认 1800s, 与 bugtrack 定时器对齐)。save_state 每条消息刷新过期时间;
    30min 不活动 -> get_state 返回 default (新会话), 防 conv 永不过期致跨话题
    串话/A↔B 弹跳 (修根因5 的 memory 模式补丁)。
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._states: Dict[Tuple[str, str], Dict[str, Any]] = {}
        self._expires: Dict[Tuple[str, str], float] = {}  # key -> 过期时间戳

    @staticmethod
    def _default() -> Dict[str, Any]:
        return {
            "active": "A",
            "conv_a": "",
            "conv_b": "",
            "bug_v2_active": False,
        }

    @staticmethod
    def _ttl() -> float:
        from app.core.config import settings

        return float(getattr(settings.app, "conversation_ttl", 1800) or 1800)

    async def get_state(self, user_id: str, scope: str) -> Dict[str, Any]:
        k = _key(user_id, scope)
        async with self._lock:
            # 过期清理 (滑动 TTL): 30min 不活动 -> 视为新会话
            if k in self._expires and self._expires[k] < time.time():
                self._states.pop(k, None)
                self._expires.pop(k, None)
            st = self._states.get(k)
            return self.normalize_state(st) if st else self._default()

    async def save_state(self, user_id: str, scope: str, state: Dict[str, Any]) -> None:
        k = _key(user_id, scope)
        async with self._lock:
            self._states[k] = self.normalize_state(state)
            self._expires[k] = time.time() + self._ttl()


class RedisConversationStore(ConversationStore):
    """Redis 映射 (多 worker 抗重启)。

    key: wecom:convstate:{user_id}:{scope}, 存 JSON {active,conv_a,conv_b}。
    TTL=conversation_ttl(默认1800s, 与 bugtrack 定时器对齐; save_state 每条消息调一次
    -> 滑动刷新, 活跃会话不过期, 30min 不活动才过期)。修根因5: 防 conv 永不过期致
    跨话题串话/状态残留(A↔B 弹跳)。需 ``settings.redis`` 已配置且 Redis 可达。
    """

    _KEY_PREFIX = "wecom:convstate:"

    def __init__(self, redis_client) -> None:  # type: ignore[no-untyped-def]
        self._redis = redis_client

    @staticmethod
    def _default() -> Dict[str, Any]:
        return {
            "active": "A",
            "conv_a": "",
            "conv_b": "",
            "bug_v2_active": False,
        }

    async def get_state(self, user_id: str, scope: str) -> Dict[str, Any]:
        import json

        val = await self._redis.get(f"{self._KEY_PREFIX}{user_id}:{scope}")
        if isinstance(val, bytes):
            val = val.decode("utf-8")
        if not val:
            return self._default()
        try:
            st = json.loads(val)
            if not isinstance(st, dict):
                return self._default()
            state = self._default()
            state.update(st)
            return self.normalize_state(state)
        except Exception:
            return self._default()

    async def save_state(self, user_id: str, scope: str, state: Dict[str, Any]) -> None:
        import json

        from app.core.config import settings

        ttl = int(getattr(settings.app, "conversation_ttl", 1800) or 1800)
        await self._redis.set(
            f"{self._KEY_PREFIX}{user_id}:{scope}",
            json.dumps(self.normalize_state(state), ensure_ascii=False),
            ex=ttl,
        )


def create_conversation_store() -> ConversationStore:
    """根据 ``settings.app`` 选择默认 conversation store。

    当前默认 InMemory。Redis 实现待 ``APP_CONVERSATION_STORE=redis`` 启用时
    接入 (Phase 3+ 在 MessageProcessor 中使用)。
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
            logger.info("ConversationStore: Redis 模式")
            return RedisConversationStore(client)
        except Exception as e:
            logger.warning("Redis ConversationStore 初始化失败, 回退 InMemory: %s", e)
    return InMemoryConversationStore()


__all__ = [
    "ConversationStore",
    "InMemoryConversationStore",
    "RedisConversationStore",
    "create_conversation_store",
]
