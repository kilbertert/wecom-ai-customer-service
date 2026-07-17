"""持久消息队列 + 分布式锁 (#15 + #17B 耦合对)。

把路由层的 ``BackgroundTasks`` / ``asyncio.create_task`` 内存派发换成 Redis list 持久队列,
使消息在进程重启/崩溃后不丢失; 并用每 (user, scope) 的 Redis 分布式锁串行化
``MessageProcessor.process``, 消除同用户并发消息的 read->Dify->save 竞态。

设计要点
--------
- **队列 (Redis list, FIFO)**: ``LPUSH`` 入队 (head), ``BLMOVE`` 出队
  (tail -> 本实例 proc)。``wecom:msgq`` (主) / ``wecom:msgq:proc:{consumer_id}``
  (处理中) / ``wecom:msgq:dead`` (死信)。
- **崩溃恢复 (实例所有权)**: 每个进程使用独立 ``wecom:msgq:proc:{consumer_id}``
  处理中列表，并持续刷新 consumer heartbeat。仅 heartbeat 过期的实例会被其他健康实例
  原子回灌到 ``main``；优雅关闭只回灌本实例列表。这样一个实例启动/停止不会移动其他
  健康实例的 in-flight 消息，也不会清除其 dedup ``_processing`` key。硬崩后恢复时逐条
  ``release_processing`` 清 stale key，让重投递重新 acquire；``done`` key 仍防完成后的
  重复发送。维护循环会周期扫描失联实例，因此多进程中单实例崩溃无需等待全栈重启。
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
    # Q_PROC 保留为旧版全局 processing key，仅用于启动迁移恢复。新实例一律使用
    # ``Q_PROC_PREFIX + consumer_id``，防一个实例的 orphan sweep 误动其他健康实例。
    Q_PROC = "wecom:msgq:proc"
    Q_PROC_PREFIX = "wecom:msgq:proc:"
    CONSUMER_HEARTBEAT_PREFIX = "wecom:msgq:consumer:"
    Q_DEAD = "wecom:msgq:dead"

    _HEARTBEAT_TTL = 60
    _MAINTENANCE_INTERVAL = 15

    # 锁释放: 仅当持有 token 一致才 del (防误删他人锁)。
    _RELEASE_LUA = """
if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) else return 0 end
"""

    # 原子 orphan move: 只移动 Python 已确认清除 dedup processing key 的精确 raw。
    # 不再 DEL 整个 proc，避免 heartbeat 误判后原 owner 恢复并新写入的 delivery 被带走。
    _REQUEUE_LUA = """
local moved = 0
for i=1,#ARGV do
  local removed = redis.call('LREM', KEYS[1], 1, ARGV[i])
  if removed == 1 then
    redis.call('LPUSH', KEYS[2], ARGV[i])
    moved = moved + 1
  end
end
return moved
"""

    # 原子 move: 从 processing 按 source_raw 删除，再把 dest_raw 推到目标队列。
    # retry 会更新 attempts，source_raw 与 dest_raw 不同；旧实现拿 new_raw 做 LREM，
    # 导致旧 delivery 永久残留 proc，后续 orphan sweep 又以 attempts=0 回灌。
    _MOVE_LUA = """
local removed = redis.call('LREM', KEYS[1], 1, ARGV[1])
if removed == 0 then return 0 end
redis.call('LPUSH', KEYS[2], ARGV[2])
return 1
"""

    # BLMOVE 后把该 delivery 在本实例 proc 中的 raw 替换为带 processing_started_at
    # 的 envelope，供观察脚本直接判断在途年龄（观察进程晚启动也不丢起始时间）。
    _CLAIM_LUA = """
local items = redis.call('LRANGE', KEYS[1], 0, -1)
for i=1,#items do
  if items[i] == ARGV[1] then
    redis.call('LSET', KEYS[1], i-1, ARGV[2])
    return 1
  end
end
return 0
"""

    def __init__(
        self,
        redis_client: Any,
        adapters: Dict[str, Any],
        processor: Any,
        consumer_id: Optional[str] = None,
    ) -> None:
        from app.core.config import settings

        self._r = redis_client
        self._adapters = adapters  # {"kf": KfAdapter, "bot": BotAdapter}
        self._processor = processor
        self._n_workers = max(1, int(getattr(settings.app, "queue_workers", 2) or 2))
        self._lock_ttl = int(getattr(settings.app, "queue_lock_ttl", 300) or 300)
        self._max_attempts = int(getattr(settings.app, "queue_max_attempts", 3) or 3)
        self._consumer_id = consumer_id or uuid4().hex
        self._proc_key = f"{self.Q_PROC_PREFIX}{self._consumer_id}"
        # 滚动升级时旧版本进程仍可能在全局 Q_PROC 中处理消息且没有 heartbeat。
        # 新版本不能启动即回灌；至少等待一个完整 lock TTL + heartbeat 宽限，届时旧 delivery
        # 要么完成移除，要么其处理锁也已失效，可安全按 legacy orphan 恢复。
        self._legacy_recovery_after = (
            time.monotonic() + self._lock_ttl + self._HEARTBEAT_TTL
        )
        # 向后兼容既有测试/观测代码里的 ``q.Q_PROC``，实例值指向自己的 processing list；
        # 类属性 ``RedisMessageQueue.Q_PROC`` 仍表示旧版迁移 key。
        self.Q_PROC = self._proc_key
        self._stop = asyncio.Event()
        self._tasks: list[asyncio.Task] = []
        self._maintenance_task: Optional[asyncio.Task] = None
        self._pending_acks: set[str] = set()

    # ------------------------------------------------------------------
    # 入队 (路由层调用)
    # ------------------------------------------------------------------
    async def enqueue(self, inbound: InboundMessage, adapter_id: str) -> bool:
        """序列化 InboundMessage 入主队列 (head)。返回 True=已入队, False=未入队。

        路由层据返回值决定: True -> 已持久化, 可安全 ACK; False -> Redis 不可达或
        序列化失败, 路由应回退内存派发 (BackgroundTasks/create_task), 避免微信已
        ACK 却丢消息 (审查 P1 #2)。
        """
        env = {
            "id": uuid4().hex,
            "adapter": adapter_id,
            "payload": asdict(inbound),
            "attempts": 0,
            "enqueued_at": time.time(),
        }
        try:
            raw = json.dumps(env, ensure_ascii=False)
        except Exception as e:
            # 序列化失败 (审查 P1 #1, 适配器已 model_dump 规避, 此为兜底):
            # 入死信 (可观测, 不静默丢) + 返回 False 让路由回退内存派发。
            logger.error(
                "[QUEUE] 序列化失败, 入死信 + 回退内存派发: "
                "adapter=%s msgid=%s %s", adapter_id, inbound.msgid, e,
            )
            await self._push_dead_unparseable(inbound, adapter_id, f"serialize: {e}")
            return False
        try:
            await self._r.lpush(self.Q_MAIN, raw)
            logger.info(
                "[QUEUE] 入队 adapter=%s msgid=%s (qlen 将由 worker 消费)",
                adapter_id, inbound.msgid,
            )
            return True
        except Exception as e:
            # Redis 不可达: 返回 False, 路由回退内存派发 (审查 P1 #2)。
            logger.error(
                "[QUEUE] 入队失败 (Redis 不可达?), 路由将回退内存派发: "
                "adapter=%s msgid=%s %s", adapter_id, inbound.msgid, e,
            )
            return False

    async def _push_dead_unparseable(
        self, inbound: InboundMessage, adapter_id: str, reason: str
    ) -> None:
        """把不可序列化的消息以最小可序列化占位写入死信 (供排查, 不重试)。"""
        dead_env = {
            "id": uuid4().hex,
            "adapter": adapter_id,
            "payload": {
                "msgid": inbound.msgid,
                "protocol": inbound.protocol,
                "user_id": inbound.user_id,
                "msg_type": inbound.msg_type,
            },
            "attempts": self._max_attempts,
            "enqueued_at": time.time(),
            "_dead_reason": reason,
        }
        try:
            await self._r.lpush(
                self.Q_DEAD, json.dumps(dead_env, ensure_ascii=False)
            )
        except Exception as de:
            logger.error(
                "[QUEUE] 死信写入也失败 (Redis 不可达, 彻底丢): msgid=%s %s",
                inbound.msgid, de,
            )

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    async def start(self) -> None:
        self._stop.clear()
        # 先声明本实例存活，再扫描失联 consumer。否则并发启动时会把刚创建实例的
        # processing list 当 orphan。旧版全局 proc key 无 owner，启动时迁移一次。
        await self._touch_heartbeat()
        await self._recover_stale_consumers()
        for i in range(self._n_workers):
            self._tasks.append(
                asyncio.create_task(self._loop(i), name=f"msgq-w{i}")
            )
        self._maintenance_task = asyncio.create_task(
            self._maintenance_loop(), name=f"msgq-maint-{self._consumer_id[:8]}"
        )
        logger.info(
            "[QUEUE] 启动 %d worker consumer=%s (lock_ttl=%ss max_attempts=%s)",
            self._n_workers, self._consumer_id[:8], self._lock_ttl,
            self._max_attempts,
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
        if self._maintenance_task is not None:
            self._maintenance_task.cancel()
            try:
                await self._maintenance_task
            except (asyncio.CancelledError, Exception):
                pass
            self._maintenance_task = None
        # 被 cancel 的 in-flight 已留在本实例 proc；只回灌本实例，不能触碰其他健康进程。
        await self._requeue_orphans()
        try:
            await self._r.delete(self._heartbeat_key())
        except Exception as e:
            logger.warning("[QUEUE] 删除 consumer heartbeat 失败 (靠 TTL): %s", e)
        # 关闭队列独占的 Redis 连接 (conv/timer/dedup store 各自的连接由各自管理)
        try:
            aclose = getattr(self._r, "aclose", None)
            if aclose is not None:
                await aclose()
        except Exception as e:
            logger.warning("[QUEUE] 关闭 Redis 连接失败: %s", e)
        logger.info("[QUEUE] 已停止")

    def _heartbeat_key(self, consumer_id: Optional[str] = None) -> str:
        cid = consumer_id or self._consumer_id
        return f"{self.CONSUMER_HEARTBEAT_PREFIX}{cid}"

    async def _touch_heartbeat(self) -> None:
        await self._r.set(
            self._heartbeat_key(), "1", ex=self._HEARTBEAT_TTL
        )

    async def _maintenance_loop(self) -> None:
        """刷新本实例 heartbeat，并周期恢复已失联实例的 processing list。"""
        while not self._stop.is_set():
            try:
                await self._touch_heartbeat()
                await self._recover_stale_consumers()
                await self._flush_pending_acks()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("[QUEUE] consumer 维护循环异常: %s", e)
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=self._MAINTENANCE_INTERVAL
                )
            except asyncio.TimeoutError:
                pass

    async def _recover_stale_consumers(self) -> None:
        """恢复旧版全局 proc 及 heartbeat 已过期的 consumer processing list。

        新实例拥有随机 consumer_id，启动前先写 heartbeat；因此只要 heartbeat 存在，
        其他实例绝不会回灌其 in-flight。硬崩实例 heartbeat 最迟 TTL 后消失，由健康实例
        的维护循环恢复，无需等待整个服务重启。
        """
        # 兼容升级前遗留的全局 processing list。延迟恢复以兼容旧实例仍存活的滚动升级；
        # 否则新实例无法区分“旧版活跃 delivery”和“旧版崩溃 orphan”。
        if time.monotonic() >= self._legacy_recovery_after:
            await self._recover_proc_key(type(self).Q_PROC, owner="legacy")

        keys = self._r.scan_iter(match=f"{self.Q_PROC_PREFIX}*")
        async for key in keys:
            if isinstance(key, bytes):
                key = key.decode("utf-8", errors="ignore")
            owner = key.removeprefix(self.Q_PROC_PREFIX)
            if not owner or owner == self._consumer_id:
                continue
            try:
                alive = await self._r.exists(self._heartbeat_key(owner))
            except Exception as e:
                logger.warning("[QUEUE] 检查 consumer=%s heartbeat 失败: %s", owner[:8], e)
                continue
            if alive:
                continue
            await self._recover_proc_key(key, owner=owner)

    async def _requeue_orphans(self) -> None:
        """优雅关闭时仅回灌本实例的 in-flight，不影响其他健康实例。"""
        await self._recover_proc_key(self._proc_key, owner=self._consumer_id)

    async def _flush_pending_acks(self) -> None:
        """重试成功处理后因瞬态 Redis 故障未完成的 LREM。"""
        for raw in list(self._pending_acks):
            try:
                await self._r.lrem(self._proc_key, 1, raw)
                self._pending_acks.discard(raw)
            except Exception as e:
                logger.warning("[QUEUE] success ACK 重试仍失败: %s", e)

    async def _recover_proc_key(self, proc_key: str, owner: str) -> int:
        """先确认清除 stale dedup，再原子暴露 delivery 回主队列。"""
        try:
            items = await self._r.lrange(proc_key, 0, -1)
        except Exception as e:
            logger.warning(
                "[QUEUE] orphan 读取失败 owner=%s (proc 残留待下次): %s",
                owner[:8], e,
            )
            return 0
        if not items:
            return 0

        cleared = 0
        dedup_ready = True
        for it in items:
            parse_raw = it.decode("utf-8", errors="ignore") if isinstance(it, bytes) else it
            try:
                env = json.loads(parse_raw)
                msgid = (env.get("payload") or {}).get("msgid", "")
                adapter = self._adapters.get(env.get("adapter"))
                if msgid and adapter is not None:
                    released = await adapter.dedup.release_processing(msgid)
                    if released:
                        cleared += 1
                    else:
                        dedup_ready = False
            except Exception as e:
                # 解析失败项不持有可定位的 dedup key，可安全移动后由 worker 入死信。
                logger.warning("[QUEUE] orphan release dedup 跳过 (解析失败): %s", e)
        if not dedup_ready:
            logger.warning(
                "[QUEUE] orphan owner=%s 的 dedup 清理未确认, 保留 %d 条 proc 待重试",
                owner[:8], len(items),
            )
            return 0

        try:
            moved = await self._r.eval(
                self._REQUEUE_LUA, 2, proc_key, self.Q_MAIN, *items
            )
        except Exception as e:
            logger.warning(
                "[QUEUE] orphan move 失败 owner=%s (proc 残留待下次): %s",
                owner[:8], e,
            )
            return 0
        logger.info(
            "[QUEUE] 原子回灌 owner=%s 的 %d 条 in-flight -> main, "
            "清 %d 个 stale dedup key", owner[:8], moved, cleared,
        )
        return int(moved or 0)

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
            raw = await self._mark_processing_started(raw, wid)
            await self._handle(raw, wid)

    async def _mark_processing_started(self, raw: str, wid: int) -> str:
        """给刚 claim 的 envelope 写 processing_started_at；失败时保留原 raw 继续处理。"""
        try:
            env = json.loads(raw)
            env["processing_started_at"] = time.time()
            claimed_raw = json.dumps(env, ensure_ascii=False)
            replaced = await self._r.eval(
                self._CLAIM_LUA, 1, self._proc_key, raw, claimed_raw
            )
            if replaced:
                return claimed_raw
            logger.warning(
                "[QUEUE w%s] claim 标记未找到 delivery, 继续处理原 envelope", wid
            )
        except Exception as e:
            logger.warning("[QUEUE w%s] 写 processing_started_at 失败: %s", wid, e)
        return raw

    async def _handle(self, raw: str, wid: int) -> None:
        # 1) 反序列化
        try:
            env = json.loads(raw)
            if not isinstance(env, dict):
                raise ValueError("queue envelope must be a JSON object")
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
                await self._move_to(self.Q_MAIN, raw, new_raw)
            else:
                logger.error(
                    "[QUEUE w%s] 死信 (重试耗尽) msgid=%s: %s",
                    wid, inbound.msgid, err,
                )
                await self._move_to(self.Q_DEAD, raw, new_raw)
            return

        # success: 移出 proc (仅 LREM; 失败则留 proc -> sweep 重投递, dedup 幂等)
        try:
            await self._r.lrem(self.Q_PROC, 1, raw)
            self._pending_acks.discard(raw)
        except Exception as e:
            self._pending_acks.add(raw)
            logger.warning(
                "[QUEUE w%s] success LREM 失败, 交维护循环重试 (dedup 幂等): %s",
                wid, e,
            )

    async def _move_to(
        self, dest: str, source_raw: str, dest_raw: Optional[str] = None
    ) -> None:
        """把 processing 中的 source_raw 原子移到 dest，可同时替换 envelope 内容。"""
        dest_raw = source_raw if dest_raw is None else dest_raw
        try:
            moved = await self._r.eval(
                self._MOVE_LUA,
                2,
                self._proc_key,
                dest,
                source_raw,
                dest_raw,
            )
            if not moved:
                logger.warning(
                    "[QUEUE] _move_to(%s) 未找到 source delivery, 可能已被恢复: %s",
                    dest, source_raw[:80],
                )
        except Exception as e:
            logger.warning("[QUEUE] _move_to(%s) 失败, 留 proc 待恢复: %s", dest, e)

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


async def create_message_queue(
    adapters: Dict[str, Any], processor: Any
) -> Optional[RedisMessageQueue]:
    """根据 ``settings.app.message_queue`` 选择队列。

    "memory" (默认) -> 返回 None (路由层回退 BackgroundTasks/create_task)。
    "redis" -> 连通性 PING 通过则返回 RedisMessageQueue; Redis 不可达 -> 回退 None
    (审查 P1 #2: 启动期发现 Redis 连不上即回退内存派发, 而非启动一个连不上的队列
    导致后续每条消息入队失败被静默 ACK 丢)。
    """
    from app.core.config import settings

    mode = (getattr(settings.app, "message_queue", "memory") or "memory").lower()
    if mode != "redis":
        return None
    client = None
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
        await client.ping()  # 连通性检查: 失败则回退内存派发
    except Exception as e:
        if client is not None:
            try:
                await client.aclose()
            except Exception:
                pass
        logger.warning(
            "Redis MessageQueue PING 失败, 回退 memory (BackgroundTasks): %s", e
        )
        return None
    logger.info(
        "MessageQueue: Redis 持久队列模式 (workers=%s)",
        getattr(settings.app, "queue_workers", 2),
    )
    return RedisMessageQueue(client, adapters, processor)


__all__ = ["RedisMessageQueue", "create_message_queue"]
