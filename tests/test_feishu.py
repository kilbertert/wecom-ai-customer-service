"""feishu_bitable._write_with_retry 测试 (审查 P1 #7)。

覆盖: 1254291 Write conflict 指数退避重试 -> 成功; 重试耗尽 -> 抛; 非冲突码不重试。
"""

from __future__ import annotations

import pytest

import app.services.feishu_bitable as fb
from app.services.feishu_bitable import FeishuBitableError, _write_with_retry


def _no_sleep(monkeypatch):
    """跳过真实退避 sleep, 加速测试。"""
    monkeypatch.setattr(fb.time, "sleep", lambda _s: None)


def test_write_with_retry_retries_on_1254291_then_succeeds(monkeypatch):
    """遇 1254291 退避重试, 最终成功 (审查 P1 #7)。"""
    _no_sleep(monkeypatch)
    seq = iter([1254291, 1254291, 0])
    calls = []

    def _do():
        code = next(seq)
        calls.append(code)
        return {"code": code, "msg": "x", "data": {}}

    data = _write_with_retry("新增记录", _do)
    assert data["code"] == 0
    assert calls == [1254291, 1254291, 0]


def test_write_with_retry_raises_after_exhausting_retries(monkeypatch):
    """持续 1254291 -> 重试耗尽 -> 抛 FeishuBitableError (code=1254291)。"""
    _no_sleep(monkeypatch)
    calls = []

    def _do():
        calls.append(1)
        return {"code": 1254291, "msg": "Write conflict", "data": {}}

    with pytest.raises(FeishuBitableError) as ei:
        _write_with_retry("修改记录", _do)
    assert "1254291" in str(ei.value)
    # 1 次初始 + 3 次重试 = 4 次
    assert len(calls) == 1 + len(fb._WRITE_RETRY_DELAYS)


def test_write_with_retry_no_retry_on_non_conflict_error(monkeypatch):
    """非 1254291 业务错误直接抛, 不重试 (避免无效重试鉴权/字段错)。"""
    _no_sleep(monkeypatch)
    calls = []

    def _do():
        calls.append(1)
        return {"code": 9999, "msg": "bad field", "data": {}}

    with pytest.raises(FeishuBitableError):
        _write_with_retry("新增记录", _do)
    assert calls == [1]  # 未重试


def test_write_with_retry_success_first_try(monkeypatch):
    """首次即成功, 不重试。"""
    _no_sleep(monkeypatch)
    calls = []

    def _do():
        calls.append(1)
        return {"code": 0, "msg": "", "data": {"record": {"record_id": "rec1"}}}

    data = _write_with_retry("新增记录", _do)
    assert data["code"] == 0
    assert calls == [1]
