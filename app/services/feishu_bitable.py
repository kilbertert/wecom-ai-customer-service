"""飞书多维表格 (Bitable) 客户端。

二阶段 bug 反馈表用飞书多维表格替代企微智能表格 (企微查表是死路, 见
memory wecom-smartsheet-deadend)。飞书 ``records/search`` 支持 ``contains``
服务端过滤, 解决查表问题。

鉴权: ``tenant_access_token`` (app_id+app_secret 换, 2h 有效, 双 token 并存)。
两层权限: 应用需有 ``bitable:app`` scope + 是目标表的协作者/所有者。

⚠️ 并发写: 同一数据表不支持并发写 (报 1254291 Write conflict)。Celery worker
concurrency=1 天然串行; FastAPI 侧写表需注意 (本模块不内置锁, 调用方串行调)。

本模块为**同步实现** (httpx.Client), 供 Celery task 直接调; async 上下文用
``asyncio.to_thread`` 包装 (见 smartsheet_query_service)。
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)

_BASE = "https://open.feishu.cn/open-apis"
_TIMEOUT = 30.0

# token 缓存 (进程级): {token, expire_at}
_token_cache: Dict[str, Any] = {"token": "", "expire_at": 0.0}


class FeishuBitableError(Exception):
    """飞书多维表格调用异常。"""


# ======================================================================
# token
# ======================================================================

def _get_token() -> str:
    """获取 tenant_access_token, 带缓存 (剩余<5min 刷新)。"""
    now = time.time()
    cached = _token_cache["token"]
    # 剩余 >5min 直接用缓存
    if cached and _token_cache["expire_at"] - now > 300:
        return cached

    app_id = settings.bugtrack.feishu_app_id
    app_secret = settings.bugtrack.feishu_app_secret
    if not app_id or not app_secret:
        raise FeishuBitableError(
            "未配置 FEISHU_APP_ID / FEISHU_APP_SECRET"
        )
    with httpx.Client(timeout=_TIMEOUT) as c:
        r = c.post(
            f"{_BASE}/auth/v3/tenant_access_token/internal",
            json={"app_id": app_id, "app_secret": app_secret},
        )
        r.raise_for_status()
        data = r.json()
    if data.get("code") != 0:
        raise FeishuBitableError(
            f"获取 tenant_access_token 失败: code={data.get('code')} "
            f"msg={data.get('msg')}"
        )
    token = data["tenant_access_token"]
    expire = data.get("expire", 7200)
    _token_cache["token"] = token
    _token_cache["expire_at"] = now + expire
    logger.debug("[feishu] token 刷新, expire=%ss", expire)
    return token


def _headers() -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {_get_token()}",
        "Content-Type": "application/json; charset=utf-8",
    }


def _app_token() -> str:
    t = settings.bugtrack.feishu_app_token
    if not t:
        raise FeishuBitableError("未配置 FEISHU_APP_TOKEN")
    return t


def _table_id() -> str:
    t = settings.bugtrack.feishu_table_id
    if not t:
        raise FeishuBitableError("未配置 FEISHU_TABLE_ID")
    return t


def _check(data: Dict[str, Any], action: str) -> None:
    """飞书统一 code 检查 (code != 0 抛错)。"""
    code = data.get("code", -1)
    if code != 0:
        raise FeishuBitableError(
            f"飞书{action}失败: code={code} msg={data.get('msg')} "
            f"data={str(data.get('data',''))[:120]}"
        )


# ======================================================================
# 查表 (N2/N9)
# ======================================================================

def search_records(
    keyword: str,
    field_name: str = "操作描述",
    limit: int = 20,
) -> List[Dict[str, Any]]:
    """N2 查表: 关键词在某文本字段 contains 匹配 (服务端过滤)。

    Args:
        keyword: 关键词 (中文)
        field_name: 在哪个字段做 contains (默认"操作描述")
        limit: 最多返回条数

    Returns:
        命中记录列表, 每条含 record_id + fields。空列表表示无命中。
    """
    keyword = (keyword or "").strip()
    if not keyword:
        return []
    body: Dict[str, Any] = {
        "filter": {
            "conjunction": "and",
            "conditions": [
                {
                    "field_name": field_name,
                    "operator": "contains",
                    "value": [keyword],
                }
            ],
        },
        "page_size": min(limit, 500),
    }
    try:
        with httpx.Client(timeout=_TIMEOUT) as c:
            r = c.post(
                f"{_BASE}/bitable/v1/apps/{_app_token()}/tables/{_table_id()}/records/search",
                headers=_headers(),
                json=body,
            )
            r.raise_for_status()
            data = r.json()
        _check(data, "查表")
        items = (data.get("data") or {}).get("items") or []
        logger.info("[feishu] 关键词'%s' 命中 %d 条", keyword, len(items))
        return items
    except (FeishuBitableError, httpx.HTTPError) as e:
        logger.warning("[feishu] 查表失败: %s", e)
        return []


def get_record(record_id: str) -> Optional[Dict[str, Any]]:
    """N9 读旧行: 按 record_id 精确取单条。

    用 search 接口 (飞书无 record_id 单查的专用端点, 用 list + filter 不便,
    改用 GET /records/:record_id)。
    """
    if not record_id:
        return None
    try:
        with httpx.Client(timeout=_TIMEOUT) as c:
            r = c.get(
                f"{_BASE}/bitable/v1/apps/{_app_token()}/tables/{_table_id()}/records/{record_id}",
                headers=_headers(),
            )
            r.raise_for_status()
            data = r.json()
        _check(data, "读单条")
        return (data.get("data") or {}).get("record")
    except (FeishuBitableError, httpx.HTTPError) as e:
        logger.warning("[feishu] 读单条失败: %s", e)
        return None


# ======================================================================
# 写表 (N16/N14)
# ======================================================================

def add_record(fields: Dict[str, Any]) -> str:
    """N16 新增记录。fields key 用字段名(中文标题)。

    Returns:
        新记录的 record_id
    """
    with httpx.Client(timeout=_TIMEOUT) as c:
        r = c.post(
            f"{_BASE}/bitable/v1/apps/{_app_token()}/tables/{_table_id()}/records",
            headers=_headers(),
            json={"fields": fields},
        )
        r.raise_for_status()
        data = r.json()
    _check(data, "新增记录")
    record = (data.get("data") or {}).get("record") or {}
    rid = record.get("record_id") or record.get("id") or ""
    if not rid:
        raise FeishuBitableError("新增记录未返回 record_id")
    logger.info("[feishu] 新增记录成功 record_id=%s", rid)
    return rid


def update_record(record_id: str, fields: Dict[str, Any]) -> None:
    """N14 修改记录 (增量更新, 只改传入字段)。fields key 用字段名。

    Args:
        record_id: 要修改的记录 id (来自 add 返回或 search)
        fields: 要更新的字段 (未传字段保持不变, 置空传 null)
    """
    if not record_id:
        raise FeishuBitableError("update_record 需要 record_id")
    with httpx.Client(timeout=_TIMEOUT) as c:
        r = c.put(
            f"{_BASE}/bitable/v1/apps/{_app_token()}/tables/{_table_id()}/records/{record_id}",
            headers=_headers(),
            json={"fields": fields},
        )
        r.raise_for_status()
        data = r.json()
    _check(data, "修改记录")
    logger.info("[feishu] 修改记录成功 record_id=%s", record_id)


# ======================================================================
# 附件上传 (附件字段 type=17 用)
# ======================================================================

def upload_attachment(
    content: bytes,
    filename: str,
    content_type: str = "image/jpeg",
) -> str:
    """上传附件到飞书 (多维表格图片), 返回 ``file_token``。

    附件字段 (type=17) 的值格式为 ``[{"file_token": "..."}]``, 需先调用本接口
    拿到 file_token 再写记录。

    端点: ``POST /drive/v1/medias/upload_all`` (multipart/form-data)
    - ``parent_type`` = ``bitable_image``
    - ``parent_node`` = 多维表格 ``app_token``
    - ``size`` = 文件字节数 (字符串)

    Args:
        content:      文件二进制内容
        filename:     文件名 (含扩展名, 推断类型)
        content_type: MIME (默认 image/jpeg)

    Returns:
        file_token (写入附件字段用)

    Raises:
        FeishuBitableError: 上传失败或未返回 file_token

    Note:
        需应用开通 ``drive:drive:write`` (或旧版 ``drive:file:upload``) 权限。
        multipart 上传不能带 ``Content-Type: application/json`` (httpx 自动设
        boundary), 故此处仅带 Authorization。
    """
    app_token = _app_token()
    url = f"{_BASE}/drive/v1/medias/upload_all"
    files = {
        "file_name": (None, filename),
        "parent_type": (None, "bitable_image"),
        "parent_node": (None, app_token),
        "size": (None, str(len(content))),
        "file": (filename, content, content_type or "image/jpeg"),
    }
    with httpx.Client(timeout=_TIMEOUT) as c:
        r = c.post(
            url,
            headers={"Authorization": f"Bearer {_get_token()}"},
            files=files,
        )
        r.raise_for_status()
        data = r.json()
    _check(data, "上传附件")
    file_token = (data.get("data") or {}).get("file_token") or ""
    if not file_token:
        raise FeishuBitableError(f"上传附件未返回 file_token: {str(data)[:200]}")
    logger.info(
        "[feishu] 上传附件成功 file_token=%s size=%dB", file_token, len(content)
    )
    return file_token


# ======================================================================
# 建表 / 建字段 (初始化用, 一次性)
# ======================================================================

def create_field(
    field_name: str,
    field_type: int,
    property: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """在当前配置的数据表新增字段, 返回新建字段信息。

    一次性初始化用 (如新增 "Bug截图" 附件字段 type=17)。

    Args:
        field_name: 字段名 (中文标题)
        field_type: 飞书字段类型 (1=文本, 3=单选, 5=日期, 17=附件, ...)
        property:   字段属性 (单选传 {"options":[{"name":...}]})

    Returns:
        新建字段 dict (含 field_id)
    """
    body: Dict[str, Any] = {"field_name": field_name, "type": field_type}
    if property:
        body["property"] = property
    with httpx.Client(timeout=_TIMEOUT) as c:
        r = c.post(
            f"{_BASE}/bitable/v1/apps/{_app_token()}/tables/{_table_id()}/fields",
            headers=_headers(),
            json=body,
        )
        r.raise_for_status()
        data = r.json()
    _check(data, "建字段")
    field = (data.get("data") or {}).get("field") or {}
    logger.info("[feishu] 建字段 %s type=%d field_id=%s", field_name, field_type, field.get("field_id"))
    return field

def create_app(name: str = "二阶段bug反馈表") -> Dict[str, Any]:
    """创建多维表格 (应用成所有者, 免协作者授权)。返回 {app_token, url}。

    一次性初始化用, 之后把 app_token 写入 .env。
    """
    with httpx.Client(timeout=_TIMEOUT) as c:
        r = c.post(
            f"{_BASE}/bitable/v1/apps",
            headers=_headers(),
            json={"name": name},
        )
        r.raise_for_status()
        data = r.json()
    _check(data, "创建多维表格")
    app = (data.get("data") or {}).get("app") or {}
    logger.info("[feishu] 创建多维表格 app_token=%s", app.get("app_token"))
    return app


def create_table(app_token: str, table_name: str, fields: List[Dict[str, Any]]) -> str:
    """在多维表格中建数据表 (带初始字段)。返回 table_id。

    Args:
        app_token: 多维表格 app_token
        table_name: 数据表名
        fields: 字段列表 [{field_name, type, property?}, ...]
                第一个字段为索引字段(is_primary), 需文本/数字等类型
    """
    body = {
        "table": {
            "name": table_name,
            "default_view_name": "表格视图",
            "fields": fields,
        }
    }
    with httpx.Client(timeout=_TIMEOUT) as c:
        r = c.post(
            f"{_BASE}/bitable/v1/apps/{app_token}/tables",
            headers=_headers(),
            json=body,
        )
        r.raise_for_status()
        data = r.json()
    _check(data, "建数据表")
    table_id = (data.get("data") or {}).get("table_id") or ""
    logger.info("[feishu] 建数据表 table_id=%s", table_id)
    return table_id


def list_fields(app_token: str, table_id: str) -> List[Dict[str, Any]]:
    """列出数据表所有字段 (拿 field_id + 类型)。"""
    with httpx.Client(timeout=_TIMEOUT) as c:
        r = c.get(
            f"{_BASE}/bitable/v1/apps/{app_token}/tables/{table_id}/fields",
            headers=_headers(),
        )
        r.raise_for_status()
        data = r.json()
    _check(data, "列出字段")
    return (data.get("data") or {}).get("items") or []


# ======================================================================
# 辅助: 单元格值归一
# ======================================================================

def cell_to_str(value: Any) -> str:
    """飞书字段值归一字符串 (文本数组取 text, 标量直转)。"""
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
        return str(value.get("text") or value.get("name") or value)
    return str(value)


def record_to_summary(record: Dict[str, Any]) -> Dict[str, str]:
    """记录归一成 {record_id, module, op_desc, summary, dev_status, reply, result} 便于 Dify 消费。

    dev_status(问题状态)/reply(产品回复)/result(完成结果) 是"进度"字段,
    供 Dify D4 命中汇报告知客户当前处理进度(根因3: 不再只塞 raw op_desc)。
    """
    fields = record.get("fields") or {}
    return {
        "record_id": record.get("record_id") or record.get("id") or "",
        "module": cell_to_str(fields.get("模块/功能点")),
        "op_desc": cell_to_str(fields.get("操作描述")),
        "summary": cell_to_str(fields.get("产品备注")),
        "dev_status": cell_to_str(fields.get("问题状态")),
        "reply": cell_to_str(fields.get("产品回复")),
        "result": cell_to_str(fields.get("完成结果")),
    }


__all__ = [
    "FeishuBitableError",
    "search_records",
    "get_record",
    "add_record",
    "update_record",
    "upload_attachment",
    "create_app",
    "create_table",
    "create_field",
    "list_fields",
    "cell_to_str",
    "record_to_summary",
]
