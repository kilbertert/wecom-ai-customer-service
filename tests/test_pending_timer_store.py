"""PendingTimerStore.clear_if_match 原子 CAS 测试 (审查 P1 #5)。

覆盖: 匹配才清 / 不匹配不清 / InMemory + Redis 两种实现。
防旧 (被 revoke) 任务延迟 fire 误删新 arm 的 timer (get-then-clear 非原子竞态)。
"""

from __future__ import annotations

import json

import pytest

from app.services.pending_timer_store import (
    InMemoryPendingTimerStore,
    PendingTimer,
    RedisPendingTimerStore,
)


def _timer(task_id="T1", state="await_confirm_bug"):
    return PendingTimer(task_id=task_id, state=state, record_id="r1", armed_at=0.0)


# ----------------------------------------------------------------------
# InMemoryPendingTimerStore
# ----------------------------------------------------------------------


async def test_inmemory_clear_if_match_clears_when_matched():
    s = InMemoryPendingTimerStore()
    await s.arm("u1", "kf1", _timer("T1"))
    assert await s.clear_if_match("u1", "kf1", "T1") is True
    assert await s.get("u1", "kf1") is None  # 已清


async def test_inmemory_clear_if_match_keeps_when_mismatch():
    """旧任务 (T_old) 延迟 fire, store 里已是新 arm (T_new) -> 不清, 防误删新 timer。"""
    s = InMemoryPendingTimerStore()
    await s.arm("u1", "kf1", _timer("T_new"))
    assert await s.clear_if_match("u1", "kf1", "T_old") is False
    # 新 timer 仍在
    assert (await s.get("u1", "kf1")).task_id == "T_new"


async def test_inmemory_clear_if_match_no_pending():
    s = InMemoryPendingTimerStore()
    assert await s.clear_if_match("u1", "kf1", "T1") is False


# ----------------------------------------------------------------------
# RedisPendingTimerStore (用进程内 fake redis 模拟 cjson Lua CAS)
# ----------------------------------------------------------------------


class _FakeRedis:
    """最小 redis 模拟: set/get/delete + eval(_CLEAR_IF_MATCH_LUA)。"""

    def __init__(self):
        self._kv: dict = {}

    async def set(self, k, v, ex=None):
        self._kv[k] = v

    async def get(self, k):
        return self._kv.get(k)

    async def delete(self, k):
        return 1 if self._kv.pop(k, None) is not None else 0

    async def eval(self, script, numkeys, *args):
        # _CLEAR_IF_MATCH_LUA: KEYS[1]=k, ARGV[1]=expected_task_id
        k = args[0]
        expected = args[1]
        val = self._kv.get(k)
        if val is None:
            return 0
        if isinstance(val, bytes):
            val = val.decode("utf-8")
        try:
            obj = json.loads(val)
        except Exception:
            return 0
        if obj.get("task_id") == expected:
            self._kv.pop(k, None)
            return 1
        return 0


async def test_redis_clear_if_match_clears_when_matched():
    s = RedisPendingTimerStore(_FakeRedis())
    await s.arm("u1", "kf1", _timer("T1"))
    assert await s.clear_if_match("u1", "kf1", "T1") is True
    assert await s.get("u1", "kf1") is None


async def test_redis_clear_if_match_keeps_when_mismatch():
    """CAS: 旧任务 T_old 误 fire 时 store 已是新 T_new -> 原子比对不匹配 -> 不清。"""
    s = RedisPendingTimerStore(_FakeRedis())
    await s.arm("u1", "kf1", _timer("T_new"))
    assert await s.clear_if_match("u1", "kf1", "T_old") is False
    assert (await s.get("u1", "kf1")).task_id == "T_new"


async def test_redis_clear_if_match_no_pending():
    s = RedisPendingTimerStore(_FakeRedis())
    assert await s.clear_if_match("u1", "kf1", "T1") is False


@pytest.mark.parametrize("impl", ["memory", "redis"])
async def test_clear_if_match_atomic_concurrent_arm(impl):
    """模拟竞态: T_old fire 读到 T_old, 但在 clear 前 T_new arm 覆盖。
    clear_if_match(T_old) 必须因 CAS 不匹配而不清 (新 T_new 保留)。"""
    if impl == "memory":
        s = InMemoryPendingTimerStore()
    else:
        s = RedisPendingTimerStore(_FakeRedis())
    await s.arm("u1", "kf1", _timer("T_old"))
    # 在 clear 前新 arm 覆盖 (模拟另一路径 arm 了新 timer)
    await s.arm("u1", "kf1", _timer("T_new"))
    # 旧任务此时 fire, 想清 T_old -- 但 store 已是 T_new
    assert await s.clear_if_match("u1", "kf1", "T_old") is False
    assert (await s.get("u1", "kf1")).task_id == "T_new"
