"""微信路由内存派发兜底测试。"""

from __future__ import annotations

from types import SimpleNamespace

from starlette.background import BackgroundTasks

from app.routes.wechat import _safe_process


class _Processor:
    def __init__(self, failures: int = 0) -> None:
        self.failures = failures
        self.calls: list[str] = []

    async def process(self, inbound, adapter) -> None:
        self.calls.append(inbound.msgid)
        if self.failures > 0:
            self.failures -= 1
            raise RuntimeError("boom")


async def test_safe_process_retries_transient_failure(monkeypatch):
    processor = _Processor(failures=1)
    inbound = SimpleNamespace(msgid="m1")

    async def _no_sleep(_delay):
        return None

    monkeypatch.setattr("app.routes.wechat.asyncio.sleep", _no_sleep)
    await _safe_process(processor, inbound, object(), max_attempts=2)

    assert processor.calls == ["m1", "m1"]


async def test_kf_background_failure_does_not_abort_later_message():
    failing = _Processor(failures=1)
    succeeding = _Processor()
    first = SimpleNamespace(msgid="m1")
    second = SimpleNamespace(msgid="m2")
    tasks = BackgroundTasks()
    tasks.add_task(_safe_process, failing, first, object(), 1)
    tasks.add_task(_safe_process, succeeding, second, object(), 1)

    await tasks()

    assert failing.calls == ["m1"]
    assert succeeding.calls == ["m2"]
