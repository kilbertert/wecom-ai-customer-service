"""Idempotent external synchronization for queued Bug v2 issues."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
from typing import Any
import uuid

from sqlalchemy import select

from app.core.database import session_scope
from app.models.bugtrack_db import (
    BugAttachment,
    BugDraft,
    BugIssue,
    BugOutbox,
    BugReport,
    utcnow,
)
from app.services.bugtrack_attachment_storage import attachment_storage
from app.services.bugtrack_service import bugtrack_service
from app.services.feishu_bitable import (
    add_record as feishu_add_record,
    search_records as feishu_search_records,
    update_record as feishu_update_record,
    upload_attachment as feishu_upload_attachment,
)


logger = logging.getLogger(__name__)


class BugIssueSyncError(RuntimeError):
    pass


@dataclass(frozen=True)
class BugIssueSyncResult:
    draft_id: str
    issue_id: str
    report_id: str
    record_id: str
    attachment_count: int
    idempotent: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "draft_id": self.draft_id,
            "issue_id": self.issue_id,
            "report_id": self.report_id,
            "record_id": self.record_id,
            "attachment_count": self.attachment_count,
            "idempotent": self.idempotent,
        }


class BugIssueSyncService:
    async def sync(self, draft_id: str) -> BugIssueSyncResult:
        try:
            draft_uuid = uuid.UUID(str(draft_id))
        except ValueError as exc:
            raise BugIssueSyncError("invalid draft id") from exc

        failure: Exception | None = None
        result: BugIssueSyncResult | None = None
        async with session_scope() as session:
            draft = (
                await session.execute(
                    select(BugDraft).where(BugDraft.id == draft_uuid).with_for_update()
                )
            ).scalar_one_or_none()
            if draft is None:
                raise BugIssueSyncError("draft not found")

            report = (
                await session.execute(
                    select(BugReport).where(BugReport.draft_id == draft.id)
                )
            ).scalar_one_or_none()
            if report is None or report.issue_id is None:
                raise BugIssueSyncError("queued report not found")
            issue = await session.get(BugIssue, report.issue_id)
            if issue is None:
                raise BugIssueSyncError("queued issue not found")

            outbox = (
                await session.execute(
                    select(BugOutbox)
                    .where(BugOutbox.idempotency_key == f"create_issue_v2:{draft.id}")
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if outbox is None:
                raise BugIssueSyncError("submission outbox not found")

            if (
                outbox.status == "succeeded"
                and issue.external_record_id
                and report.external_record_id
            ):
                return BugIssueSyncResult(
                    draft_id=str(draft.id),
                    issue_id=str(issue.id),
                    report_id=str(report.id),
                    record_id=issue.external_record_id,
                    attachment_count=await self._attachment_count(session, draft.id),
                    idempotent=True,
                )

            if outbox.status == "pending":
                outbox.attempts += 1
            outbox.status = "processing"
            outbox.last_error = ""

            attachments = list(
                (
                    await session.execute(
                        select(BugAttachment)
                        .where(BugAttachment.draft_id == draft.id)
                        .order_by(BugAttachment.created_at)
                    )
                ).scalars()
            )
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
                    attachment.status = "failed"
                    attachment.last_error = str(exc)[:2000]
                    failure = exc
                    break
                attachment.feishu_file_token = token
                attachment.status = "synced"
                attachment.last_error = ""
                attachment.synced_at = utcnow()
                tokens.append({"file_token": token})

            record_id = ""
            idempotent = False
            fields = {
                "模块/功能点": draft.module,
                "操作描述": draft.operation_description,
                "环境": draft.environment,
                "类型": draft.issue_type or "bug",
                "业务草稿ID": str(draft.id),
            }
            if tokens:
                fields["Bug截图"] = tokens

            if failure is None:
                try:
                    records = await asyncio.to_thread(
                        feishu_search_records,
                        str(draft.id),
                        "业务草稿ID",
                        2,
                    )
                    if records:
                        first = records[0]
                        record_id = str(first.get("record_id") or first.get("id") or "")
                    if record_id:
                        await asyncio.to_thread(feishu_update_record, record_id, fields)
                        idempotent = True
                    else:
                        record_id = await asyncio.to_thread(feishu_add_record, fields)
                except Exception as exc:
                    failure = exc

            if failure is not None:
                outbox.status = "pending"
                outbox.last_error = str(failure)[:2000]
                await bugtrack_service.transition(
                    session,
                    draft,
                    event_type="assistant_submission_sync_failed",
                    flow_state="queued_for_submission",
                    status="active",
                    actor="bug_assistant_v2",
                    data={"error": str(failure)[:500]},
                )
            else:
                issue.external_record_id = record_id
                issue.external_snapshot = {
                    "record_id": record_id,
                    "fields": fields,
                }
                issue.status = "submitted"
                report.external_record_id = record_id
                report.status = "submitted"
                report.submitted_at = report.submitted_at or utcnow()
                draft.feishu_record_id = record_id
                draft.submitted_at = draft.submitted_at or utcnow()
                await bugtrack_service.transition(
                    session,
                    draft,
                    event_type="assistant_submission_succeeded",
                    flow_state="submitted",
                    status="submitted",
                    actor="bug_assistant_v2",
                    data={
                        "record_id": record_id,
                        "attachment_count": len(tokens),
                    },
                )
                await bugtrack_service.complete_outbox(
                    session, outbox=outbox, success=True
                )
                result = BugIssueSyncResult(
                    draft_id=str(draft.id),
                    issue_id=str(issue.id),
                    report_id=str(report.id),
                    record_id=record_id,
                    attachment_count=len(tokens),
                    idempotent=idempotent,
                )

        if failure is not None:
            raise BugIssueSyncError(str(failure)) from failure
        if result is None:
            raise BugIssueSyncError("submission result missing")
        logger.info(
            "[bug-assistant-v2] synchronized draft=%s record=%s attachments=%d",
            result.draft_id,
            result.record_id,
            result.attachment_count,
        )
        return result

    @staticmethod
    async def _attachment_count(session, draft_id: uuid.UUID) -> int:
        rows = (
            await session.execute(
                select(BugAttachment.id).where(BugAttachment.draft_id == draft_id)
            )
        ).all()
        return len(rows)


bug_issue_sync_service = BugIssueSyncService()


__all__ = [
    "BugIssueSyncError",
    "BugIssueSyncResult",
    "BugIssueSyncService",
    "bug_issue_sync_service",
]
