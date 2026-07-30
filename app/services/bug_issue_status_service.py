"""Issue progress reconciliation and durable subscriber notifications."""

from __future__ import annotations

from dataclasses import dataclass, field
import logging
from typing import Any, Optional
import uuid

from sqlalchemy import func, select

from app.core.database import session_scope
from app.models.bugtrack_db import (
    BugIssue,
    BugIssueStatusEvent,
    BugNotificationDelivery,
    BugReport,
    BugSubscription,
    utcnow,
)
from app.services.smartsheet_query_service import SmartSheetQueryService
from app.services.wechat import WeChatService


logger = logging.getLogger(__name__)

_PUSH_CHANNELS = {"wecom_kf"}
_VISIBLE_DELIVERY_STATES = {"available", "pending", "sent"}


class BugIssueStatusError(RuntimeError):
    pass


@dataclass(frozen=True)
class BugIssueReconcileResult:
    checked: int = 0
    changed: int = 0
    skipped: int = 0
    failed: int = 0
    delivery_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "checked": self.checked,
            "changed": self.changed,
            "skipped": self.skipped,
            "failed": self.failed,
            "delivery_ids": list(self.delivery_ids),
        }


@dataclass(frozen=True)
class BugNotificationItem:
    notification_id: str
    issue_id: str
    event_id: str
    message: str
    status: str
    created_at: str
    payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "notification_id": self.notification_id,
            "issue_id": self.issue_id,
            "event_id": self.event_id,
            "message": self.message,
            "status": self.status,
            "created_at": self.created_at,
            "payload": dict(self.payload),
        }


def _progress_from_issue(issue: BugIssue) -> dict[str, str]:
    snapshot = dict(issue.external_snapshot or {})
    progress = snapshot.get("progress")
    if isinstance(progress, dict):
        return {
            "status": str(progress.get("status") or ""),
            "reply": str(progress.get("reply") or ""),
            "result": str(progress.get("result") or ""),
        }
    return {
        "status": str(snapshot.get("status") or ""),
        "reply": str(snapshot.get("reply") or ""),
        "result": str(snapshot.get("result") or ""),
    }


def _progress_from_summary(summary: dict[str, Any]) -> dict[str, str]:
    return {
        "status": str(summary.get("dev_status") or "").strip(),
        "reply": str(summary.get("reply") or "").strip(),
        "result": str(summary.get("result") or "").strip(),
    }


def _has_progress(progress: dict[str, str]) -> bool:
    return any(value.strip() for value in progress.values())


def _notification_message(issue: BugIssue, progress: dict[str, str]) -> str:
    lines = [f"您订阅的问题有新进展：{issue.title or issue.external_record_id}"]
    if progress["status"]:
        lines.append(f"当前状态：{progress['status']}")
    if progress["reply"]:
        lines.append(f"产品回复：{progress['reply']}")
    if progress["result"]:
        lines.append(f"完成结果：{progress['result']}")
    return "\n".join(lines)


def _subscribed_issue_query(limit: int):
    return (
        select(BugIssue.id, BugIssue.external_record_id)
        .join(BugSubscription, BugSubscription.issue_id == BugIssue.id)
        .where(
            BugIssue.external_record_id.is_not(None),
            BugIssue.external_record_id != "",
            BugSubscription.status == "active",
        )
        .distinct()
        .order_by(BugIssue.id)
        .limit(max(1, min(limit, 500)))
    )


class BugIssueStatusService:
    def __init__(self, candidate_service=None) -> None:
        self._candidate_service = candidate_service or SmartSheetQueryService()

    async def reconcile(self, *, limit: int = 100) -> BugIssueReconcileResult:
        async with session_scope() as session:
            issue_rows = (await session.execute(_subscribed_issue_query(limit))).all()

        checked = changed = skipped = failed = 0
        delivery_ids: list[str] = []
        for issue_id, record_id in issue_rows:
            checked += 1
            try:
                record = await self._candidate_service.get_record(str(record_id))
                if not record:
                    failed += 1
                    continue
                summary = self._candidate_service.record_to_summary(record)
                result = await self._apply_observation(
                    issue_id=issue_id,
                    record_id=str(record_id),
                    summary=summary,
                )
            except Exception as exc:
                failed += 1
                logger.warning(
                    "[bug-status] reconcile failed issue=%s record=%s error=%s",
                    issue_id,
                    record_id,
                    str(exc)[:200],
                )
                continue
            if result is None:
                skipped += 1
                continue
            changed += 1
            delivery_ids.extend(result)

        return BugIssueReconcileResult(
            checked=checked,
            changed=changed,
            skipped=skipped,
            failed=failed,
            delivery_ids=delivery_ids,
        )

    async def _apply_observation(
        self,
        *,
        issue_id: uuid.UUID,
        record_id: str,
        summary: dict[str, Any],
    ) -> Optional[list[str]]:
        observed = _progress_from_summary(summary)
        if not _has_progress(observed):
            return None
        async with session_scope() as session:
            issue = (
                await session.execute(
                    select(BugIssue).where(BugIssue.id == issue_id).with_for_update()
                )
            ).scalar_one_or_none()
            if issue is None:
                return None

            previous = _progress_from_issue(issue)
            snapshot = dict(issue.external_snapshot or {})
            snapshot.update(
                {
                    "record_id": record_id,
                    "module": str(summary.get("module") or issue.module),
                    "operation_description": str(
                        summary.get("op_desc") or issue.normalized_description
                    ),
                    "progress": observed,
                }
            )
            issue.external_snapshot = snapshot
            if observed["status"]:
                issue.status = observed["status"]

            if not _has_progress(previous) or previous == observed:
                return None

            event = BugIssueStatusEvent(
                issue_id=issue.id,
                source_system=issue.source_system or "feishu",
                previous_status=previous["status"],
                new_status=observed["status"],
                summary=_notification_message(issue, observed),
                event_snapshot={"previous": previous, "current": observed},
            )
            session.add(event)
            await session.flush()

            subscriptions = list(
                (
                    await session.execute(
                        select(BugSubscription).where(
                            BugSubscription.issue_id == issue.id,
                            BugSubscription.status == "active",
                        )
                    )
                ).scalars()
            )
            delivery_ids: list[str] = []
            for subscription in subscriptions:
                delivery = BugNotificationDelivery(
                    subscription_id=subscription.id,
                    status_event_id=event.id,
                    channel=subscription.channel,
                    recipient_key=subscription.user_key or subscription.subscriber_key,
                    session_id=subscription.session_id,
                    status=(
                        "pending"
                        if subscription.channel in _PUSH_CHANNELS
                        else "available"
                    ),
                    payload={
                        "issue_id": str(issue.id),
                        "external_record_id": issue.external_record_id or "",
                        "title": issue.title,
                        "module": issue.module,
                        "progress": observed,
                        "message": event.summary,
                    },
                )
                session.add(delivery)
                await session.flush()
                if delivery.status == "pending":
                    delivery_ids.append(str(delivery.id))
            return delivery_ids

    async def deliver(
        self,
        delivery_id: str,
        *,
        wechat_service: Optional[WeChatService] = None,
    ) -> dict[str, Any]:
        try:
            delivery_uuid = uuid.UUID(str(delivery_id))
        except ValueError as exc:
            raise BugIssueStatusError("invalid delivery id") from exc

        async with session_scope() as session:
            delivery = (
                await session.execute(
                    select(BugNotificationDelivery)
                    .where(BugNotificationDelivery.id == delivery_uuid)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if delivery is None:
                raise BugIssueStatusError("delivery not found")
            if delivery.status in {"sent", "read"}:
                return {"delivery_id": delivery_id, "status": delivery.status}
            if delivery.channel not in _PUSH_CHANNELS:
                delivery.status = "available"
                return {"delivery_id": delivery_id, "status": "available"}
            delivery.attempts += 1
            delivery.status = "processing"
            delivery.last_error = ""
            recipient_key = delivery.recipient_key
            session_id = delivery.session_id
            message = str((delivery.payload or {}).get("message") or "")

        open_kfid = session_id.split(":", 1)[1] if ":" in session_id else ""
        if not recipient_key or not open_kfid or not message:
            error = "notification recipient context is incomplete"
            await self._mark_delivery_failed(delivery_uuid, error)
            raise BugIssueStatusError(error)

        service = wechat_service or WeChatService()
        try:
            await service.send_message_simple(recipient_key, open_kfid, message)
        except Exception as exc:
            await self._mark_delivery_failed(delivery_uuid, str(exc))
            raise BugIssueStatusError(str(exc)) from exc
        finally:
            if wechat_service is None:
                await service.close()

        async with session_scope() as session:
            delivery = await session.get(BugNotificationDelivery, delivery_uuid)
            if delivery is not None:
                delivery.status = "sent"
                delivery.sent_at = delivery.sent_at or utcnow()
                delivery.last_error = ""
        return {"delivery_id": delivery_id, "status": "sent"}

    async def _mark_delivery_failed(self, delivery_id: uuid.UUID, error: str) -> None:
        async with session_scope() as session:
            delivery = await session.get(BugNotificationDelivery, delivery_id)
            if delivery is not None:
                delivery.status = "pending"
                delivery.last_error = error[:2000]

    async def list_notifications(
        self,
        *,
        channel: str,
        user_key: str,
        session_id: str,
        limit: int = 20,
    ) -> list[BugNotificationItem]:
        recipient = (user_key or session_id).strip()
        if not recipient:
            return []
        async with session_scope() as session:
            rows = list(
                (
                    await session.execute(
                        select(BugNotificationDelivery, BugIssueStatusEvent)
                        .join(
                            BugIssueStatusEvent,
                            BugIssueStatusEvent.id
                            == BugNotificationDelivery.status_event_id,
                        )
                        .where(
                            BugNotificationDelivery.channel == channel.strip(),
                            BugNotificationDelivery.recipient_key == recipient,
                            BugNotificationDelivery.status.in_(
                                _VISIBLE_DELIVERY_STATES
                            ),
                        )
                        .order_by(BugNotificationDelivery.created_at)
                        .limit(max(1, min(limit, 100)))
                    )
                ).all()
            )
        items: list[BugNotificationItem] = []
        for delivery, event in rows:
            payload = dict(delivery.payload or {})
            items.append(
                BugNotificationItem(
                    notification_id=str(delivery.id),
                    issue_id=str(event.issue_id),
                    event_id=str(event.id),
                    message=str(payload.get("message") or event.summary),
                    status=delivery.status,
                    created_at=delivery.created_at.isoformat(),
                    payload=payload,
                )
            )
        return items

    async def acknowledge(
        self,
        *,
        channel: str,
        user_key: str,
        session_id: str,
        notification_ids: list[str],
    ) -> int:
        recipient = (user_key or session_id).strip()
        parsed_ids: list[uuid.UUID] = []
        for value in notification_ids:
            try:
                parsed_ids.append(uuid.UUID(str(value)))
            except ValueError:
                continue
        if not recipient or not parsed_ids:
            return 0
        async with session_scope() as session:
            deliveries = list(
                (
                    await session.execute(
                        select(BugNotificationDelivery).where(
                            BugNotificationDelivery.id.in_(parsed_ids),
                            BugNotificationDelivery.channel == channel.strip(),
                            BugNotificationDelivery.recipient_key == recipient,
                        )
                    )
                ).scalars()
            )
            now = utcnow()
            for delivery in deliveries:
                delivery.status = "read"
                delivery.sent_at = delivery.sent_at or now
                delivery.read_at = delivery.read_at or now
            return len(deliveries)

    async def issue_impact(self, issue_id: str) -> dict[str, Any]:
        try:
            issue_uuid = uuid.UUID(str(issue_id))
        except ValueError:
            issue_uuid = None
        async with session_scope() as session:
            if issue_uuid is not None:
                issue = await session.get(BugIssue, issue_uuid)
            else:
                issue = (
                    await session.execute(
                        select(BugIssue).where(
                            BugIssue.external_record_id == str(issue_id).strip()
                        )
                    )
                ).scalar_one_or_none()
            if issue is None:
                raise BugIssueStatusError("issue not found")
            report_count = int(
                (
                    await session.execute(
                        select(func.count(BugReport.id)).where(
                            BugReport.issue_id == issue.id
                        )
                    )
                ).scalar_one()
            )
            subscriber_count = int(
                (
                    await session.execute(
                        select(func.count(BugSubscription.id)).where(
                            BugSubscription.issue_id == issue.id,
                            BugSubscription.status == "active",
                        )
                    )
                ).scalar_one()
            )
        return {
            "issue_id": str(issue.id),
            "external_record_id": issue.external_record_id or "",
            "title": issue.title,
            "module": issue.module,
            "status": issue.status,
            "report_count": report_count,
            "subscriber_count": subscriber_count,
            "progress": _progress_from_issue(issue),
        }


bug_issue_status_service = BugIssueStatusService()


__all__ = [
    "BugIssueReconcileResult",
    "BugIssueStatusError",
    "BugIssueStatusService",
    "BugNotificationItem",
    "_subscribed_issue_query",
    "bug_issue_status_service",
]
