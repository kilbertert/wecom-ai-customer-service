"""飞书 Bug 查重多字段召回与候选排序回归。"""

from __future__ import annotations

import pytest

import app.services.smartsheet_query_service as query_module
from app.services.smartsheet_query_service import SmartSheetQueryService


def _record(record_id: str, module: str, op_desc: str) -> dict:
    return {
        "record_id": record_id,
        "fields": {"模块/功能点": module, "操作描述": op_desc},
    }


@pytest.mark.asyncio
async def test_module_field_recalls_existing_record_when_full_keyword_misses(monkeypatch):
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

