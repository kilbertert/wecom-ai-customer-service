"""RedisMessageQueue + RedisDedupStore 单元测试。

用进程内 FakeRedis (实现 queue/dedup 用到的子集命令) 驱动, 不依赖真实 Redis。
覆盖: 入队往返、锁被占重入队、cancel 留 proc、真异常重试/死信、不可解析死信、
orphan sweep 回灌、RedisDedupStore 状态机。
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict

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
                # RELEASE_LUA: KEYS[1]=lock, ARGV[1]=token
                key, token = keys[0], argv[0]
                if self._kv.get(key) == token:
                    self._kv.pop(key, None)
                    self._cond.notify_all()
                    return 1
                return 0
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
    pass


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

    # attempts=1 (== max-1) -> 再失败 -> attempts=2 == max -> 死信
    await fake.delete(q.Q_MAIN)
    raw1 = _env(im, attempts=1)
    await fake.lpush(q.Q_PROC, raw1)
    await q._handle(raw1, 0)
    assert len(await fake.lrange(q.Q_DEAD, 0, -1)) == 1
    assert len(await fake.lrange(q.Q_MAIN, 0, -1)) == 0


async def test_unparseable_goes_to_dead():
    fake = FakeRedis()
    q = RedisMessageQueue(fake, {"kf": FakeAdapter()}, FakeProcessor())
    await fake.lpush(q.Q_PROC, "not-json{")
    await q._handle("not-json{", 0)
    assert len(await fake.lrange(q.Q_DEAD, 0, -1)) == 1
    assert len(await fake.lrange(q.Q_PROC, 0, -1)) == 0


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
