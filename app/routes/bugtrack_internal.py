"""Internal API owned by the relational Bug feedback service.

PostgreSQL is the source of truth for drafts, turns, state transitions and
attachment ownership. Dify remains an intent/field extraction client during
the compatibility migration; Feishu is the final synchronized view.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List

from fastapi import APIRouter, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.core.config import settings
from app.core.database import session_scope, verify_database
from app.models.bugtrack_db import BugAttachment, BugOutbox
from app.services.bug_assistant_message_service import bug_assistant_message_service
from app.services.bug_assistant_orchestrator import (
    InvalidBugAssistantEvent,
    InvalidBugAssistantTransition,
    bug_assistant_orchestrator,
)
from app.services.bug_issue_status_service import (
    BugIssueStatusError,
    bug_issue_status_service,
)
from app.services.bugtrack_attachment_storage import attachment_storage
from app.services.bugtrack_service import (
    DraftIdentity,
    bugtrack_service,
    draft_to_dict,
    fields_patch_from_feishu,
)
from app.services.dify import fetch_upload_bytes
from app.services.feishu_bitable import (
    add_record as feishu_add_record,
    get_record as feishu_get_record,
    search_records as feishu_search_records,
    update_record as feishu_update_record,
    upload_attachment as feishu_upload_attachment,
)
from app.services.smartsheet_query_service import (
    EXISTING_ISSUE_SCORE_THRESHOLD,
    SmartSheetQueryService,
)
from app.services.wechat import WeChatService

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/internal/bugtrack", tags=["bugtrack-internal"])


def _verify_access(request: Request) -> None:
    raw = getattr(settings.bugtrack, "allowed_ips", "") or ""
    allowed = {ip.strip() for ip in raw.split(",") if ip.strip()}
    if not allowed:
        logger.warning("[bugtrack/internal] BUGTRACK_ALLOWED_IPS 未配置，生产环境必须配置")
        return
    client_ip = (request.client.host if request.client else "") or ""
    if client_ip not in allowed:
        logger.warning("[bugtrack/internal] 拒绝来源 IP: %s", client_ip)
        raise HTTPException(status_code=403, detail="ip not allowed")


class DraftContext(BaseModel):
    draft_id: str = ""
    conversation_id: str = ""
    session_id: str = ""
    channel: str = "dify"
    user_key: str = ""
    source_text: str = ""
    flow_state: str = ""
    idempotency_key: str = ""

    def identity(self) -> DraftIdentity:
        return DraftIdentity(
            channel=(self.channel or "dify").strip(),
            user_key=(self.user_key or "").strip(),
            session_id=(self.session_id or "").strip(),
            conversation_id=(self.conversation_id or "").strip(),
        )


class SearchRequest(DraftContext):
    keyword: str = Field(default="", description="客户反馈查重关键词")
    module: str = ""
    op_desc: str = ""
    environment: str = ""
    issue_type: str = "bug"
    limit: int = 20
    force_new: bool = False


class BugAssistantFields(BaseModel):
    module: str | None = None
    operation_description: str | None = None
    environment: str | None = None
    issue_type: str | None = None
    search_keyword: str | None = None


class BugAssistantTurnRequest(DraftContext):
    event: str
    fields: BugAssistantFields = Field(default_factory=BugAssistantFields)
    force_new: bool = False


class BugNotificationAckRequest(BaseModel):
    channel: str = "h5"
    user_key: str = ""
    session_id: str = ""
    notification_ids: list[str] = Field(default_factory=list)


class BugStatusReconcileRequest(BaseModel):
    limit: int = Field(default=100, ge=1, le=500)


@router.post("/v2/turn")
async def bug_assistant_turn(req: BugAssistantTurnRequest, request: Request):
    """Apply one deterministic v2 state-machine event."""

    _verify_access(request)
    try:
        async with session_scope() as session:
            decision = await bug_assistant_orchestrator.handle(
                session,
                event=req.event,
                identity=req.identity(),
                draft_id=req.draft_id,
                fields_patch=req.fields.model_dump(
                    exclude_none=True, exclude_unset=True
                ),
                source_text=req.source_text,
                idempotency_key=req.idempotency_key,
                force_new=req.force_new,
            )
    except InvalidBugAssistantEvent as exc:
        raise HTTPException(
            status_code=400, detail=f"unsupported event: {exc}"
        ) from exc
    except InvalidBugAssistantTransition as exc:
        raise HTTPException(
            status_code=409,
            detail={"event": exc.event, "state": exc.state},
        ) from exc
    return JSONResponse(content=decision.to_dict())


@router.post("/v2/message")
async def bug_assistant_message(
    request: Request,
    text: str = Form(""),
    session_id: str = Form(""),
    channel: str = Form("h5"),
    user_key: str = Form(""),
    language: str = Form(""),
    message_id: str = Form(""),
    source_file_id: str = Form(""),
    image: UploadFile | None = File(None),
):
    """Process one raw channel message through the active Bug v2 path."""

    _verify_access(request)
    if not settings.bugtrack.enabled:
        return JSONResponse(
            status_code=503,
            content={"success": False, "error": "bugtrack disabled"},
        )
    if not (session_id.strip() or user_key.strip()):
        return JSONResponse(
            status_code=400,
            content={"success": False, "error": "session or user identity required"},
        )

    image_bytes: bytes | None = None
    image_name = ""
    image_mime = ""
    if image is not None:
        image_bytes = await image.read(_MAX_CACHED_IMAGE_BYTES + 1)
        if not image_bytes:
            return JSONResponse(
                status_code=400,
                content={"success": False, "error": "empty file"},
            )
        if len(image_bytes) > _MAX_CACHED_IMAGE_BYTES:
            return JSONResponse(
                status_code=413,
                content={"success": False, "error": "file exceeds 10MB"},
            )
        image_mime = _file_mime(image_bytes)
        if not image_mime:
            return JSONResponse(
                status_code=400,
                content={"success": False, "error": "unsupported image"},
            )
        image_name = image.filename or "bug-screenshot.png"

    result = await bug_assistant_message_service.process(
        channel=channel,
        user_key=user_key or session_id,
        session_id=session_id or user_key,
        text=text,
        language=language,
        message_id=message_id,
        image_bytes=image_bytes,
        image_name=image_name,
        image_mime=image_mime,
        source_file_id=source_file_id,
    )
    return JSONResponse(
        status_code=202 if result.sync_pending else 200,
        content=result.to_dict(),
    )


@router.get("/v2/notifications")
async def list_bug_notifications(
    request: Request,
    channel: str = Query("h5"),
    user_key: str = Query(""),
    session_id: str = Query(""),
    limit: int = Query(20, ge=1, le=100),
):
    """Return durable unread/pending progress notifications for one subscriber."""

    _verify_access(request)
    if not (user_key.strip() or session_id.strip()):
        raise HTTPException(status_code=400, detail="subscriber identity required")
    items = await bug_issue_status_service.list_notifications(
        channel=channel,
        user_key=user_key,
        session_id=session_id,
        limit=limit,
    )
    return JSONResponse(
        content={"success": True, "notifications": [item.to_dict() for item in items]}
    )


@router.post("/v2/notifications/ack")
async def acknowledge_bug_notifications(
    req: BugNotificationAckRequest, request: Request
):
    """Acknowledge notifications only when they belong to the same subscriber."""

    _verify_access(request)
    acknowledged = await bug_issue_status_service.acknowledge(
        channel=req.channel,
        user_key=req.user_key,
        session_id=req.session_id,
        notification_ids=req.notification_ids,
    )
    return JSONResponse(content={"success": True, "acknowledged": acknowledged})


@router.post("/v2/issues/reconcile")
async def reconcile_bug_issue_statuses(
    req: BugStatusReconcileRequest, request: Request
):
    """Run one bounded Feishu progress reconciliation pass."""

    _verify_access(request)
    result = await bug_issue_status_service.reconcile(limit=req.limit)
    return JSONResponse(content={"success": True, **result.to_dict()})


@router.get("/v2/issues/{issue_id}/impact")
async def get_bug_issue_impact(issue_id: str, request: Request):
    """Expose Report and subscriber counts without modifying the Issue row."""

    _verify_access(request)
    try:
        impact = await bug_issue_status_service.issue_impact(issue_id)
    except BugIssueStatusError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return JSONResponse(content={"success": True, "issue": impact})


@router.post("/search")
async def search_bugtrack(req: SearchRequest, request: Request):
    """Persist the structured draft first, then query Feishu for duplicates."""

    _verify_access(request)
    patch = {
        "module": req.module,
        "operation_description": req.op_desc,
        "environment": req.environment,
        "issue_type": req.issue_type,
        "search_keyword": req.keyword,
    }
    async with session_scope() as session:
        draft = await bugtrack_service.ensure_draft(
            session,
            identity=req.identity(),
            draft_id=req.draft_id,
            force_new=req.force_new,
            fields_patch=patch,
            flow_state=req.flow_state or "searching",
            source_text=req.source_text,
            intent="SEARCH",
            idempotency_key=req.idempotency_key,
            event_type="search_requested",
        )
        draft_id = str(draft.id)

    if not settings.bugtrack.enabled:
        return JSONResponse(
            content={
                "success": True,
                "hits": [],
                "enabled": False,
                "draft_id": draft_id,
            }
        )

    wechat_svc = WeChatService()
    try:
        svc = SmartSheetQueryService(wechat_svc)
        records = await svc.search_by_feedback(
            req.keyword,
            module=req.module,
            op_desc=req.op_desc,
            limit=max(1, min(req.limit, 100)),
        )
        hits = []
        for record in records:
            summary = svc.record_to_summary(record)
            summary["match_score"] = round(
                svc.feedback_score(
                    record,
                    keyword=req.keyword,
                    module=req.module,
                    op_desc=req.op_desc,
                ),
                2,
            )
            summary["match_threshold"] = EXISTING_ISSUE_SCORE_THRESHOLD
            hits.append(summary)
        async with session_scope() as session:
            draft = await bugtrack_service.get_draft(session, draft_id)
            if draft is not None:
                await bugtrack_service.record_search_result(session, draft, hits)
        return JSONResponse(
            content={
                "success": True,
                "hits": hits,
                "count": len(hits),
                "draft_id": draft_id,
            }
        )
    except Exception as exc:
        logger.error("[bugtrack/search] failed: %s", exc, exc_info=True)
        return JSONResponse(
            status_code=500,
            content={
                "success": False,
                "error": str(exc),
                "hits": [],
                "draft_id": draft_id,
            },
        )
    finally:
        await wechat_svc.close()


@router.get("/record/{record_id}")
async def get_record(record_id: str, request: Request):
    _verify_access(request)
    if not settings.bugtrack.enabled:
        return JSONResponse(content={"success": True, "record": None, "enabled": False})
    wechat_svc = WeChatService()
    try:
        svc = SmartSheetQueryService(wechat_svc)
        record = await svc.get_record(record_id)
        return JSONResponse(
            content={
                "success": True,
                "record": svc.record_to_summary(record) if record is not None else None,
            }
        )
    except Exception as exc:
        logger.error("[bugtrack/record/%s] failed: %s", record_id, exc, exc_info=True)
        return JSONResponse(
            status_code=500,
            content={"success": False, "error": str(exc), "record": None},
        )
    finally:
        await wechat_svc.close()


class AddRecordRequest(DraftContext):
    fields: Dict[str, Any]
    image_file_ids: List[str] = Field(default_factory=list)


class UpdateRecordRequest(DraftContext):
    record_id: str
    fields: Dict[str, Any]


class TransitionRequest(DraftContext):
    event_type: str
    next_state: str = ""
    status: str = ""
    intent: str = ""
    force_new: bool = False
    fields: Dict[str, Any] = Field(default_factory=dict)


_MAX_CACHED_IMAGE_BYTES = 10 * 1024 * 1024


def _file_mime(content: bytes) -> str:
    if content[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if content[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if content[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return "image/webp"
    if content[:5] == b"%PDF-":
        return "application/pdf"
    return ""


@router.post("/cache-image")
async def cache_bug_image(
    request: Request,
    conversation_id: str = Form(""),
    draft_id: str = Form(""),
    session_id: str = Form(""),
    channel: str = Form("h5"),
    user_key: str = Form(""),
    source_file_id: str = Form(""),
    image: UploadFile = File(...),
):
    """Persist an attachment and bind it to the current concrete draft."""

    _verify_access(request)
    if not (conversation_id or draft_id or session_id):
        return JSONResponse(
            status_code=400,
            content={
                "success": False,
                "error": "draft or conversation identity required",
            },
        )
    content = await image.read(_MAX_CACHED_IMAGE_BYTES + 1)
    if not content:
        return JSONResponse(
            status_code=400, content={"success": False, "error": "empty file"}
        )
    if len(content) > _MAX_CACHED_IMAGE_BYTES:
        return JSONResponse(
            status_code=413, content={"success": False, "error": "file exceeds 10MB"}
        )
    mime_type = _file_mime(content)
    if not mime_type:
        return JSONResponse(
            status_code=400,
            content={"success": False, "error": "unsupported image/document format"},
        )

    identity = DraftIdentity(
        channel=channel or "h5",
        user_key=user_key,
        session_id=session_id,
        conversation_id=conversation_id,
    )
    async with session_scope() as session:
        draft = await bugtrack_service.ensure_draft(
            session,
            identity=identity,
            draft_id=draft_id,
            event_type="attachment_context_resolved",
        )
        attachment = await bugtrack_service.add_attachment(
            session,
            draft=draft,
            content=content,
            original_name=image.filename or "bug-screenshot",
            mime_type=mime_type,
            source_file_id=source_file_id,
        )
        response = {
            "success": True,
            "draft_id": str(draft.id),
            "attachment_id": str(attachment.id),
            "sha256": attachment.sha256,
        }
    logger.info(
        "[bugtrack/cache-image] draft=%s attachment=%s size=%dB",
        response["draft_id"][:8],
        response["attachment_id"][:8],
        len(content),
    )
    return JSONResponse(content=response)


async def _import_legacy_file_ids(
    *, draft_id: str, identity: DraftIdentity, file_ids: list[str]
) -> None:
    for file_id in [item.strip() for item in file_ids if item and item.strip()]:
        got = await fetch_upload_bytes(file_id)
        if not got:
            raise RuntimeError(f"attachment {file_id[:12]} unavailable")
        content, filename, content_type = got
        mime_type = _file_mime(content) or content_type or "application/octet-stream"
        async with session_scope() as session:
            draft = await bugtrack_service.ensure_draft(
                session,
                identity=identity,
                draft_id=draft_id,
                event_type="legacy_attachment_imported",
            )
            await bugtrack_service.add_attachment(
                session,
                draft=draft,
                content=content,
                original_name=filename,
                mime_type=mime_type,
                source_file_id=file_id,
            )


async def _upload_draft_attachments(draft_id: str) -> list[dict[str, str]]:
    async with session_scope() as session:
        draft = await bugtrack_service.get_draft(session, draft_id)
        if draft is None:
            raise RuntimeError("draft not found")
        attachments = await bugtrack_service.staged_attachments(session, draft.id)

    tokens: list[dict[str, str]] = []
    for attachment in attachments:
        if attachment.feishu_file_token:
            tokens.append({"file_token": attachment.feishu_file_token})
            continue
        try:
            content = await asyncio.to_thread(
                attachment_storage.read, attachment.storage_key
            )
            token = await asyncio.to_thread(
                feishu_upload_attachment,
                content,
                attachment.original_name,
                attachment.mime_type,
            )
        except Exception as exc:
            async with session_scope() as session:
                current = await session.get(BugAttachment, attachment.id)
                if current is not None:
                    current.status = "failed"
                    current.last_error = str(exc)[:2000]
            raise
        async with session_scope() as session:
            current = await session.get(BugAttachment, attachment.id)
            if current is not None:
                current.feishu_file_token = token
                current.status = "synced"
                current.last_error = ""
        tokens.append({"file_token": token})
    return tokens


async def _find_existing_record_for_draft(draft_id: str) -> str:
    records = await asyncio.to_thread(feishu_search_records, draft_id, "业务草稿ID", 2)
    if not records:
        return ""
    first = records[0]
    return str(first.get("record_id") or first.get("id") or "")


@router.post("/add")
async def add_record_endpoint(req: AddRecordRequest, request: Request):
    _verify_access(request)
    if not settings.bugtrack.enabled:
        return JSONResponse(
            content={"success": False, "error": "bugtrack disabled", "record_id": ""}
        )

    identity = req.identity()
    async with session_scope() as session:
        draft = await bugtrack_service.ensure_draft(
            session,
            identity=identity,
            draft_id=req.draft_id,
            fields_patch=fields_patch_from_feishu(req.fields),
            flow_state=req.flow_state or "submitting",
            source_text=req.source_text,
            intent="CONFIRM_NEW",
            idempotency_key=req.idempotency_key,
            event_type="submission_requested",
        )
        draft_id = str(draft.id)
        if draft.feishu_record_id:
            return JSONResponse(
                content={
                    "success": True,
                    "record_id": draft.feishu_record_id,
                    "draft_id": draft_id,
                    "idempotent": True,
                }
            )
        await bugtrack_service.prepare_outbox(
            session, draft=draft, operation="add", payload={"fields": dict(req.fields)}
        )

    try:
        if req.image_file_ids:
            await _import_legacy_file_ids(
                draft_id=draft_id, identity=identity, file_ids=req.image_file_ids
            )
        tokens = await _upload_draft_attachments(draft_id)
        fields = dict(req.fields)
        fields["业务草稿ID"] = draft_id
        if tokens:
            fields["Bug截图"] = tokens

        record_id = await _find_existing_record_for_draft(draft_id)
        if record_id:
            await asyncio.to_thread(feishu_update_record, record_id, fields)
            idempotent = True
        else:
            record_id = await asyncio.to_thread(feishu_add_record, fields)
            idempotent = False

        async with session_scope() as session:
            draft = await bugtrack_service.get_draft(session, draft_id)
            if draft is None:
                raise RuntimeError("draft disappeared after Feishu write")
            draft.feishu_record_id = record_id
            draft.status = "submitted"
            draft.flow_state = "await_modify_window"
            from app.models.bugtrack_db import utcnow

            draft.submitted_at = utcnow()
            await bugtrack_service.transition(
                session,
                draft,
                event_type="submission_succeeded",
                flow_state="await_modify_window",
                status="submitted",
                data={"record_id": record_id, "attachment_count": len(tokens)},
            )
            outbox = (
                await session.execute(
                    select(BugOutbox).where(
                        BugOutbox.idempotency_key == f"add:{draft.id}"
                    )
                )
            ).scalar_one()
            await bugtrack_service.complete_outbox(session, outbox=outbox, success=True)
        return JSONResponse(
            content={
                "success": True,
                "record_id": record_id,
                "draft_id": draft_id,
                "attachment_count": len(tokens),
                "idempotent": idempotent,
            }
        )
    except Exception as exc:
        logger.error("[bugtrack/add] failed draft=%s: %s", draft_id, exc, exc_info=True)
        async with session_scope() as session:
            outbox = (
                await session.execute(
                    select(BugOutbox).where(
                        BugOutbox.idempotency_key == f"add:{draft_id}"
                    )
                )
            ).scalar_one_or_none()
            if outbox is not None:
                await bugtrack_service.complete_outbox(
                    session, outbox=outbox, success=False, error=str(exc)
                )
        return JSONResponse(
            status_code=502,
            content={
                "success": False,
                "error": str(exc),
                "record_id": "",
                "draft_id": draft_id,
            },
        )


@router.post("/update")
async def update_record_endpoint(req: UpdateRecordRequest, request: Request):
    _verify_access(request)
    if not settings.bugtrack.enabled:
        return JSONResponse(content={"success": False, "error": "bugtrack disabled"})

    identity = req.identity()
    async with session_scope() as session:
        draft = await bugtrack_service.ensure_draft(
            session,
            identity=identity,
            draft_id=req.draft_id,
            fields_patch=fields_patch_from_feishu(req.fields),
            flow_state=req.flow_state or "submitting_update",
            source_text=req.source_text,
            intent="CONFIRM_MODIFY",
            idempotency_key=req.idempotency_key,
            event_type="update_requested",
        )
        draft_id = str(draft.id)
        draft.feishu_record_id = req.record_id
        await bugtrack_service.prepare_outbox(
            session,
            draft=draft,
            operation="update",
            payload={"record_id": req.record_id, "fields": dict(req.fields)},
        )

    try:
        tokens = await _upload_draft_attachments(draft_id)
        fields = dict(req.fields)
        fields["业务草稿ID"] = draft_id
        if tokens:
            old = await asyncio.to_thread(feishu_get_record, req.record_id)
            existing = ((old or {}).get("fields") or {}).get("Bug截图") or []
            existing_tokens = [item for item in existing if isinstance(item, dict)]
            seen = {str(item.get("file_token") or "") for item in existing_tokens}
            fields["Bug截图"] = existing_tokens + [
                item for item in tokens if item["file_token"] not in seen
            ]
        await asyncio.to_thread(feishu_update_record, req.record_id, fields)
        async with session_scope() as session:
            draft = await bugtrack_service.get_draft(session, draft_id)
            if draft is not None:
                draft.feishu_record_id = req.record_id
                draft.status = "submitted"
                await bugtrack_service.transition(
                    session,
                    draft,
                    event_type="update_succeeded",
                    flow_state="await_modify_window",
                    status="submitted",
                    data={"record_id": req.record_id},
                )
            outbox = (
                await session.execute(
                    select(BugOutbox).where(
                        BugOutbox.idempotency_key == f"update:{draft_id}"
                    )
                )
            ).scalar_one()
            await bugtrack_service.complete_outbox(session, outbox=outbox, success=True)
        return JSONResponse(content={"success": True, "draft_id": draft_id})
    except Exception as exc:
        logger.error(
            "[bugtrack/update] failed draft=%s: %s", draft_id, exc, exc_info=True
        )
        async with session_scope() as session:
            outbox = (
                await session.execute(
                    select(BugOutbox).where(
                        BugOutbox.idempotency_key == f"update:{draft_id}"
                    )
                )
            ).scalar_one_or_none()
            if outbox is not None:
                await bugtrack_service.complete_outbox(
                    session, outbox=outbox, success=False, error=str(exc)
                )
        return JSONResponse(
            status_code=502,
            content={"success": False, "error": str(exc), "draft_id": draft_id},
        )


@router.post("/transition")
async def transition_draft(req: TransitionRequest, request: Request):
    _verify_access(request)
    patch = fields_patch_from_feishu(req.fields)
    async with session_scope() as session:
        draft = await bugtrack_service.ensure_draft(
            session,
            identity=req.identity(),
            draft_id=req.draft_id,
            force_new=req.force_new,
            fields_patch=patch,
            source_text=req.source_text,
            intent=req.intent,
            idempotency_key=req.idempotency_key,
            event_type=req.event_type,
        )
        await bugtrack_service.transition(
            session,
            draft,
            event_type=req.event_type,
            flow_state=req.next_state or draft.flow_state,
            status=req.status,
            actor="dify",
        )
        body = draft_to_dict(draft)
    return JSONResponse(content={"success": True, **body})


@router.get("/draft/{draft_id}")
async def get_draft(draft_id: str, request: Request):
    _verify_access(request)
    async with session_scope() as session:
        draft = await bugtrack_service.get_draft(
            session, draft_id, include_attachments=True
        )
        if draft is None:
            return JSONResponse(
                status_code=404, content={"success": False, "error": "not found"}
            )
        body = draft_to_dict(draft, include_attachments=True)
    return JSONResponse(content={"success": True, "draft": body})


class RouteSessionRequest(BaseModel):
    active: str = "A"
    conv_a: str = ""
    conv_b: str = ""
    route_data: Dict[str, Any] = Field(default_factory=dict)


@router.get("/route-session/{channel}/{session_id}")
async def get_route_session(channel: str, session_id: str, request: Request):
    _verify_access(request)
    async with session_scope() as session:
        route = await bugtrack_service.get_route_session(
            session, channel=channel, session_id=session_id
        )
        if route is None:
            return JSONResponse(content={"success": True, "route": None})
        body = {
            "active": route.active_app,
            "conv_a": route.conv_a,
            "conv_b": route.conv_b,
            **(route.route_data or {}),
        }
    return JSONResponse(content={"success": True, "route": body})


@router.put("/route-session/{channel}/{session_id}")
async def put_route_session(
    channel: str,
    session_id: str,
    req: RouteSessionRequest,
    request: Request,
):
    _verify_access(request)
    async with session_scope() as session:
        route = await bugtrack_service.put_route_session(
            session,
            channel=channel,
            session_id=session_id,
            active_app=req.active,
            conv_a=req.conv_a,
            conv_b=req.conv_b,
            route_data=req.route_data,
        )
    return JSONResponse(content={"success": True, "version": route.version})


@router.get("/health")
async def bugtrack_health(request: Request):
    _verify_access(request)
    database_ok = True
    database_error = ""
    try:
        await verify_database()
    except Exception as exc:
        database_ok = False
        database_error = str(exc)[:200]
    return {
        "ok": bool(settings.bugtrack.enabled and database_ok),
        "bugtrack_enabled": settings.bugtrack.enabled,
        "database_ok": database_ok,
        "database_error": database_error,
        "attachment_root_set": bool(settings.bugtrack.attachment_root),
        "feishu_configured": bool(
            settings.bugtrack.feishu_app_id
            and settings.bugtrack.feishu_app_secret
            and settings.bugtrack.feishu_app_token
            and settings.bugtrack.feishu_table_id
        ),
        "allowed_ips_set": bool(settings.bugtrack.allowed_ips),
    }
