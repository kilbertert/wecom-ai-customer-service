"""协议适配器抽象基类与统一消息模型。

借鉴对标仓库 (chatgpt-yunju/wecom-ai-customer-service) 的 Channel Provider 模式,
把"微信协议"(客服 KF / 智能机器人) 从 route/service 层剥离:

    ProtocolAdapter (ABC)
        ├─ KfAdapter      (微信客服: XML + sync_msg 拉取 + send_kf)
        └─ BotAdapter     (智能机器人: JSON envelope + response_url 推送)

``MessageProcessor`` 只消费 ``InboundMessage`` / ``OutboundReply``, 协议无关,
新增协议只需实现 ``ProtocolAdapter`` 即可, 无需改动编排器。

``DedupStore`` 把原本散落在 ``WeChatService`` 类属性与 route 模块字典里的两套
去重逻辑统一到一个接口, 默认 InMemory(单 worker), 可换 Redis 实现(多 worker)。
"""

from __future__ import annotations

import asyncio
import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Protocol

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class InboundMessage:
    """协议无关的入站消息。

    把 KF (WeChatMessage) 与智能机器人 (decrypted JSON) 两种协议载荷
    归一到一个结构, 供 ``MessageProcessor`` 统一处理。
    """

    protocol: str  # "kf" | "bot"
    msgid: str
    msg_type: str  # text | image | voice | mixed
    text: str = ""
    # 媒体定位符 (协议无关): KF 用 media_id 走 /media/get; bot image 用 CDN url
    media_ref: str = ""
    media_kind: str = ""  # "url" | "media_id" | ""
    user_id: str = ""  # KF: external_userid; bot: from.userid
    open_kfid: str = ""  # KF 专用; bot 为空
    response_url: str = ""  # bot 专用 (异步推送回复); KF 为空
    chat_type: str = "single"  # "single" | "group" (bot 区分; KF 恒 single)
    raw: Dict[str, Any] = field(default_factory=dict)  # 原始解析载荷 (trace/调试)


@dataclass(frozen=True)
class OutboundReply:
    """协议无关的出站回复。"""

    text: str


class ProtocolAdapter(ABC):
    """微信协议适配器抽象基类。

    每个具体协议 (KF / 智能机器人) 实现本接口, 拥有自己的:
        - 凭证 (token / encoding_aes_key)
        - 验签 + 解密 + 入站消息解析 (receive)
        - 回复投递 (send)
        - 同步 ACK (build_sync_ack: KF 返回 "success", bot 返回加密 envelope)
        - 去重 (委托共享 DedupStore)
    """

    @abstractmethod
    async def receive(self, request: Any) -> List[InboundMessage]:
        """解析入站请求, 返回一条或多条 ``InboundMessage``。

        KF: 验签 + 解密 + 拉 sync_msg, 可能返回多条 (取最新)。
        bot: 验签 + 解密 JSON, 返回单条。
        """
        ...

    @abstractmethod
    async def send(self, inbound: InboundMessage, reply: OutboundReply) -> bool:
        """把回复投递回用户。

        KF: 调 send_kf_msg (用 inbound.user_id + open_kfid)。
        bot: POST response_url (markdown, 用 inbound.response_url)。
        """
        ...

    @abstractmethod
    def build_sync_ack(
        self, timestamp: str, nonce: str, text: str = ""
    ) -> str:
        """构造同步响应体 (在后台任务跑完前立即返回给微信的 ACK)。

        KF: 返回 "success"。
        bot: 返回加密 JSON envelope (含占位 markdown)。
        """
        ...

    # ---- 去重 (委托共享 DedupStore) ----
    @property
    @abstractmethod
    def dedup(self) -> "DedupStore":
        ...


class DedupStore(ABC):
    """消息去重存储接口。

    统一 KF 与 bot 两套原本互不相干的去重机制。
    防止微信重试风暴导致同一 msgid 被多次处理 / 多次回复。
    """

    @abstractmethod
    async def acquire(self, msgid: str, ttl: float) -> bool:
        """尝试占有 msgid 的处理权。

        Returns:
            True = 首次占有, 调用方可继续处理;
            False = 已被占有 (在 ttl 内), 调用方应跳过。
        """
        ...

    @abstractmethod
    async def mark_done(self, msgid: str) -> None:
        """标记 msgid 处理完成 (从"处理中"移除)。"""
        ...

    @abstractmethod
    async def mark_sent(self, msgid: str) -> bool:
        """标记 msgid 已发送回复, 防止重复发送。

        Returns:
            True = 首次标记; False = 之前已发送过 (应跳过本次发送)。
        """
        ...

    @abstractmethod
    async def release_processing(self, msgid: str) -> None:
        """处理失败时释放"处理中"标记, 允许重试。"""
        ...


class InMemoryDedupStore(DedupStore):
    """进程内去重存储 (默认, 单 worker 够用)。

    与历史 ``WeChatService._processed_messages`` / route 模块 ``_bot_processed_msgs``
    行为一致: 进程级, 不抗重启 / 多 worker。多 worker 部署需换 Redis 实现。
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        # msgid -> 处理完成时间戳 (用于 ttl 判断)
        self._processed: Dict[str, float] = {}
        # 正在处理中的 msgid 集合
        self._processing: set[str] = set()
        # 已发送回复的 msgid 集合
        self._sent: set[str] = set()

    async def acquire(self, msgid: str, ttl: float) -> bool:
        if not msgid:
            return True
        now = time.time()
        async with self._lock:
            # 清理过期记录
            expired = [k for k, t in self._processed.items() if now - t > ttl]
            for k in expired:
                self._processed.pop(k, None)
                self._sent.discard(k)

            if msgid in self._processing:
                return False
            if msgid in self._processed and (now - self._processed[msgid]) < ttl:
                return False
            self._processing.add(msgid)
            return True

    async def mark_done(self, msgid: str) -> None:
        if not msgid:
            return
        async with self._lock:
            self._processed[msgid] = time.time()
            self._processing.discard(msgid)

    async def mark_sent(self, msgid: str) -> bool:
        if not msgid:
            return True
        async with self._lock:
            if msgid in self._sent:
                return False
            self._sent.add(msgid)
            return True

    async def release_processing(self, msgid: str) -> None:
        if not msgid:
            return
        async with self._lock:
            self._processing.discard(msgid)


__all__ = [
    "InboundMessage",
    "OutboundReply",
    "ProtocolAdapter",
    "DedupStore",
    "InMemoryDedupStore",
]
