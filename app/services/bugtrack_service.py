"""Transactional Bug draft service and relational state machine primitives."""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional

from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.models.bugtrack_db import (
    BugAttachment,
    BugConversationBinding,
    BugDraft,
    BugOutbox,
    BugRouteSession,
    BugStateEvent,
    BugTurn,
)
from app.services.bugtrack_attachment_storage import attachment_storage


TERMINAL_STATUSES = {"submitted", "abandoned", "expired", "superseded", "escalated"}


@dataclass(frozen=True)
class DraftIdentity:
    channel: str = "dify"
    user_key: str = ""
    session_id: str = ""
    conversation_id: str = ""

    def bindings(self) -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        if self.conversation_id:
            out.append(("dify_conversation", self.conversation_id.strip()))
        if self.session_id:
            out.append(
                (f"{self.channel or 'unknown'}_session", self.session_id.strip())
            )
        if self.user_key:
            out.append((f"{self.channel or 'unknown'}_user", self.user_key.strip()))
        return [(namespace, key) for namespace, key in out if key]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _is_expired(value: Optional[datetime]) -> bool:
    if value is None:
        return False
    now = _utcnow()
    if value.tzinfo is None:
        now = now.replace(tzinfo=None)
    return value <= now


def _uuid(value: str | uuid.UUID | None) -> Optional[uuid.UUID]:
    if isinstance(value, uuid.UUID):
        return value
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return uuid.UUID(raw)
    except ValueError:
        return None


def fields_patch_from_feishu(fields: dict[str, Any]) -> dict[str, str]:
    """Map Feishu field titles into the canonical draft columns."""

    data = fields or {}
    return {
        "module": str(data.get("模块/功能点") or "").strip(),
        "operation_description": str(data.get("操作描述") or "").strip(),
        "environment": str(data.get("环境") or "").strip(),
        "issue_type": str(data.get("类型") or "bug").strip(),
    }


def draft_to_dict(
    draft: BugDraft, *, include_attachments: bool = False
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "draft_id": str(draft.id),
        "status": draft.status,
        "flow_state": draft.flow_state,
        "channel": draft.channel,
        "user_key": draft.user_key,
        "session_id": draft.session_id,
        "conversation_id": draft.dify_conversation_id,
        "module": draft.module,
        "op_desc": draft.operation_description,
        "environment": draft.environment,
        "issue_type": draft.issue_type,
        "search_keyword": draft.search_keyword,
        "matched_record_id": draft.matched_record_id,
        "matched_snapshot": draft.matched_snapshot or {},
        "record_id": draft.feishu_record_id,
        "version": draft.version,
        "created_at": draft.created_at.isoformat() if draft.created_at else None,
        "updated_at": draft.updated_at.isoformat() if draft.updated_at else None,
    }
    if include_attachments:
        body["attachments"] = [
            {
                "attachment_id": str(item.id),
                "name": item.original_name,
                "mime_type": item.mime_type,
                "size_bytes": item.size_bytes,
                "sha256": item.sha256,
                "status": item.status,
            }
            for item in (draft.attachments or [])
        ]
    return body


class BugtrackService:
    async def _lock_bindings(
        self, session: AsyncSession, bindings: Iterable[tuple[str, str]]
    ) -> None:
        """Serialize draft selection across workers for the same external identity."""

        bind = session.get_bind()
        if bind.dialect.name != "postgresql":
            return
        for namespace, key in sorted(set(bindings)):
            await session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:value, 0))"),
                {"value": f"bugtrack:{namespace}:{key}"},
            )

    async def _find_bound_draft(
        self,
        session: AsyncSession,
        bindings: Iterable[tuple[str, str]],
    ) -> Optional[BugDraft]:
        for namespace, key in bindings:
            statement = (
                select(BugConversationBinding)
                .where(
                    BugConversationBinding.namespace == namespace,
                    BugConversationBinding.binding_key == key,
                )
                .with_for_update()
            )
            binding = (await session.execute(statement)).scalar_one_or_none()
            if binding is not None:
                return await session.get(
                    BugDraft, binding.draft_id, with_for_update=True
                )
        return None

    async def _upsert_bindings(
        self,
        session: AsyncSession,
        draft: BugDraft,
        identity: DraftIdentity,
    ) -> None:
        for namespace, key in identity.bindings():
            statement = select(BugConversationBinding).where(
                BugConversationBinding.namespace == namespace,
                BugConversationBinding.binding_key == key,
            )
            binding = (await session.execute(statement)).scalar_one_or_none()
            if binding is None:
                session.add(
                    BugConversationBinding(
                        draft_id=draft.id,
                        namespace=namespace,
                        binding_key=key,
                        user_key=identity.user_key,
                    )
                )
            else:
                binding.draft_id = draft.id
                binding.user_key = identity.user_key or binding.user_key

    async def _event(
        self,
        session: AsyncSession,
        draft: BugDraft,
        *,
        event_type: str,
        from_state: str = "",
        to_state: str = "",
        actor: str = "system",
        data: Optional[dict[str, Any]] = None,
    ) -> None:
        session.add(
            BugStateEvent(
                draft_id=draft.id,
                event_type=event_type,
                from_state=from_state,
                to_state=to_state,
                actor=actor,
                event_data=data or {},
            )
        )

    async def resolve_draft(
        self,
        session: AsyncSession,
        *,
        identity: DraftIdentity,
        draft_id: str | uuid.UUID | None = None,
    ) -> Optional[BugDraft]:
        """Resolve an existing draft without creating a new business object."""

        bindings = identity.bindings()
        await self._lock_bindings(session, bindings)
        explicit_id = _uuid(draft_id)
        if explicit_id is not None:
            return await session.get(BugDraft, explicit_id, with_for_update=True)
        return await self._find_bound_draft(session, bindings)

    async def ensure_draft(
        self,
        session: AsyncSession,
        *,
        identity: DraftIdentity,
        draft_id: str | uuid.UUID | None = None,
        force_new: bool = False,
        fields_patch: Optional[dict[str, Any]] = None,
        flow_state: str = "",
        source_text: str = "",
        intent: str = "",
        idempotency_key: str = "",
        event_type: str = "draft_ensured",
    ) -> BugDraft:
        bindings = identity.bindings()
        await self._lock_bindings(session, bindings)

        draft: Optional[BugDraft] = None
        explicit_id = _uuid(draft_id)
        if explicit_id is not None:
            draft = await session.get(BugDraft, explicit_id, with_for_update=True)
        if draft is None:
            draft = await self._find_bound_draft(session, bindings)

        if (
            draft is not None
            and draft.status not in TERMINAL_STATUSES
            and _is_expired(draft.expires_at)
        ):
            old_state = draft.flow_state
            draft.status = "expired"
            draft.flow_state = "expired"
            draft.closed_at = _utcnow()
            await self._event(
                session,
                draft,
                event_type="draft_expired",
                from_state=old_state,
                to_state="expired",
            )
            force_new = True

        if force_new or draft is None:
            if draft is not None and draft.status not in TERMINAL_STATUSES:
                old_state = draft.flow_state
                draft.status = "superseded"
                draft.flow_state = "superseded"
                draft.closed_at = _utcnow()
                await self._event(
                    session,
                    draft,
                    event_type="draft_superseded",
                    from_state=old_state,
                    to_state="superseded",
                    data={"reason": "force_new"},
                )
            draft = BugDraft(
                status="active",
                flow_state=flow_state or "collecting",
                channel=identity.channel or "dify",
                user_key=identity.user_key,
                session_id=identity.session_id,
                dify_conversation_id=identity.conversation_id,
                expires_at=_utcnow()
                + timedelta(seconds=settings.bugtrack.timeout_seconds),
            )
            session.add(draft)
            await session.flush()
            await self._event(
                session,
                draft,
                event_type="draft_created",
                to_state=draft.flow_state,
                data={"force_new": force_new},
            )
        elif draft.status in TERMINAL_STATUSES and event_type == "search_requested":
            return await self.ensure_draft(
                session,
                identity=identity,
                force_new=True,
                fields_patch=fields_patch,
                flow_state=flow_state,
                source_text=source_text,
                intent=intent,
                idempotency_key=idempotency_key,
                event_type=event_type,
            )

        old_state = draft.flow_state
        patch = fields_patch or {}
        for field in (
            "module",
            "operation_description",
            "environment",
            "issue_type",
            "search_keyword",
        ):
            value = patch.get(field)
            if value is not None and str(value).strip():
                setattr(draft, field, str(value).strip())
        if identity.channel:
            draft.channel = identity.channel
        if identity.user_key:
            draft.user_key = identity.user_key
        if identity.session_id:
            draft.session_id = identity.session_id
        if identity.conversation_id:
            draft.dify_conversation_id = identity.conversation_id
        if flow_state:
            draft.flow_state = flow_state
        if draft.status not in TERMINAL_STATUSES:
            draft.expires_at = _utcnow() + timedelta(
                seconds=settings.bugtrack.timeout_seconds
            )

        await self._upsert_bindings(session, draft, identity)

        if source_text:
            idem = idempotency_key.strip()
            if not idem:
                material = "|".join(
                    [
                        str(draft.id),
                        identity.conversation_id,
                        source_text,
                        intent,
                        event_type,
                    ]
                )
                idem = "auto-" + hashlib.sha256(material.encode("utf-8")).hexdigest()
            existing = (
                await session.execute(
                    select(BugTurn.id).where(BugTurn.idempotency_key == idem)
                )
            ).scalar_one_or_none()
            if existing is None:
                session.add(
                    BugTurn(
                        draft_id=draft.id,
                        channel=identity.channel or "dify",
                        role="user",
                        raw_text=source_text,
                        intent=intent,
                        structured_patch=patch,
                        idempotency_key=idem,
                    )
                )

        await self._event(
            session,
            draft,
            event_type=event_type,
            from_state=old_state,
            to_state=draft.flow_state,
            data={"intent": intent} if intent else {},
        )
        await session.flush()
        return draft

    async def record_search_result(
        self,
        session: AsyncSession,
        draft: BugDraft,
        hits: list[dict[str, Any]],
    ) -> None:
        old_state = draft.flow_state
        if hits:
            first = hits[0]
            draft.matched_record_id = str(first.get("record_id") or "")
            draft.matched_snapshot = dict(first)
            draft.flow_state = "await_confirm_identity"
        else:
            draft.matched_record_id = ""
            draft.matched_snapshot = {}
            draft.flow_state = "collecting"
        await self._event(
            session,
            draft,
            event_type="search_completed",
            from_state=old_state,
            to_state=draft.flow_state,
            data={"hit_count": len(hits)},
        )

    async def transition(
        self,
        session: AsyncSession,
        draft: BugDraft,
        *,
        event_type: str,
        flow_state: str,
        status: str = "",
        actor: str = "dify",
        data: Optional[dict[str, Any]] = None,
    ) -> None:
        old_state = draft.flow_state
        if flow_state:
            draft.flow_state = flow_state
        if status:
            draft.status = status
        if draft.status in TERMINAL_STATUSES:
            draft.closed_at = draft.closed_at or _utcnow()
        await self._event(
            session,
            draft,
            event_type=event_type,
            from_state=old_state,
            to_state=draft.flow_state,
            actor=actor,
            data=data,
        )

    async def add_attachment(
        self,
        session: AsyncSession,
        *,
        draft: BugDraft,
        content: bytes,
        original_name: str,
        mime_type: str,
        source_file_id: str = "",
    ) -> BugAttachment:
        storage_key, digest = attachment_storage.save(
            draft_id=draft.id, content=content, mime_type=mime_type
        )
        existing = (
            await session.execute(
                select(BugAttachment).where(
                    BugAttachment.draft_id == draft.id,
                    BugAttachment.sha256 == digest,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            if source_file_id and not existing.source_file_id:
                existing.source_file_id = source_file_id
            return existing
        attachment = BugAttachment(
            draft_id=draft.id,
            sha256=digest,
            storage_key=storage_key,
            original_name=(original_name or "attachment")[:255],
            mime_type=(mime_type or "application/octet-stream")[:127],
            size_bytes=len(content),
            source_file_id=source_file_id[:191],
        )
        session.add(attachment)
        await session.flush()
        await self._event(
            session,
            draft,
            event_type="attachment_added",
            from_state=draft.flow_state,
            to_state=draft.flow_state,
            data={"attachment_id": str(attachment.id), "sha256": digest},
        )
        return attachment

    async def get_draft(
        self,
        session: AsyncSession,
        draft_id: str | uuid.UUID,
        *,
        include_attachments: bool = False,
    ) -> Optional[BugDraft]:
        uid = _uuid(draft_id)
        if uid is None:
            return None
        statement = select(BugDraft).where(BugDraft.id == uid)
        if include_attachments:
            statement = statement.options(selectinload(BugDraft.attachments))
        return (await session.execute(statement)).scalar_one_or_none()

    async def staged_attachments(
        self, session: AsyncSession, draft_id: uuid.UUID
    ) -> list[BugAttachment]:
        statement = (
            select(BugAttachment)
            .where(BugAttachment.draft_id == draft_id)
            .order_by(BugAttachment.created_at)
        )
        return list((await session.execute(statement)).scalars().all())

    async def prepare_outbox(
        self,
        session: AsyncSession,
        *,
        draft: BugDraft,
        operation: str,
        payload: dict[str, Any],
    ) -> BugOutbox:
        idem = f"{operation}:{draft.id}"
        existing = (
            await session.execute(
                select(BugOutbox)
                .where(BugOutbox.idempotency_key == idem)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if existing is not None:
            existing.payload = payload
            existing.attempts += 1
            existing.status = "processing"
            return existing
        outbox = BugOutbox(
            draft_id=draft.id,
            operation=operation,
            idempotency_key=idem,
            payload=payload,
            status="processing",
            attempts=1,
        )
        session.add(outbox)
        await session.flush()
        return outbox

    async def complete_outbox(
        self,
        session: AsyncSession,
        *,
        outbox: BugOutbox,
        success: bool,
        error: str = "",
    ) -> None:
        outbox.status = "succeeded" if success else "pending"
        outbox.last_error = error[:2000]
        if success:
            outbox.completed_at = _utcnow()

    async def get_route_session(
        self, session: AsyncSession, *, channel: str, session_id: str
    ) -> Optional[BugRouteSession]:
        statement = select(BugRouteSession).where(
            BugRouteSession.channel == channel,
            BugRouteSession.session_id == session_id,
        )
        route = (await session.execute(statement)).scalar_one_or_none()
        if route is not None and _is_expired(route.expires_at):
            await session.delete(route)
            return None
        return route

    async def put_route_session(
        self,
        session: AsyncSession,
        *,
        channel: str,
        session_id: str,
        active_app: str,
        conv_a: str,
        conv_b: str,
        route_data: Optional[dict[str, Any]] = None,
    ) -> BugRouteSession:
        # M4 keeps the columns for schema compatibility, while all new route
        # writes are normalized to the single A runtime. Existing untouched rows
        # remain available for audit and manual rollback.
        active_app = "A"
        conv_b = ""
        await self._lock_bindings(session, [(f"route:{channel}", session_id)])
        route = await self.get_route_session(
            session, channel=channel, session_id=session_id
        )
        expires = _utcnow() + timedelta(seconds=settings.bugtrack.route_session_ttl)
        if route is None:
            route = BugRouteSession(
                channel=channel,
                session_id=session_id,
                active_app=active_app or "A",
                conv_a=conv_a,
                conv_b=conv_b,
                route_data=route_data or {},
                expires_at=expires,
            )
            session.add(route)
        else:
            route.active_app = active_app or route.active_app
            route.conv_a = conv_a
            route.conv_b = conv_b
            route.route_data = route_data or {}
            route.expires_at = expires
        await session.execute(
            delete(BugRouteSession).where(BugRouteSession.expires_at < _utcnow())
        )
        await session.flush()
        return route


bugtrack_service = BugtrackService()
