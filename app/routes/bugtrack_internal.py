"""Dify chatflow 调 wecom-ai 的二阶段 bug 反馈内部端点。

由 Dify 工作流的 HTTP 请求节点调用 (N2 查表 / N9 读旧行)。后端持企微 corp_secret
换取 access_token 查询智能表格 (webhook 不支持查询, 见决策点3)。

鉴权: 来源 IP 白名单 ``BUGTRACK_ALLOWED_IPS`` (逗号分隔; 生产配 127.0.0.1,::1,<dify_server_ip>)。
替代原 Bearer token (token 已从 Dify 侧移除)。未配置时仅记 warning 不强制 (开发期便利, 生产必配)。

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
from app.services.dify import (
    _conv_image_clear,
    _conv_image_get,
    fetch_upload_bytes,
)
from app.services.feishu_bitable import (
    FeishuBitableError,
    add_record as feishu_add_record,
    update_record as feishu_update_record,
    upload_attachment as feishu_upload_attachment,
)
from app.services.smartsheet_query_service import SmartSheetQueryService
from app.services.wechat import WeChatService

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/internal/bugtrack", tags=["bugtrack-internal"])


def _verify_access(request: Request) -> None:
    """校验来源 IP 白名单 (替代 Bearer token; token 已从 Dify 侧移除)。

    未配置 BUGTRACK_ALLOWED_IPS 时放行 (开发期便利, 生产必配)。
    生产应配: ``127.0.0.1,::1,<dify_server_ip>``。
    """
    raw = getattr(settings.bugtrack, "allowed_ips", "") or ""
    allowed = {ip.strip() for ip in raw.split(",") if ip.strip()}
    if not allowed:
        logger.warning(
            "[bugtrack/internal] BUGTRACK_ALLOWED_IPS 未配置, 跳过鉴权 "
            "(生产环境务必配置: 127.0.0.1,::1,<dify_server_ip>)"
        )
        return
    client_ip = (request.client.host if request.client else "") or ""
    if client_ip not in allowed:
        logger.warning(
            "[bugtrack/internal] 拒绝来源 IP: %s (allowed: %s)", client_ip, allowed
        )
        raise HTTPException(status_code=403, detail=f"ip not allowed: {client_ip}")


class SearchRequest(BaseModel):
    keyword: str = Field(..., description="客户反馈关键词 (建议 LLM 提取核心词)")
    module: str = Field(default="", description="结构化模块名，用于模块字段补充召回")
    op_desc: str = Field(default="", description="完整操作描述，用于候选相似度排序")
    limit: int = Field(20, description="最多返回条数")


@router.post("/search")
async def search_bugtrack(
    req: SearchRequest,
    request: Request,
):
    """N2 查表: 关键词/模块多字段召回并按问题描述相似度排序。

    Body: ``{"keyword": "...", "module": "...", "op_desc": "...", "limit": 20}``
    旧调用方只传 ``keyword`` 仍保持兼容。
    Response: ``{"success": true, "hits": [{record_id, module, op_desc, summary}, ...]}``
    """
    _verify_access(request)
    if not settings.bugtrack.enabled:
        return JSONResponse(
            content={"success": True, "hits": [], "enabled": False}
        )

    wechat_svc = WeChatService()
    try:
        svc = SmartSheetQueryService(wechat_svc)
        records = await svc.search_by_feedback(
            req.keyword,
            module=req.module,
            op_desc=req.op_desc,
            limit=req.limit,
        )
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
async def get_record(record_id: str, request: Request):
    """N9 读旧行: 按 record_id 精确取单条, 返回内容快照供差异对比。

    Response: ``{"success": true, "record": {record_id, module, op_desc, summary}}``
    未找到: ``{"success": true, "record": null}``
    """
    _verify_access(request)
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
    image_file_ids: List[str] = Field(
        default_factory=list,
        description="用户 bug 截图的 Dify upload_file_id 列表; 后端回取字节->"
        "上传飞书附件字段 'Bug截图'。空则不附图。",
    )
    conversation_id: str = Field(
        default="",
        description="Dify 会话 id; image_file_ids 为空时, 后端按 conv_id 从跨轮"
        "图片缓存回取本会话累积的截图 file_id (turn1 发图 turn2 写表场景)。",
    )


class UpdateRecordRequest(BaseModel):
    record_id: str = Field(..., description="要修改的记录 id (来自 add 返回或 search)")
    fields: Dict[str, Any] = Field(..., description="要增量更新的字段(未传保持不变)")


@router.post("/add")
async def add_record_endpoint(
    req: AddRecordRequest,
    request: Request,
):
    """N16 新增记录: 写飞书主表, 返回新 record_id (存 cv_record_id)。

    Body: ``{"fields": {"模块/功能点": "...", "操作描述": "...", "类型": "bug", ...}}``
    Response: ``{"success": true, "record_id": "recXXXX"}``
    """
    _verify_access(request)
    if not settings.bugtrack.enabled:
        return JSONResponse(
            content={"success": False, "error": "bugtrack disabled", "record_id": ""}
        )
    try:
        # 不可变拷贝, 不就地改入参 (coding-style)
        fields = dict(req.fields)
        # 解析要附的图片 file_id: 优先显式 image_file_ids, 否则按 conversation_id
        # 从跨轮图片缓存回取 (turn1 发图 turn2 写表场景)
        img_ids: List[str] = [i for i in req.image_file_ids if (i or "").strip()]
        used_conv_cache = False
        if not img_ids and req.conversation_id:
            img_ids = _conv_image_get(req.conversation_id)
            used_conv_cache = bool(img_ids)
        # 用户 bug 截图 -> 飞书附件字段 "Bug截图" (type=17)
        # 单张失败只记 warning 跳过, 不阻断主表写入
        if img_ids:
            tokens: List[Dict[str, str]] = []
            for fid in img_ids:
                fid = (fid or "").strip()
                if not fid:
                    continue
                try:
                    got = await fetch_upload_bytes(fid)
                    if not got:
                        logger.warning(
                            "[bugtrack/internal/add] 截图 %s 取字节失败,跳过", fid
                        )
                        continue
                    img_bytes, fname, ctype = got
                    tok = await asyncio.to_thread(
                        feishu_upload_attachment, img_bytes, fname, ctype
                    )
                    tokens.append({"file_token": tok})
                except Exception as ie:
                    logger.warning(
                        "[bugtrack/internal/add] 截图 %s 入飞书失败,跳过: %s", fid, ie
                    )
                    continue
            if tokens:
                fields["Bug截图"] = tokens
                logger.info("[bugtrack/internal/add] 附图 %d 张", len(tokens))
        record_id = await asyncio.to_thread(feishu_add_record, fields)
        # 写表成功后清空会话图片缓存, 防同会话下个 bug 复用旧图
        if used_conv_cache:
            _conv_image_clear(req.conversation_id)
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
    request: Request,
):
    """N14 修改记录: 增量更新飞书主表 (只改传入字段)。

    Body: ``{"record_id": "recXXXX", "fields": {"操作描述": "..."}}``
    Response: ``{"success": true}``
    """
    _verify_access(request)
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
        "allowed_ips_set": bool(settings.bugtrack.allowed_ips),
    }


__all__ = ["router"]
