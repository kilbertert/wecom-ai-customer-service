"""Dify chatflow 调 wecom-ai 的二阶段 bug 反馈内部端点。

由 Dify 工作流的 HTTP 请求节点调用 (N2 查表 / N9 读旧行)。后端持企微 corp_secret
换取 access_token 查询智能表格 (webhook 不支持查询, 见决策点3)。

鉴权: Bearer token, 共享密钥 ``BUGTRACK_INTERNAL_TOKEN`` (Dify HTTP 节点 header
携带)。未配置 token 时仅记 warning 不强制 (开发期便利, 生产必配)。

端点:
    POST /internal/bugtrack/search    — N2 关键词查表, 返回命中行
    GET  /internal/bugtrack/record/{record_id} — N9 读旧行
    POST /internal/bugtrack/add       — N16 新增记录 (写飞书主表), 返回 record_id
    POST /internal/bugtrack/update    — N14 修改记录 (增量更新飞书主表)
    GET  /internal/bugtrack/health    — 健康检查
"""
import asyncio
import logging
from typing import Any, Dict, List

from fastapi import APIRouter, Request, HTTPException, Header
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.core.config import settings
from app.services.feishu_bitable import (
    FeishuBitableError,
    add_record as feishu_add_record,
    update_record as feishu_update_record,
)
from app.services.smartsheet_query_service import SmartSheetQueryService
from app.services.wechat import WeChatService

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/internal/bugtrack", tags=["bugtrack-internal"])


def _verify_token(authorization: str) -> None:
    """校验 Bearer token。未配置 BUGTRACK_INTERNAL_TOKEN 时放行 (开发期)。"""
    expected = getattr(settings.bugtrack, "internal_token", "") or ""
    if not expected:
        logger.warning(
            "[bugtrack/internal] BUGTRACK_INTERNAL_TOKEN 未配置, 跳过鉴权 "
            "(生产环境务必配置)"
        )
        return
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    token = authorization.removeprefix("Bearer ").strip()
    if token != expected:
        raise HTTPException(status_code=403, detail="invalid token")


class SearchRequest(BaseModel):
    keyword: str = Field(..., description="客户反馈关键词 (建议 LLM 提取核心词)")
    limit: int = Field(20, description="最多返回条数")


@router.post("/search")
async def search_bugtrack(
    req: SearchRequest,
    authorization: str = Header(None),
):
    """N2 查表: 关键词在主表 (操作描述) 字段 CONTAINS 匹配, 返回命中行。

    Body: ``{"keyword": "...", "limit": 20}``
    Response: ``{"success": true, "hits": [{record_id, module, op_desc, summary}, ...]}``
    """
    _verify_token(authorization or "")
    if not settings.bugtrack.enabled:
        return JSONResponse(
            content={"success": True, "hits": [], "enabled": False}
        )

    wechat_svc = WeChatService()
    try:
        svc = SmartSheetQueryService(wechat_svc)
        records = await svc.search_by_keyword(req.keyword, limit=req.limit)
        hits = [svc.record_to_summary(r) for r in records]
        return JSONResponse(
            content={"success": True, "hits": hits, "count": len(hits)}
        )
    except Exception as e:
        logger.error("[bugtrack/internal/search] failed: %s", e, exc_info=True)
        return JSONResponse(
            status_code=500,
            content={"success": False, "error": str(e), "hits": []},
        )
    finally:
        await wechat_svc.close()


@router.get("/record/{record_id}")
async def get_record(record_id: str, authorization: str = Header(None)):
    """N9 读旧行: 按 record_id 精确取单条, 返回内容快照供差异对比。

    Response: ``{"success": true, "record": {record_id, module, op_desc, summary}}``
    未找到: ``{"success": true, "record": null}``
    """
    _verify_token(authorization or "")
    if not settings.bugtrack.enabled:
        return JSONResponse(
            content={"success": True, "record": None, "enabled": False}
        )

    wechat_svc = WeChatService()
    try:
        svc = SmartSheetQueryService(wechat_svc)
        record = await svc.get_record(record_id)
        if record is None:
            return JSONResponse(
                content={"success": True, "record": None}
            )
        return JSONResponse(
            content={"success": True, "record": svc.record_to_summary(record)}
        )
    except Exception as e:
        logger.error(
            "[bugtrack/internal/record/%s] failed: %s", record_id, e,
            exc_info=True,
        )
        return JSONResponse(
            status_code=500,
            content={"success": False, "error": str(e), "record": None},
        )
    finally:
        await wechat_svc.close()


class AddRecordRequest(BaseModel):
    fields: Dict[str, Any] = Field(
        ..., description="飞书字段名(中文标题)→值;单选字段传选项名字符串"
    )


class UpdateRecordRequest(BaseModel):
    record_id: str = Field(..., description="要修改的记录 id (来自 add 返回或 search)")
    fields: Dict[str, Any] = Field(..., description="要增量更新的字段(未传保持不变)")


@router.post("/add")
async def add_record_endpoint(
    req: AddRecordRequest,
    authorization: str = Header(None),
):
    """N16 新增记录: 写飞书主表, 返回新 record_id (存 cv_record_id)。

    Body: ``{"fields": {"模块/功能点": "...", "操作描述": "...", "类型": "bug", ...}}``
    Response: ``{"success": true, "record_id": "recXXXX"}``
    """
    _verify_token(authorization or "")
    if not settings.bugtrack.enabled:
        return JSONResponse(
            content={"success": False, "error": "bugtrack disabled", "record_id": ""}
        )
    try:
        record_id = await asyncio.to_thread(feishu_add_record, req.fields)
        return JSONResponse(content={"success": True, "record_id": record_id})
    except FeishuBitableError as e:
        logger.error("[bugtrack/internal/add] failed: %s", e, exc_info=True)
        return JSONResponse(
            status_code=500,
            content={"success": False, "error": str(e), "record_id": ""},
        )
    except Exception as e:
        logger.error("[bugtrack/internal/add] unexpected: %s", e, exc_info=True)
        return JSONResponse(
            status_code=500,
            content={"success": False, "error": str(e), "record_id": ""},
        )


@router.post("/update")
async def update_record_endpoint(
    req: UpdateRecordRequest,
    authorization: str = Header(None),
):
    """N14 修改记录: 增量更新飞书主表 (只改传入字段)。

    Body: ``{"record_id": "recXXXX", "fields": {"操作描述": "..."}}``
    Response: ``{"success": true}``
    """
    _verify_token(authorization or "")
    if not settings.bugtrack.enabled:
        return JSONResponse(content={"success": False, "error": "bugtrack disabled"})
    if not req.record_id:
        return JSONResponse(
            status_code=400,
            content={"success": False, "error": "record_id required"},
        )
    try:
        await asyncio.to_thread(feishu_update_record, req.record_id, req.fields)
        return JSONResponse(content={"success": True})
    except FeishuBitableError as e:
        logger.error("[bugtrack/internal/update] failed: %s", e, exc_info=True)
        return JSONResponse(status_code=500, content={"success": False, "error": str(e)})
    except Exception as e:
        logger.error("[bugtrack/internal/update] unexpected: %s", e, exc_info=True)
        return JSONResponse(status_code=500, content={"success": False, "error": str(e)})


@router.get("/health")
async def health():
    return {
        "status": "ok",
        "bugtrack_enabled": settings.bugtrack.enabled,
        "feishu_configured": bool(
            settings.bugtrack.feishu_app_id
            and settings.bugtrack.feishu_app_secret
            and settings.bugtrack.feishu_app_token
            and settings.bugtrack.feishu_table_id
        ),
        "feishu_app_id_set": bool(settings.bugtrack.feishu_app_id),
        "feishu_app_token_set": bool(settings.bugtrack.feishu_app_token),
        "feishu_table_id_set": bool(settings.bugtrack.feishu_table_id),
        "internal_token_set": bool(settings.bugtrack.internal_token),
    }


__all__ = ["router"]
