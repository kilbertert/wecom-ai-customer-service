"""持久消息队列 + 分布式锁 (#15 + #17B 耦合对)。

把路由层的 ``BackgroundTasks`` / ``asyncio.create_task`` 内存派发换成 Redis list 持久队列,
使消息在进程重启/崩溃后不丢失; 并用每 (user, scope) 的 Redis 分布式锁串行化
``MessageProcessor.process``, 消除同用户并发消息的 read->Dify->save 竞态。

设计要点
--------
- **队列 (Redis list, FIFO)**: ``LPUSH`` 入队 (head), ``BRPOPLPUSH`` 出队 (tail -> proc)。
  ``wecom:msgq`` (主) / ``wecom:msgq:proc`` (处理中) / ``wecom:msgq:dead`` (死信)。
- **崩溃恢复 (无周期 sweeper)**: 启动 + 优雅关闭时把 ``proc`` 列表里的 in-flight 全部
  回灌 ``main`` (至少一次投递)。worker 崩溃 -> item 留 proc -> 下次启动重入队重投递。
  幂等由 ``DedupStore`` 保证 (redis 模式用 ``RedisDedupStore`` 跨进程抗重启)。
- **锁 (#17B, 与队列共享同一 Redis client)**: ``SET lock NX EX ttl`` 占有, Lua(token 比对)
  释放。**锁被占** -> 消息回 main 队尾 (不算失败, 不增 attempts) + 短暂 sleep 防忙轮询;
  **Redis 异常** -> fail-open 直处理 (不阻塞); TTL > 最坏 4 轮 Dify 耗时, worker 崩溃靠
  TTL 自动释放。
- **死信**: 反序列化/未知 adapter/InboundMessage 重建失败, 或 process 真异常重试耗尽。
- **CancelledError (shutdown 取消)**: 不计数, 留 proc 待 orphan sweep 重入队 (不重跑
  Dify 除非真崩)。

序列化: ``InboundMessage`` 经 ``dataclasses.asdict`` (全字符串 + raw dict, JSON 安全)。
媒体在 ``process()`` 内下载 (非 ``receive()``), 故入队的是预下载 InboundMessage, worker
按 ``env["adapter"]`` 从注册表取 adapter 后调 ``process(inbound, adapter)``。

⚠️ send 重试: 不在 send 失败时重跑整个 process (会令 Dify chatflow 重复一轮污染上下文)。
仅做崩溃恢复重投递。瞬态 send_kf/response_url 失败仍按 process 内既有逻辑 (release 去重
+ 记日志)。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import asdict
from typing import Any, Dict, Optional, Tuple
from uuid import uuid4

from app.protocols.base import InboundMessage

logger = logging.getLogger(__name__)


class RedisMessageQueue:
    """Redis 持久消息队列 + 每 (user,scope) 分布式锁。"""

    Q_MAIN = "wecom:msgq"
    Q_PROC = "wecom:msgq:proc"
    Q_DEAD = "wecom:msgq:dead"

    # 锁释放: 仅当持有 token 一致才 del (防误删他人锁)。
    _RELEASE_LUA = """
if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) else return 0 end
"""

    def __init__(
        self,
        redis_client: Any,
        adapters: Dict[str, Any],
        processor: Any,
    ) -> None:
        from app.core.config import settings

        self._r = redis_client
        self._adapters = adapters  # {"kf": KfAdapter, "bot": BotAdapter}
        self._processor = processor
        self._n_workers = max(1, int(getattr(settings.app, "queue_workers", 2) or 2))
        self._lock_ttl = int(getattr(settings.app, "queue_lock_ttl", 300) or 300)
        self._max_attempts = int(getattr(settings.app, "queue_max_attempts", 3) or 3)
        self._stop = asyncio.Event()
        self._tasks: list[asyncio.Task] = []

    # ------------------------------------------------------------------
    # 入队 (路由层调用)
    # ------------------------------------------------------------------
    async def enqueue(self, inbound: InboundMessage, adapter_id: str) -> None:
        """序列化 InboundMessage 入主队列 (head)。路由层 ACK 后立即返回。"""
        env = {
            "id": uuid4().hex,
            "adapter": adapter_id,
            "payload": asdict(inbound),
            "attempts": 0,
            "enqueued_at": time.time(),
        }
        raw = json.dumps(env, ensure_ascii=False)
        try:
            await self._r.lpush(self.Q_MAIN, raw)
            logger.info(
                "[QUEUE] 入队 adapter=%s msgid=%s (qlen 将由 worker 消费)",
                adapter_id, inbound.msgid,
            )
        except Exception as e:
            # 入队失败: 消息无法持久化。路由层已 ACK 给微信 -> 此消息将丢失。记 ERROR。
            # (比静默丢好: 可观测。生产应配告警。)
            logger.error(
                "[QUEUE] 入队失败, 消息将丢失 (微信已 ACK 不会重发): "
                "adapter=%s msgid=%s %s", adapter_id, inbound.msgid, e,
            )

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    async def start(self) -> None:
        await self._requeue_orphans()
        self._stop.clear()
        for i in range(self._n_workers):
            self._tasks.append(
                asyncio.create_task(self._loop(i), name=f"msgq-w{i}")
            )
        logger.info(
            "[QUEUE] 启动 %d worker (lock_ttl=%ss max_attempts=%s)",
            self._n_workers, self._lock_ttl, self._max_attempts,
        )

    async def stop(self) -> None:
        logger.info("[QUEUE] 停止中: 取消 worker, in-flight 回灌 main ...")
        self._stop.set()
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        self._tasks = []
        # 被 cancel 的 in-flight 已留 proc (见 _handle), 这里统一回灌 main 供下次启动重处理。
        await self._requeue_orphans()
        # 关闭队列独占的 Redis 连接 (conv/timer/dedup store 各自的连接由各自管理)
        try:
            aclose = getattr(self._r, "aclose", None)
            if aclose is not None:
                await aclose()
        except Exception as e:
            logger.warning("[QUEUE] 关闭 Redis 连接失败: %s", e)
        logger.info("[QUEUE] 已停止")

    async def _requeue_orphans(self) -> None:
        """把 proc 列表里的 in-flight 全部回灌 main (启动/关闭时调用)。

        启动: 上次崩溃留下的 proc -> 重投递。关闭: 被 cancel 的 in-flight -> 下次启动重处理。
        不增 attempts (崩溃/取消不算失败)。
        """
        try:
            items = await self._r.lrange(self.Q_PROC, 0, -1)
            if not items:
                return
            # 逐条 LPUSH 回 main (保持顺序不必严格, FIFO 近似即可)
            for it in items:
                await self._r.lpush(self.Q_MAIN, it)
            await self._r.delete(self.Q_PROC)
            logger.info("[QUEUE] 回灌 %d 条 in-flight -> main (orphan sweep)", len(items))
        except Exception as e:
            logger.warning("[QUEUE] orphan sweep 失败 (proc 残留待下次): %s", e)

    # ------------------------------------------------------------------
    # worker 循环
    # ------------------------------------------------------------------
    async def _loop(self, wid: int) -> None:
        while not self._stop.is_set():
            try:
                # blmove = BRPOPLPUSH 的后继 (后者自 Redis 6.2 / redis-py 5 起废弃):
                # 从 main 弹 tail (RIGHT), 推入 proc head (LEFT)。timeout=5 便于周期
                # 检查 _stop 优雅退出。
                raw = await self._r.blmove(
                    self.Q_MAIN, self.Q_PROC, "RIGHT", "LEFT", timeout=5
                )
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("[QUEUE w%s] brpoplpush 失败, 2s 后重试: %s", wid, e)
                await asyncio.sleep(2)
                continue
            if raw is None:
                continue  # 5s 超时, 回头检查 _stop
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")
            await self._handle(raw, wid)

    async def _handle(self, raw: str, wid: int) -> None:
        # 1) 反序列化
        try:
            env = json.loads(raw)
        except Exception as e:
            logger.error("[QUEUE w%s] 反序列化失败, 入死信: %s", wid, e)
            await self._move_to(self.Q_DEAD, raw)
            return

        adapter = self._adapters.get(env.get("adapter"))
        if adapter is None:
            logger.error("[QUEUE w%s] 未知 adapter=%s, 入死信", wid, env.get("adapter"))
            await self._move_to(self.Q_DEAD, raw)
            return
        try:
            inbound = InboundMessage(**env["payload"])
        except Exception as e:
            logger.error("[QUEUE w%s] InboundMessage 重建失败, 入死信: %s", wid, e)
            await self._move_to(self.Q_DEAD, raw)
            return

        # 2) 加锁 + 跑 process
        status, err = await self._run_with_lock(inbound, adapter, wid)

        # 3) 簿记
        if status == "requeued":
            # 锁被占: 回 main 队尾 (先入队再移 proc, 入队失败则留 proc 待 sweep)
            await asyncio.sleep(0.5)  # 防短队列忙轮询
            await self._move_to(self.Q_MAIN, raw)
            return

        if isinstance(err, asyncio.CancelledError):
            # shutdown 取消: 留 proc, stop() 的 orphan sweep 会回灌 main (不增 attempts)
            logger.info(
                "[QUEUE w%s] cancel, 留 proc 待 sweep: msgid=%s", wid, inbound.msgid
            )
            return

        if err is not None:
            # 真异常 (非 cancel): retry / dead
            env["attempts"] = env.get("attempts", 0) + 1
            new_raw = json.dumps(env, ensure_ascii=False)
            if env["attempts"] < self._max_attempts:
                logger.warning(
                    "[QUEUE w%s] 重试 %s/%s msgid=%s: %s",
                    wid, env["attempts"], self._max_attempts, inbound.msgid, err,
                )
                await self._move_to(self.Q_MAIN, new_raw)
            else:
                logger.error(
                    "[QUEUE w%s] 死信 (重试耗尽) msgid=%s: %s",
                    wid, inbound.msgid, err,
                )
                await self._move_to(self.Q_DEAD, new_raw)
            return

        # success: 移出 proc (仅 LREM; 失败则留 proc -> sweep 重投递, dedup 幂等)
        try:
            await self._r.lrem(self.Q_PROC, 1, raw)
        except Exception as e:
            logger.warning(
                "[QUEUE w%s] success LREM 失败, 留 proc 待 sweep (dedup 幂等): %s",
                wid, e,
            )

    async def _move_to(self, dest: str, raw: str) -> None:
        """把当前 proc 里的 raw 原子地移到 dest (先 LPUSH dest 再 LREM proc)。

        先入 dest 再删 proc: 若 LPUSH 失败, raw 仍在 proc -> 下次 sweep 回灌 (不丢)。
        若 LREM 失败, raw 同时在 proc+dest -> 重复投递, 由 dedup 幂等兜底。
        """
        try:
            await self._r.lpush(dest, raw)
            await self._r.lrem(self.Q_PROC, 1, raw)
        except Exception as e:
            logger.warning("[QUEUE] _move_to(%s) 失败, 留 proc 待 sweep: %s", dest, e)

    # ------------------------------------------------------------------
    # 分布式锁 + process
    # ------------------------------------------------------------------
    async def _run_with_lock(
        self, inbound: InboundMessage, adapter: Any, wid: int
    ) -> Tuple[str, Optional[BaseException]]:
        """加 (user,scope) 锁后跑 process。返回 (status, err)。

        status: "done" (跑完, 成功或异常) | "requeued" (锁被占, 调用方应回队尾)。
        err: process 抛出的异常 (CancelledError=shutdown; 其他=真异常) 或 None。
        """
        user_id = inbound.user_id or "anon"
        scope = "bot" if inbound.protocol == "bot" else (inbound.open_kfid or "kf")
        lock_key = f"wecom:lock:{user_id}:{scope}"
        token = uuid4().hex

        try:
            acquired = await self._r.set(lock_key, token, nx=True, ex=self._lock_ttl)
        except Exception as e:
            logger.warning(
                "[QUEUE w%s] 锁 Redis 不可用, fail-open 直处理: %s", wid, e
            )
            acquired = True
            token = None  # 标记无需释放

        if not acquired:
            return ("requeued", None)

        try:
            await self._processor.process(inbound, adapter)
            return ("done", None)
        except BaseException as e:  # CancelledError (shutdown) 或真异常
            return ("done", e)
        finally:
            if token is not None:
                try:
                    await self._r.eval(self._RELEASE_LUA, 1, lock_key, token)
                except Exception as e:
                    logger.warning(
                        "[QUEUE w%s] 释放锁失败 (靠 TTL=%ss 过期): %s",
                        wid, self._lock_ttl, e,
                    )


def create_message_queue(
    adapters: Dict[str, Any], processor: Any
) -> Optional[RedisMessageQueue]:
    """根据 ``settings.app.message_queue`` 选择队列。

    "memory" (默认) / Redis 不可达 -> 返回 None (路由层回退 BackgroundTasks/create_task)。
    "redis" -> RedisMessageQueue。
    """
    from app.core.config import settings

    mode = (getattr(settings.app, "message_queue", "memory") or "memory").lower()
    if mode != "redis":
        return None
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
        logger.info(
            "MessageQueue: Redis 持久队列模式 (workers=%s)",
            getattr(settings.app, "queue_workers", 2),
        )
        return RedisMessageQueue(client, adapters, processor)
    except Exception as e:
        logger.warning(
            "Redis MessageQueue 初始化失败, 回退 memory (BackgroundTasks): %s", e
        )
        return None


__all__ = ["RedisMessageQueue", "create_message_queue"]
