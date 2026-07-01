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
from abc import ABC, abstractmethod
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)


def _key(user_id: str, scope: str) -> Tuple[str, str]:
    """构造映射键。scope 通常用 open_kfid (KF) 或 "bot" (智能机器人)。"""
    return (user_id or "anon", scope or "default")


class ConversationStore(ABC):
    """conversation_id 映射存储接口。"""

    @abstractmethod
    async def get(self, user_id: str, scope: str) -> Optional[str]:
        """取已保存的 conversation_id, 首次为 None (Dify 会新建会话)。"""
        ...

    @abstractmethod
    async def save(self, user_id: str, scope: str, conversation_id: str) -> None:
        """保存 Dify 返回的新 conversation_id, 供下一轮续接。"""
        ...


class InMemoryConversationStore(ConversationStore):
    """进程内映射 (默认, 单 worker)。"""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._map: Dict[Tuple[str, str], str] = {}

    async def get(self, user_id: str, scope: str) -> Optional[str]:
        async with self._lock:
            return self._map.get(_key(user_id, scope))

    async def save(self, user_id: str, scope: str, conversation_id: str) -> None:
        if not conversation_id:
            return
        async with self._lock:
            self._map[_key(user_id, scope)] = conversation_id


class RedisConversationStore(ConversationStore):
    """Redis 映射 (多 worker 抗重启)。

    key: wecom:conv:{user_id}:{scope}, 不设 TTL (会话长期续接)。
    需 ``settings.redis`` 已配置且 Redis 可达。
    """

    _KEY_PREFIX = "wecom:conv:"

    def __init__(self, redis_client) -> None:  # type: ignore[no-untyped-def]
        self._redis = redis_client

    async def get(self, user_id: str, scope: str) -> Optional[str]:
        import redis.asyncio as aioredis  # noqa: F401

        val = await self._redis.get(f"{self._KEY_PREFIX}{user_id}:{scope}")
        if isinstance(val, bytes):
            return val.decode("utf-8")
        return val

    async def save(self, user_id: str, scope: str, conversation_id: str) -> None:
        if not conversation_id:
            return
        await self._redis.set(
            f"{self._KEY_PREFIX}{user_id}:{scope}", conversation_id
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
            logger.warning(
                "Redis ConversationStore 初始化失败, 回退 InMemory: %s", e
            )
    return InMemoryConversationStore()


__all__ = [
    "ConversationStore",
    "InMemoryConversationStore",
    "RedisConversationStore",
    "create_conversation_store",
]
