"""企微智能表格写入/查询服务 (二阶段 bug 反馈表)。

封装三类操作:
    - ``add_record(webhook_key, values)``: N16 新增主表记录, 返回 record_id
    - ``update_record(webhook_key, record_id, values)``: N14 修改主表记录
    - ``add_cache_record(webhook_key, values)``: N19 超时缓存表新增

查询 (N2/N9) 需 access_token, 见 :class:`SmartSheetQueryService`。

Webhook 机制 (企微"接收外部数据到智能表格", 文档 path/101240 & 101241):
    - 同一 URL ``https://qyapi.weixin.qq.com/cgi-bin/wedoc/smartsheet/webhook?key=KEY``
    - 请求体区分 add_records / update_records
    - 鉴权仅靠 URL 里的 key, 无需 access_token
    - add 返回值含 record_id; update 必须提供 record_id
    - single_select 字段值必须是 enum 内文本, 格式 [{"text": "选项"}]
    - 不支持给公式/自动编号/创建人/时间等字段写值

本服务为**同步实现** (供 Celery task 调用), 也提供 async 版本供 FastAPI 路由用。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)

_WEBHOOK_BASE = "https://qyapi.weixin.qq.com/cgi-bin/wedoc/smartsheet/webhook"
_HTTP_TIMEOUT = 30.0


class SmartSheetError(Exception):
    """智能表格写入/查询异常。"""


def _webhook_url(webhook_key: str) -> str:
    return f"{_WEBHOOK_BASE}?key={webhook_key}"


def _check_errcode(resp_json: Dict[str, Any], action: str) -> None:
    """企微统一错误码检查。errcode!=0 抛 SmartSheetError。"""
    errcode = resp_json.get("errcode", -1)
    if errcode != 0:
        raise SmartSheetError(
            f"智能表格{action}失败: errcode={errcode} "
            f"errmsg={resp_json.get('errmsg')}"
        )


# ======================================================================
# 同步版本 (Celery task 用)
# ======================================================================

def add_record_sync(
    webhook_key: str, values: Dict[str, Any]
) -> str:
    """新增一条记录 (N16)。返回 record_id。

    Args:
        webhook_key: 目标表的 webhook key
        values: 字段映射, key=field_id, value=对应类型值
                (single_select 用 [{"text": "选项"}], text 用 str, checkbox 用 bool)

    Returns:
        新记录的 record_id (企微返回)
    """
    body = {"add_records": [{"values": values}]}
    with httpx.Client(timeout=_HTTP_TIMEOUT) as client:
        r = client.post(_webhook_url(webhook_key), json=body)
        r.raise_for_status()
        data = r.json()
    _check_errcode(data, "新增记录")
    records = data.get("add_records") or []
    if not records:
        raise SmartSheetError("新增记录返回空 add_records")
    record_id = records[0].get("record_id") or ""
    if not record_id:
        raise SmartSheetError("新增记录未返回 record_id")
    logger.info("[SmartSheet] 新增记录成功 record_id=%s", record_id)
    return record_id


def update_record_sync(
    webhook_key: str, record_id: str, values: Dict[str, Any]
) -> None:
    """修改一条记录 (N14)。按 record_id 精确定位。

    Args:
        webhook_key: 主表 webhook key
        record_id: 要修改的记录 id (来自 add 返回或查询)
        values: 仅包含要更新的字段 (未传字段保持不变)
    """
    if not record_id:
        raise SmartSheetError("update_record 需要 record_id")
    body = {
        "update_records": [{"record_id": record_id, "values": values}]
    }
    with httpx.Client(timeout=_HTTP_TIMEOUT) as client:
        r = client.post(_webhook_url(webhook_key), json=body)
        r.raise_for_status()
        data = r.json()
    _check_errcode(data, "修改记录")
    logger.info("[SmartSheet] 修改记录成功 record_id=%s", record_id)


def add_cache_record_sync(
    cache_webhook_key: str, values: Dict[str, Any]
) -> str:
    """新增一条缓存表记录 (N19 超时未完成暂存)。返回 record_id。

    缓存表复用主表 schema + 额外 "关联主表record_id" 字段。
    values 里应包含该关联字段 (新增超时则为空串)。
    """
    return add_record_sync(cache_webhook_key, values)


# ======================================================================
# 异步版本 (FastAPI 路由 / MessageProcessor 用)
# ======================================================================

async def add_record(
    webhook_key: str, values: Dict[str, Any]
) -> str:
    """异步版 N16 新增, 返回 record_id。"""
    body = {"add_records": [{"values": values}]}
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
        r = await client.post(_webhook_url(webhook_key), json=body)
        r.raise_for_status()
        data = r.json()
    _check_errcode(data, "新增记录")
    records = data.get("add_records") or []
    if not records:
        raise SmartSheetError("新增记录返回空 add_records")
    record_id = records[0].get("record_id") or ""
    if not record_id:
        raise SmartSheetError("新增记录未返回 record_id")
    return record_id


async def update_record(
    webhook_key: str, record_id: str, values: Dict[str, Any]
) -> None:
    """异步版 N14 修改。"""
    if not record_id:
        raise SmartSheetError("update_record 需要 record_id")
    body = {
        "update_records": [{"record_id": record_id, "values": values}]
    }
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
        r = await client.post(_webhook_url(webhook_key), json=body)
        r.raise_for_status()
        data = r.json()
    _check_errcode(data, "修改记录")


__all__ = [
    "SmartSheetError",
    "add_record_sync",
    "update_record_sync",
    "add_cache_record_sync",
    "add_record",
    "update_record",
]
