"""企微智能表格 MCP 客户端 (StreamableHttp JSON-RPC)。

走"智能机器人文档能力" MCP 通道, 用 apikey 鉴权, 绕开 wedoc REST API 的 48002
(自建应用无文档权限) 问题。MCP 工具集完整: 查表/建字段/增删改记录均支持。

⚠️ MCP 协议握手 (极易踩坑, 见 memory wecom-48002-permission):
    必须按顺序: initialize → notifications/initialized → tools/call。
    漏发 notifications/initialized 会报 850003 authorization expired (误导性)。

⚠️ MCP add_records 的 records key 必须用 **field_title(中文标题)**, 不是 field_id
    (与 webhook API 相反)。single_select 用 [{"text":"选项"}], 选项写入时自动建。

本客户端为**同步实现** (httpx.Client), 供 Celery task 和 async 上下文 (用
asyncio.to_thread 包装) 通用。保持无状态: 每次调用都重新 initialize (企微 MCP
端点不返回 Mcp-Session-Id, 无需维护 session)。
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)

_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}
_TIMEOUT = 30.0


class MCPError(Exception):
    """MCP 调用异常。"""


def _mcp_url() -> str:
    apikey = settings.bugtrack.mcp_apikey
    if not apikey:
        raise MCPError("未配置 BUGTRACK_MCP_APIKEY")
    return f"{settings.bugtrack.mcp_url}?apikey={apikey}"


def _call_tool(tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    """调一个 MCP 工具, 返回其 content text 解析后的 JSON dict。

    完整握手: initialize → notifications/initialized → tools/call。
    """
    url = _mcp_url()
    with httpx.Client(timeout=_TIMEOUT) as c:
        # 1. initialize
        r0 = c.post(url, headers=_HEADERS, json={
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "wecom-backend", "version": "1.0"},
            },
        })
        r0.raise_for_status()
        # 2. notifications/initialized (必发, 否则 tools/call 报 850003)
        c.post(url, headers=_HEADERS, json={
            "jsonrpc": "2.0", "method": "notifications/initialized",
        })
        # 3. tools/call
        r = c.post(url, headers=_HEADERS, json={
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": tool_name, "arguments": arguments},
        })
        r.raise_for_status()
        resp = r.json()

    # MCP 响应: {"result": {"content": [{"type":"text","text":"<JSON字符串>"}]}}
    if "error" in resp:
        raise MCPError(f"MCP error: {resp['error']}")
    result = resp.get("result") or {}
    content = result.get("content") or []
    if not content:
        raise MCPError(f"MCP {tool_name} 返回空 content")
    text = content[0].get("text", "")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # 非 JSON (如纯文本错误), 原样返回
        return {"raw_text": text, "errcode": -1, "errmsg": text[:200]}


def _check_errcode(data: Dict[str, Any], action: str) -> None:
    """企微统一 errcode 检查。"""
    errcode = data.get("errcode", -1)
    if errcode != 0:
        raise MCPError(
            f"MCP {action} 失败: errcode={errcode} errmsg={data.get('errmsg')}"
        )


# ======================================================================
# 查表 (N2/N9)
# ======================================================================

def search_records_by_keyword(keyword: str, limit: int = 20) -> List[Dict[str, Any]]:
    """N2 查表: 用关键词在主表"操作描述"字段 CONTAINS 匹配。

    MCP 没有 filter 查询, 改用 smartsheet_get_records 拉全部记录后本地过滤
    (主表记录量不大, 可接受)。返回命中记录列表 (含 record_id + values)。

    Args:
        keyword: 关键词 (中文)
        limit: 最多返回条数

    Returns:
        命中记录列表, 空列表表示无命中。
    """
    keyword = (keyword or "").strip()
    if not keyword:
        return []
    docid = settings.bugtrack.main_doc_id
    sheet_id = settings.bugtrack.main_sheet_id
    if not docid or not sheet_id:
        logger.warning("[MCP] 查表缺 doc_id/sheet_id")
        return []
    try:
        # get_records 拉记录 (key_type=field_title, values key 是中文标题)
        data = _call_tool("smartsheet_get_records", {
            "docid": docid, "sheet_id": sheet_id,
            "key_type": "CELL_VALUE_KEY_TYPE_FIELD_TITLE",
            "offset": 0, "limit": 1000,
        })
        _check_errcode(data, "search_records")
        records = data.get("records") or []
        # 本地过滤: 操作描述 / 模块/功能点 含 keyword
        hits = []
        kw = keyword.lower()
        for rec in records:
            vals = rec.get("values") or {}
            op = _cell_to_str(vals.get("操作描述"))
            mod = _cell_to_str(vals.get("模块/功能点"))
            if kw in op.lower() or kw in mod.lower():
                hits.append(rec)
                if len(hits) >= limit:
                    break
        logger.info("[MCP] 关键词'%s' 命中 %d 条", keyword, len(hits))
        return hits
    except (MCPError, httpx.HTTPError) as e:
        logger.warning("[MCP] 查表失败: %s", e)
        return []


def get_record(record_id: str) -> Optional[Dict[str, Any]]:
    """N9 读旧行: 按 record_id 精确取单条。

    MCP get_records 支持 record_ids 参数。
    """
    if not record_id:
        return None
    docid = settings.bugtrack.main_doc_id
    sheet_id = settings.bugtrack.main_sheet_id
    try:
        data = _call_tool("smartsheet_get_records", {
            "docid": docid, "sheet_id": sheet_id,
            "key_type": "CELL_VALUE_KEY_TYPE_FIELD_TITLE",
            "record_ids": [record_id],
            "limit": 1,
        })
        _check_errcode(data, "get_record")
        records = data.get("records") or []
        return records[0] if records else None
    except (MCPError, httpx.HTTPError) as e:
        logger.warning("[MCP] 读单条失败: %s", e)
        return None


# ======================================================================
# 写表 (N16/N14/N19)
# ======================================================================

def add_record(values: Dict[str, Any]) -> str:
    """N16 新增记录。values key 必须用 field_title(中文标题)。

    Returns:
        新记录的 record_id
    """
    data = _call_tool("smartsheet_add_records", {
        "docid": settings.bugtrack.main_doc_id,
        "sheet_id": settings.bugtrack.main_sheet_id,
        "records": [{"values": values}],
    })
    _check_errcode(data, "add_record")
    records = data.get("records") or []
    if not records:
        raise MCPError("add_record 返回空 records")
    rid = records[0].get("record_id", "")
    if not rid:
        raise MCPError("add_record 未返回 record_id")
    logger.info("[MCP] 新增记录成功 record_id=%s", rid)
    return rid


def update_record(record_id: str, values: Dict[str, Any]) -> None:
    """N14 修改记录。values key 用 field_title。按 record_id 定位。"""
    if not record_id:
        raise MCPError("update_record 需要 record_id")
    data = _call_tool("smartsheet_update_records", {
        "docid": settings.bugtrack.main_doc_id,
        "sheet_id": settings.bugtrack.main_sheet_id,
        "records": [{"record_id": record_id, "values": values}],
    })
    _check_errcode(data, "update_record")
    logger.info("[MCP] 修改记录成功 record_id=%s", record_id)


# ======================================================================
# 辅助
# ======================================================================

def _cell_to_str(value: Any) -> str:
    """企微单元格值归一字符串 (text 数组取 text, 标量直转)。"""
    if value is None:
        return ""
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, dict):
                parts.append(str(item.get("text") or item.get("name") or ""))
            else:
                parts.append(str(item))
        return "".join(parts)
    if isinstance(value, dict):
        return str(value.get("text") or value.get("name") or "")
    return str(value)


def record_to_summary(record: Dict[str, Any]) -> Dict[str, str]:
    """记录归一成 {record_id, module, op_desc, summary} 便于 Dify 消费。"""
    vals = record.get("values") or {}
    return {
        "record_id": record.get("record_id", ""),
        "module": _cell_to_str(vals.get("模块/功能点")),
        "op_desc": _cell_to_str(vals.get("操作描述")),
        "summary": _cell_to_str(vals.get("产品备注")),
    }


__all__ = [
    "MCPError",
    "search_records_by_keyword",
    "get_record",
    "add_record",
    "update_record",
    "record_to_summary",
]
