"""去重存储工厂 + Redis 实现。

``DedupStore`` ABC 与 ``InMemoryDedupStore`` 见 :mod:`app.protocols.base`。本模块补
Redis 实现, 供多进程 / 持久队列 (#15) 崩溃重投递场景使用:

- **多进程**: 多个 FastAPI worker 共享同一 Redis -> 同一 msgid 不会被两个进程各处理一次。
- **崩溃安全**: 进程崩溃后, RedisMessageQueue 的 orphan sweep 会把 in-flight 消息重入队
  重投递。若去重是 InMemory, 崩溃后状态丢失 -> 重投递的 msgid 重新 acquire 成功 ->
  Dify chatflow 重复一轮 (污染上下文)。RedisDedupStore 的 ``_processed`` 跨进程/抗重启,
  使重投递幂等 (acquire 返回 False, 跳过)。

状态机与 InMemory 完全一致 (见 ``DedupStore`` 文档), 区别仅是 TTL 清理由 Redis ``EX``
自动完成, 无需手动 sweep。
"""

from __future__ import annotations

import logging

from app.protocols.base import DedupStore, InMemoryDedupStore

logger = logging.getLogger(__name__)


class RedisDedupStore(DedupStore):
    """Redis 去重存储 (多进程 / 崩溃安全幂等)。

    key:
        - ``wecom:dedup:proc:{msgid}`` - 处理中 (NX 占有, EX=ttl 防泄漏)
        - ``wecom:dedup:done:{msgid}`` - 已成功发送 (EX=ttl, ttl 内防重发)
        - ``wecom:dedup:sent:{msgid}`` - 已发送回复 (mark_sent, 与 done 同生命周期)

    TTL 由 Redis ``EX`` 自动过期, 无需手动清理 (对比 InMemory 的手动 sweep)。
    """

    _PROC = "wecom:dedup:proc:"
    _DONE = "wecom:dedup:done:"
    _SENT = "wecom:dedup:sent:"

    # done/sent 的 EX (秒): 防重发窗口。微信重试窗口 ~5s, 我们 ACK 在先不会重发; done
    # 主要防队列崩溃重投递 (orphan sweep 在重启时发生, 通常 < 数分钟)。600s 覆盖绝大多数
    # 重启窗口; 极端长重启后 done 过期 -> 重投递重处理 (罕见, 可接受)。
    _DONE_TTL = 600

    # acquire: 原子地 (检查 done + 占有 proc)。返回 1=首次占有, 0=已完成或已占有。
    _ACQUIRE_LUA = """
if redis.call('exists', KEYS[1]) == 1 then return 0 end
if redis.call('set', KEYS[2], '1', 'NX', 'EX', ARGV[1]) then return 1 else return 0 end
"""

    # mark_done: 置 done (EX=ttl) + 清 proc。原子。
    _MARK_DONE_LUA = """
redis.call('set', KEYS[1], '1', 'EX', ARGV[1])
redis.call('del', KEYS[2])
return 1
"""

    def __init__(self, redis_client) -> None:  # type: ignore[no-untyped-def]
        self._r = redis_client

    async def acquire(self, msgid: str, ttl: float) -> bool:
        if not msgid:
            return True
        ttl_s = str(max(1, int(ttl)))
        try:
            res = await self._r.eval(
                self._ACQUIRE_LUA, 2,
                f"{self._DONE}{msgid}", f"{self._PROC}{msgid}", ttl_s,
            )
            return bool(res)
        except Exception as e:
            # Redis 异常: fail-open (允许处理) -- 与 InMemory 行为对齐 (InMemory 不会因
            # 存储故障拒绝)。风险: 去重失效可能重复处理, 但优于阻塞所有消息。
            logger.warning(
                "[DedupRedis] acquire 异常, fail-open (允许处理): msgid=%s %s",
                msgid, e,
            )
            return True

    async def mark_done(self, msgid: str) -> None:
        if not msgid:
            return
        ttl_s = str(self._DONE_TTL)
        try:
            await self._r.eval(
                self._MARK_DONE_LUA, 2,
                f"{self._DONE}{msgid}", f"{self._PROC}{msgid}", ttl_s,
            )
        except Exception as e:
            logger.warning("[DedupRedis] mark_done 异常: msgid=%s %s", msgid, e)

    async def mark_sent(self, msgid: str) -> bool:
        # mark_sent 当前为死代码 (process() 已改为 send 成功后直接 mark_done, 不再
        # pre-send mark_sent)。实现保留以满足 ABC 契约 + 向后兼容。
        if not msgid:
            return True
        try:
            res = await self._r.set(
                f"{self._SENT}{msgid}", "1", nx=True, ex=self._DONE_TTL,
            )
            return bool(res)
        except Exception as e:
            logger.warning("[DedupRedis] mark_sent 异常, fail-open: msgid=%s %s", msgid, e)
            return True

    async def release_processing(self, msgid: str) -> bool:
        if not msgid:
            return True
        try:
            await self._r.delete(f"{self._PROC}{msgid}")
            return True
        except Exception as e:
            logger.warning("[DedupRedis] release_processing 异常: msgid=%s %s", msgid, e)
            return False


def create_dedup_store() -> DedupStore:
    """根据 ``settings.app.dedup_store`` 选择去重存储。

    "memory" (默认) -> InMemoryDedupStore (单进程)。
    "redis"  -> RedisDedupStore (多进程 / 崩溃安全)。Redis 不可达时回退 InMemory 并告警。
    """
    from app.core.config import settings

    mode = (getattr(settings.app, "dedup_store", "memory") or "memory").lower()
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
            logger.info("DedupStore: Redis 模式")
            return RedisDedupStore(client)
        except Exception as e:
            logger.warning("Redis DedupStore 初始化失败, 回退 InMemory: %s", e)
    return InMemoryDedupStore()


__all__ = ["RedisDedupStore", "create_dedup_store"]
