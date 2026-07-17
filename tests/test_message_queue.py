"""RedisMessageQueue + RedisDedupStore 单元测试。

用进程内 FakeRedis (实现 queue/dedup 用到的子集命令) 驱动, 不依赖真实 Redis。
覆盖: 入队往返、锁被占重入队、cancel 留 proc、真异常重试/死信、不可解析死信、
orphan sweep 回灌、RedisDedupStore 状态机。
"""

from __future__ import annotations

import asyncio
import fnmatch
import json
from dataclasses import asdict

import pytest

from app.protocols.base import InboundMessage
from app.services.dedup_store import RedisDedupStore
from app.services.message_queue import RedisMessageQueue


# ----------------------------------------------------------------------
# FakeRedis: 进程内模拟 (实现 queue + dedup 用到的命令子集)
# ----------------------------------------------------------------------


class FakeRedis:
    """最小 Redis 模拟。

    list: head=index0, tail=index-1。LPUSH 入 head, BRPOPLPUSH 弹 tail (FIFO)。
    用 asyncio.Condition 让 BRPOPLPUSH 阻塞等待 (模拟阻塞 pop)。
    """

    def __init__(self):
        self._lists: dict[str, list] = {}
        self._kv: dict[str, object] = {}
        self._cond = asyncio.Condition()

    async def lpush(self, key, *vals):
        async with self._cond:
            lst = self._lists.setdefault(key, [])
            for v in vals:
                lst.insert(0, v)  # head
            self._cond.notify_all()
            return len(lst)

    async def blmove(self, src, dst, src_dir, dest_dir, timeout=5):
        async with self._cond:
            try:
                await asyncio.wait_for(
                    self._cond.wait_for(lambda: bool(self._lists.get(src))),
                    timeout=timeout,
                )
            except asyncio.TimeoutError:
                return None
            lst_src = self._lists[src]
            val = lst_src.pop() if src_dir == "RIGHT" else lst_src.pop(0)
            lst_dst = self._lists.setdefault(dst, [])
            if dest_dir == "LEFT":
                lst_dst.insert(0, val)
            else:
                lst_dst.append(val)
            self._cond.notify_all()
            return val

    async def lrem(self, key, count, value):
        async with self._cond:
            lst = self._lists.get(key)
            if not lst:
                return 0
            target = count if count > 0 else len(lst)
            removed = 0
            new = []
            for v in lst:
                if removed < target and v == value:
                    removed += 1
                else:
                    new.append(v)
            self._lists[key] = new
            self._cond.notify_all()
            return removed

    async def lrange(self, key, start, end):
        async with self._cond:
            lst = self._lists.get(key, [])
            end_idx = len(lst) if end == -1 else end + 1
            return list(lst[start:end_idx])

    async def delete(self, *keys):
        async with self._cond:
            n = 0
            for k in keys:
                if k in self._lists:
                    del self._lists[k]
                    n += 1
                elif k in self._kv:
                    del self._kv[k]
                    n += 1
            self._cond.notify_all()
            return n

    async def exists(self, key):
        async with self._cond:
            return 1 if key in self._kv else 0

    async def scan_iter(self, match=None):
        async with self._cond:
            keys = list(set(self._lists) | set(self._kv))
        for key in keys:
            if match is None or fnmatch.fnmatch(key, match):
                yield key

    async def get(self, key):
        async with self._cond:
            return self._kv.get(key)

    async def set(self, key, value, nx=False, ex=None):
        async with self._cond:
            if nx and key in self._kv:
                self._cond.notify_all()
                return None
            self._kv[key] = value
            self._cond.notify_all()
            return True

    async def eval(self, script, numkeys, *args):
        s = script
        async with self._cond:
            keys = list(args[:numkeys])
            argv = list(args[numkeys:])
            if numkeys == 1:
                if "LSET" in s:
                    # CLAIM_LUA: KEYS[1]=proc, ARGV=[source_raw,claimed_raw]
                    proc = keys[0]
                    source_raw, claimed_raw = argv[0], argv[1]
                    lst = self._lists.get(proc, [])
                    try:
                        idx = lst.index(source_raw)
                    except ValueError:
                        return 0
                    lst[idx] = claimed_raw
                    self._cond.notify_all()
                    return 1
                # RELEASE_LUA: KEYS[1]=lock, ARGV[1]=token
                key, token = keys[0], argv[0]
                if self._kv.get(key) == token:
                    self._kv.pop(key, None)
                    self._cond.notify_all()
                    return 1
                return 0
            if numkeys == 2 and "for i=1,#ARGV" in s:
                # REQUEUE_LUA: 只移动调用方传入的精确 raw items
                proc, main = keys[0], keys[1]
                moved = 0
                for it in argv:
                    lst = self._lists.get(proc, [])
                    try:
                        idx = lst.index(it)
                    except ValueError:
                        continue
                    lst.pop(idx)
                    self._lists[proc] = lst
                    self._lists.setdefault(main, []).insert(0, it)
                    moved += 1
                self._cond.notify_all()
                return moved
            if numkeys == 2 and "LREM" in s:
                # MOVE_LUA: KEYS=[proc,dest], ARGV=[source_raw,dest_raw]
                proc, dest = keys[0], keys[1]
                source_raw, dest_raw = argv[0], argv[1]
                lst = self._lists.get(proc, [])
                try:
                    idx = lst.index(source_raw)
                except ValueError:
                    return 0
                lst.pop(idx)
                self._lists[proc] = lst
                self._lists.setdefault(dest, []).insert(0, dest_raw)
                self._cond.notify_all()
                return 1
            if numkeys == 2 and "exists" in s:
                # ACQUIRE_LUA: KEYS=[done, proc]
                done, proc = keys[0], keys[1]
                if done in self._kv:
                    return 0
                if proc not in self._kv:
                    self._kv[proc] = "1"
                    self._cond.notify_all()
                    return 1
                return 0
            if numkeys == 2:
                # MARK_DONE_LUA: KEYS=[done, proc]
                done, proc = keys[0], keys[1]
                self._kv[done] = "1"
                self._kv.pop(proc, None)
                self._cond.notify_all()
                return 1
            return 0


class FakeProcessor:
    def __init__(self, raise_exc=None):
        self.calls = []
        self._raise = raise_exc

    async def process(self, inbound, adapter):
        self.calls.append((inbound.msgid, inbound.text))
        if self._raise is not None:
            raise self._raise


class FakeAdapter:
    """带 dedup 的假 adapter (orphan sweep 会调 adapter.dedup.release_processing)。"""

    def __init__(self, dedup=None):
        from app.protocols.base import InMemoryDedupStore

        self.dedup = dedup if dedup is not None else InMemoryDedupStore()


def _inbound(msgid="m1", text="hi", protocol="kf", user_id="u1", open_kfid="k1"):
    return InboundMessage(
        protocol=protocol, msgid=msgid, msg_type="text", text=text,
        user_id=user_id, open_kfid=open_kfid,
    )


def _env(im, attempts=0, adapter="kf"):
    return json.dumps(
        {"id": "x", "adapter": adapter, "payload": asdict(im),
         "attempts": attempts, "enqueued_at": 0},
        ensure_ascii=False,
    )


# ----------------------------------------------------------------------
# RedisMessageQueue
# ----------------------------------------------------------------------


async def test_enqueue_roundtrip_processes_and_clears_proc():
    fake = FakeRedis()
    proc = FakeProcessor()
    q = RedisMessageQueue(fake, {"kf": FakeAdapter(), "bot": FakeAdapter()}, proc)
    q._n_workers = 1
    await q.start()
    await q.enqueue(_inbound("m1", "hello"), "kf")
    await asyncio.sleep(0.2)
    await q.stop()

    assert proc.calls == [("m1", "hello")]
    assert await fake.lrange(q.Q_PROC, 0, -1) == []
    assert await fake.lrange(q.Q_MAIN, 0, -1) == []
    assert await fake.lrange(q.Q_DEAD, 0, -1) == []


async def test_lock_held_returns_requeued_without_processing():
    fake = FakeRedis()
    proc = FakeProcessor()
    adapter = FakeAdapter()
    q = RedisMessageQueue(fake, {"kf": adapter}, proc)
    im = _inbound("m1", "hi")

    # 预占锁 (模拟另一 worker 持有同 user+scope)
    await fake.set("wecom:lock:u1:k1", "other-token", nx=True, ex=300)

    status, err = await q._run_with_lock(im, adapter, 0)
    assert status == "requeued"
    assert err is None
    assert proc.calls == []  # 未处理


async def test_lock_released_after_success():
    fake = FakeRedis()
    proc = FakeProcessor()
    adapter = FakeAdapter()
    q = RedisMessageQueue(fake, {"kf": adapter}, proc)
    await q._run_with_lock(_inbound("m1", "hi"), adapter, 0)
    # 成功后锁应已释放 (token 比对 del)
    assert await fake.get("wecom:lock:u1:k1") is None


async def test_claim_records_processing_started_at_in_proc_envelope():
    fake = FakeRedis()
    q = RedisMessageQueue(fake, {"kf": FakeAdapter()}, FakeProcessor())
    raw = _env(_inbound("m1", "hi"))
    await fake.lpush(q.Q_PROC, raw)

    claimed = await q._mark_processing_started(raw, 0)

    assert json.loads(claimed)["processing_started_at"] > 0
    assert await fake.lrange(q.Q_PROC, 0, -1) == [claimed]


async def test_success_ack_is_retried_by_maintenance_after_transient_lrem_failure():
    class _FlakyLremRedis(FakeRedis):
        def __init__(self):
            super().__init__()
            self.fail_once = True

        async def lrem(self, key, count, value):
            if self.fail_once:
                self.fail_once = False
                raise ConnectionError("transient")
            return await super().lrem(key, count, value)

    fake = _FlakyLremRedis()
    q = RedisMessageQueue(fake, {"kf": FakeAdapter()}, FakeProcessor())
    raw = _env(_inbound("m1", "hi"))
    await fake.lpush(q.Q_PROC, raw)

    await q._handle(raw, 0)
    assert raw in q._pending_acks
    assert await fake.lrange(q.Q_PROC, 0, -1) == [raw]

    await q._flush_pending_acks()
    assert q._pending_acks == set()
    assert await fake.lrange(q.Q_PROC, 0, -1) == []


async def test_cancel_leaves_in_proc():
    fake = FakeRedis()
    proc = FakeProcessor(raise_exc=asyncio.CancelledError())
    q = RedisMessageQueue(fake, {"kf": FakeAdapter()}, proc)
    raw = _env(_inbound("m1", "hi"))
    await fake.lpush(q.Q_PROC, raw)  # 模拟 worker 已 brpoplpush

    await q._handle(raw, 0)

    assert proc.calls == [("m1", "hi")]  # process 被调一次后 raise
    assert len(await fake.lrange(q.Q_PROC, 0, -1)) == 1  # 仍留 proc
    assert await fake.lrange(q.Q_MAIN, 0, -1) == []
    assert await fake.lrange(q.Q_DEAD, 0, -1) == []


async def test_true_exception_retries_then_dead():
    fake = FakeRedis()
    proc = FakeProcessor(raise_exc=ValueError("boom"))
    q = RedisMessageQueue(fake, {"kf": FakeAdapter()}, proc)
    q._max_attempts = 2
    im = _inbound("m1", "hi")

    # attempts=0 -> 重试 -> attempts=1 入 main
    raw0 = _env(im, attempts=0)
    await fake.lpush(q.Q_PROC, raw0)
    await q._handle(raw0, 0)
    main = await fake.lrange(q.Q_MAIN, 0, -1)
    assert len(main) == 1
    assert json.loads(main[0])["attempts"] == 1
    assert await fake.lrange(q.Q_PROC, 0, -1) == []

    # attempts=1 (== max-1) -> 再失败 -> attempts=2 == max -> 死信
    await fake.delete(q.Q_MAIN)
    raw1 = _env(im, attempts=1)
    await fake.lpush(q.Q_PROC, raw1)
    await q._handle(raw1, 0)
    assert len(await fake.lrange(q.Q_DEAD, 0, -1)) == 1
    assert len(await fake.lrange(q.Q_MAIN, 0, -1)) == 0
    assert await fake.lrange(q.Q_PROC, 0, -1) == []


async def test_unparseable_goes_to_dead():
    fake = FakeRedis()
    q = RedisMessageQueue(fake, {"kf": FakeAdapter()}, FakeProcessor())
    await fake.lpush(q.Q_PROC, "not-json{")
    await q._handle("not-json{", 0)
    assert len(await fake.lrange(q.Q_DEAD, 0, -1)) == 1
    assert len(await fake.lrange(q.Q_PROC, 0, -1)) == 0


async def test_non_object_json_envelope_goes_to_dead():
    fake = FakeRedis()
    q = RedisMessageQueue(fake, {"kf": FakeAdapter()}, FakeProcessor())
    raw = '["valid-json", "wrong-shape"]'
    await fake.lpush(q.Q_PROC, raw)

    await q._handle(raw, 0)

    assert len(await fake.lrange(q.Q_DEAD, 0, -1)) == 1
    assert await fake.lrange(q.Q_PROC, 0, -1) == []


async def test_unknown_adapter_goes_to_dead():
    fake = FakeRedis()
    q = RedisMessageQueue(fake, {"kf": FakeAdapter()}, FakeProcessor())
    raw = json.dumps({"id": "x", "adapter": "wechatpy", "payload": {}, "attempts": 0})
    await fake.lpush(q.Q_PROC, raw)
    await q._handle(raw, 0)
    assert len(await fake.lrange(q.Q_DEAD, 0, -1)) == 1


async def test_orphan_sweep_requeues_proc_to_main():
    fake = FakeRedis()
    q = RedisMessageQueue(fake, {"kf": FakeAdapter()}, FakeProcessor())
    raw = _env(_inbound("m1", "hi"))
    await fake.lpush(q.Q_PROC, raw)  # 模拟崩溃残留 in-flight

    await q._requeue_orphans()

    assert len(await fake.lrange(q.Q_MAIN, 0, -1)) == 1
    assert len(await fake.lrange(q.Q_PROC, 0, -1)) == 0


async def test_orphan_sweep_on_stop_requeues_inflight():
    """cancel 留 proc 的消息, stop() 时应回灌 main (至少一次投递)。"""
    fake = FakeRedis()
    proc = FakeProcessor(raise_exc=asyncio.CancelledError())
    q = RedisMessageQueue(fake, {"kf": FakeAdapter(), "bot": FakeAdapter()}, proc)
    q._n_workers = 1
    await q.start()
    await q.enqueue(_inbound("m1", "hi"), "kf")
    await asyncio.sleep(0.2)  # worker 处理 -> process raise CancelledError -> 留 proc
    await q.stop()

    # stop 的 orphan sweep 把 proc 回灌 main
    assert len(await fake.lrange(q.Q_PROC, 0, -1)) == 0
    assert len(await fake.lrange(q.Q_MAIN, 0, -1)) == 1


# ----------------------------------------------------------------------
# RedisDedupStore
# ----------------------------------------------------------------------


async def test_redis_dedup_state_machine():
    fake = FakeRedis()
    ds = RedisDedupStore(fake)

    # 首次 acquire
    assert await ds.acquire("m1", 300) is True
    # 处理中: 再 acquire 失败
    assert await ds.acquire("m1", 300) is False

    # mark_done -> 已完成态
    await ds.mark_done("m1")
    assert await ds.acquire("m1", 300) is False  # ttl 内防重发

    # 不同 msgid 互不影响
    assert await ds.acquire("m2", 300) is True
    # release_processing 后可重新 acquire
    await ds.release_processing("m2")
    assert await ds.acquire("m2", 300) is True


# ----------------------------------------------------------------------
# 审查 P1 修复回归测试 (#1 序列化 / #2 入队兜底 / #4 崩溃恢复+dedup)
# ----------------------------------------------------------------------


def test_to_serializable_makes_pydantic_json_safe():
    """审查 P1 #1: Pydantic 模型经 to_serializable 后可 json.dumps。

    未转换时 asdict 保留对象实例 -> json.dumps 抛 TypeError (KF 丢消息根因)。
    """
    import json as _json
    from pydantic import BaseModel

    from app.protocols.base import to_serializable

    class _M(BaseModel):
        a: int = 1
        b: str = "x"

    # 未转换: 直接 json.dumps 抛 TypeError (复现 P1 #1)
    with pytest.raises(TypeError):
        _json.dumps({"message": _M()})

    # 转换后: dict, 可序列化
    out = to_serializable(_M())
    assert out == {"a": 1, "b": "x"}
    _json.dumps({"message": out})  # 不抛


def test_kf_to_inbound_is_json_serializable():
    """审查 P1 #1: KfAdapter._to_inbound 产出的 InboundMessage 经 asdict+json.dumps 不抛。"""
    import json as _json
    from dataclasses import asdict as _asdict

    from app.models.wechat import WeChatMessage
    from app.protocols.kf_adapter import KfAdapter

    wm = WeChatMessage(msgid="m1", msgtype="text", send_time=0, origin=0)
    inbound = KfAdapter._to_inbound(wm)
    # raw 已是 dict (model_dump), asdict + json.dumps 不再抛 TypeError
    raw = _json.dumps({"payload": _asdict(inbound)})
    assert "m1" in raw


async def test_enqueue_serialize_failure_goes_to_dead():
    """审查 P1 #1 兜底: 入队序列化失败 -> 入死信 + 返回 False (不静默丢)。"""
    fake = FakeRedis()
    q = RedisMessageQueue(fake, {"kf": FakeAdapter()}, FakeProcessor())
    im = InboundMessage(protocol="kf", msgid="bad1", msg_type="text", text="hi",
                        user_id="u1", open_kfid="k1", raw={"bad": object()})
    ok = await q.enqueue(im, "kf")
    assert ok is False
    assert len(await fake.lrange(q.Q_DEAD, 0, -1)) == 1
    assert len(await fake.lrange(q.Q_MAIN, 0, -1)) == 0


async def test_enqueue_lpush_failure_returns_false():
    """审查 P1 #2: LPUSH 失败 (Redis 宕机) -> 返回 False (路由回退内存派发)。"""
    class _BoomRedis(FakeRedis):
        async def lpush(self, key, *vals):
            raise ConnectionRefusedError("redis down")

    q = RedisMessageQueue(_BoomRedis(), {"kf": FakeAdapter()}, FakeProcessor())
    ok = await q.enqueue(_inbound("m1", "hi"), "kf")
    assert ok is False


async def test_orphan_sweep_clears_stale_dedup_key():
    """审查 P1 #4: 硬崩留 proc 消息 + stale dedup proc key; orphan sweep 回灌 main
    同时清 stale key, 使重投递能重新 acquire (否则 acquire=False -> 跳过 -> 丢)。"""
    fake = FakeRedis()
    ds = RedisDedupStore(fake)
    adapter = FakeAdapter(dedup=ds)
    q = RedisMessageQueue(fake, {"kf": adapter}, FakeProcessor())

    # 模拟硬崩: msgid=m1 已 acquire (proc key set) + env 留在 Q_PROC (process 没 release)
    assert await ds.acquire("m1", 300) is True
    assert await ds.acquire("m1", 300) is False  # 处理中, 占有
    await fake.lpush(q.Q_PROC, _env(_inbound("m1", "hi")))

    await q._requeue_orphans()

    # 回灌到 main
    assert len(await fake.lrange(q.Q_MAIN, 0, -1)) == 1
    assert len(await fake.lrange(q.Q_PROC, 0, -1)) == 0
    # stale dedup proc key 已清 -> 重投递可重新 acquire (P1 #4 修复点)
    assert await ds.acquire("m1", 300) is True


async def test_recovery_does_not_touch_live_consumer_processing():
    """活跃实例有 heartbeat 时，另一实例启动/维护不得回灌其消息或清 processing key。"""
    fake = FakeRedis()
    ds = RedisDedupStore(fake)
    adapter = FakeAdapter(dedup=ds)
    q1 = RedisMessageQueue(
        fake, {"kf": adapter}, FakeProcessor(), consumer_id="consumer-live"
    )
    q2 = RedisMessageQueue(
        fake, {"kf": adapter}, FakeProcessor(), consumer_id="consumer-other"
    )

    await q1._touch_heartbeat()
    assert await ds.acquire("live-msg", 600) is True
    await fake.lpush(q1.Q_PROC, _env(_inbound("live-msg", "hi")))

    await q2._recover_stale_consumers()

    assert len(await fake.lrange(q1.Q_PROC, 0, -1)) == 1
    assert await fake.lrange(q1.Q_MAIN, 0, -1) == []
    assert await ds.acquire("live-msg", 600) is False


async def test_recovery_requeues_consumer_after_heartbeat_disappears():
    """硬崩实例无 heartbeat 时，健康实例恢复其列表并释放 stale dedup key。"""
    fake = FakeRedis()
    ds = RedisDedupStore(fake)
    adapter = FakeAdapter(dedup=ds)
    dead = RedisMessageQueue(
        fake, {"kf": adapter}, FakeProcessor(), consumer_id="consumer-dead"
    )
    rescuer = RedisMessageQueue(
        fake, {"kf": adapter}, FakeProcessor(), consumer_id="consumer-rescuer"
    )

    assert await ds.acquire("dead-msg", 600) is True
    await fake.lpush(dead.Q_PROC, _env(_inbound("dead-msg", "hi")))

    await rescuer._recover_stale_consumers()

    assert await fake.lrange(dead.Q_PROC, 0, -1) == []
    assert len(await fake.lrange(dead.Q_MAIN, 0, -1)) == 1
    assert await ds.acquire("dead-msg", 600) is True


async def test_recovery_keeps_orphan_hidden_when_dedup_release_fails():
    class _FailingDedup:
        async def release_processing(self, msgid):
            return False

    fake = FakeRedis()
    adapter = FakeAdapter(dedup=_FailingDedup())
    dead = RedisMessageQueue(
        fake, {"kf": adapter}, FakeProcessor(), consumer_id="consumer-dead"
    )
    rescuer = RedisMessageQueue(
        fake, {"kf": adapter}, FakeProcessor(), consumer_id="consumer-rescuer"
    )
    raw = _env(_inbound("dead-msg", "hi"))
    await fake.lpush(dead.Q_PROC, raw)

    await rescuer._recover_stale_consumers()

    assert await fake.lrange(dead.Q_PROC, 0, -1) == [raw]
    assert await fake.lrange(dead.Q_MAIN, 0, -1) == []


async def test_create_message_queue_ping_failure_returns_none(monkeypatch):
    """审查 P1 #2: 启动期 Redis PING 失败 -> 返回 None (回退内存派发)。"""
    import redis.asyncio as aioredis
    from app.core.config import settings
    from app.services.message_queue import create_message_queue

    class _DeadRedis:
        def __init__(self, *a, **kw):
            pass

        async def ping(self):
            raise ConnectionRefusedError("no redis")

        async def aclose(self):
            pass

    monkeypatch.setattr(settings.app, "message_queue", "redis")
    monkeypatch.setattr(aioredis, "Redis", _DeadRedis)
    result = await create_message_queue({"kf": FakeAdapter()}, FakeProcessor())
    assert result is None
