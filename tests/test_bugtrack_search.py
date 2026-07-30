"""飞书 Bug 查重多字段召回与候选排序回归。"""

from __future__ import annotations

import pytest

import app.services.smartsheet_query_service as query_module
from app.services.smartsheet_query_service import (
    EXISTING_ISSUE_SCORE_THRESHOLD,
    SmartSheetQueryService,
)


def _record(record_id: str, module: str, op_desc: str) -> dict:
    return {
        "record_id": record_id,
        "fields": {"模块/功能点": module, "操作描述": op_desc},
    }


@pytest.mark.asyncio
async def test_module_field_recalls_existing_record_when_full_keyword_misses(
    monkeypatch,
):
    old = _record(
        "rec-old",
        "设备白名单",
        "Web后台充电桩模块，执行重置后原配置白名单数据丢失，列表暂无数据。",
    )

    def fake_search(keyword: str, field: str, limit: int):
        assert limit >= 20
        if keyword == "设备白名单" and field == "模块/功能点":
            return [old]
        return []

    monkeypatch.setattr(query_module, "_feishu_search", fake_search)
    records = await SmartSheetQueryService().search_by_feedback(
        "设备白名单",
        module="设备白名单",
        op_desc="后台设备白名单页面查看汽车桩时暂无数据，历史数据丢失",
        limit=5,
    )

    assert [record["record_id"] for record in records] == ["rec-old"]


@pytest.mark.asyncio
async def test_same_module_candidates_are_ranked_by_operation_similarity(monkeypatch):
    unrelated = _record(
        "rec-unrelated",
        "订单管理",
        "订单详情页导出按钮点击后没有生成文件。",
    )
    duplicate = _record(
        "rec-duplicate",
        "订单管理",
        "后台订单结算时提示失败，订单无法完成结算。",
    )

    def fake_search(keyword: str, field: str, _limit: int):
        if keyword == "订单管理" and field == "模块/功能点":
            return [unrelated, duplicate]
        return []

    monkeypatch.setattr(query_module, "_feishu_search", fake_search)
    records = await SmartSheetQueryService().search_by_feedback(
        "订单结算失败",
        module="订单管理",
        op_desc="后台订单结算操作失败，订单不能正常结算",
        limit=5,
    )

    assert [record["record_id"] for record in records] == [
        "rec-duplicate",
        "rec-unrelated",
    ]


@pytest.mark.asyncio
async def test_records_returned_by_multiple_probes_are_deduplicated(monkeypatch):
    duplicate = _record("rec-one", "设备白名单", "白名单数据丢失")

    def fake_search(_keyword: str, _field: str, _limit: int):
        return [duplicate]

    monkeypatch.setattr(query_module, "_feishu_search", fake_search)
    records = await SmartSheetQueryService().search_by_feedback(
        "白名单", module="设备白名单", op_desc="白名单数据丢失", limit=5
    )

    assert [record["record_id"] for record in records] == ["rec-one"]


@pytest.mark.asyncio
async def test_long_raw_description_uses_failure_anchor_for_recall(monkeypatch):
    existing = _record(
        "rec-existing",
        "订单管理",
        "后台订单结算时提示失败，订单无法完成结算。",
    )
    probes: list[tuple[str, str]] = []

    def fake_search(keyword: str, field: str, _limit: int):
        probes.append((keyword, field))
        if keyword == "订单结算" and field == "操作描述":
            return [existing]
        return []

    monkeypatch.setattr(query_module, "_feishu_search", fake_search)
    records = await SmartSheetQueryService().search_by_feedback(
        "Web后台订单结算失败，点击重试后仍然报错",
        module="",
        op_desc="Web后台订单结算失败，点击重试后仍然报错",
        limit=5,
    )

    assert ("订单结算", "操作描述") in probes
    assert [record["record_id"] for record in records] == ["rec-existing"]


def test_same_module_score_requires_operation_similarity() -> None:
    service = SmartSheetQueryService()
    duplicate = _record("rec-duplicate", "订单管理", "后台订单结算失败")
    unrelated = _record("rec-unrelated", "订单管理", "用户资料导出按钮样式异常")

    duplicate_score = service.feedback_score(
        duplicate,
        keyword="结算失败",
        module="订单管理",
        op_desc="后台订单结算失败",
    )
    unrelated_score = service.feedback_score(
        unrelated,
        keyword="结算失败",
        module="订单管理",
        op_desc="后台订单结算失败",
    )

    assert duplicate_score >= EXISTING_ISSUE_SCORE_THRESHOLD
    assert unrelated_score < EXISTING_ISSUE_SCORE_THRESHOLD


def test_module_keyword_alone_does_not_mark_different_operation_as_duplicate() -> None:
    service = SmartSheetQueryService()
    unrelated = _record("rec-unrelated", "设备白名单", "设备白名单新增按钮无法点击")
    score = service.feedback_score(
        unrelated,
        keyword="白名单",
        module="设备白名单",
        op_desc="后台设备白名单页面查看汽车桩时暂无数据，历史数据丢失",
    )
    assert score < EXISTING_ISSUE_SCORE_THRESHOLD


def test_exact_operation_match_without_module_reaches_threshold() -> None:
    service = SmartSheetQueryService()
    description = "Web后台充电桩模块，执行重置操作后，原配置白名单数据丢失，列表显示暂无数据。"
    existing = _record("rec-existing", "设备白名单", description)

    score = service.feedback_score(
        existing,
        keyword=description,
        module="",
        op_desc=description,
    )

    assert score >= EXISTING_ISSUE_SCORE_THRESHOLD


def test_short_generic_operation_without_module_does_not_get_exact_bonus() -> None:
    service = SmartSheetQueryService()
    generic = _record("rec-generic", "订单管理", "保存失败")

    score = service.feedback_score(
        generic,
        keyword="保存失败",
        module="",
        op_desc="保存失败",
    )

    assert score < EXISTING_ISSUE_SCORE_THRESHOLD
