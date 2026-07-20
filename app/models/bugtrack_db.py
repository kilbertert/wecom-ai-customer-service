"""Relational schema for structured Bug feedback, turns and attachments."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class BugDraft(Base):
    __tablename__ = "bug_drafts"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    flow_state: Mapped[str] = mapped_column(
        String(64), nullable=False, default="collecting"
    )
    channel: Mapped[str] = mapped_column(String(32), nullable=False, default="dify")
    user_key: Mapped[str] = mapped_column(String(191), nullable=False, default="")
    session_id: Mapped[str] = mapped_column(String(191), nullable=False, default="")
    dify_conversation_id: Mapped[str] = mapped_column(
        String(191), nullable=False, default=""
    )

    module: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    operation_description: Mapped[str] = mapped_column(
        Text, nullable=False, default=""
    )
    environment: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    issue_type: Mapped[str] = mapped_column(String(64), nullable=False, default="bug")
    search_keyword: Mapped[str] = mapped_column(String(255), nullable=False, default="")

    matched_record_id: Mapped[str] = mapped_column(
        String(191), nullable=False, default=""
    )
    matched_snapshot: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict
    )
    feishu_record_id: Mapped[str] = mapped_column(
        String(191), nullable=False, default=""
    )

    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    submitted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    attachments: Mapped[list["BugAttachment"]] = relationship(
        back_populates="draft", cascade="all, delete-orphan"
    )
    turns: Mapped[list["BugTurn"]] = relationship(
        back_populates="draft", cascade="all, delete-orphan"
    )

    __mapper_args__ = {"version_id_col": version}
    __table_args__ = (
        Index("ix_bug_drafts_status_updated", "status", "updated_at"),
        Index("ix_bug_drafts_dify_conversation", "dify_conversation_id"),
        Index("ix_bug_drafts_feishu_record", "feishu_record_id"),
    )


class BugConversationBinding(Base):
    __tablename__ = "bug_conversation_bindings"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    draft_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("bug_drafts.id", ondelete="CASCADE"), nullable=False
    )
    namespace: Mapped[str] = mapped_column(String(48), nullable=False)
    binding_key: Mapped[str] = mapped_column(String(191), nullable=False)
    user_key: Mapped[str] = mapped_column(String(191), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )

    __table_args__ = (
        UniqueConstraint("namespace", "binding_key", name="uq_bug_binding_key"),
        Index("ix_bug_bindings_draft", "draft_id"),
    )


class BugTurn(Base):
    __tablename__ = "bug_turns"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    draft_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("bug_drafts.id", ondelete="CASCADE"), nullable=False
    )
    channel: Mapped[str] = mapped_column(String(32), nullable=False, default="dify")
    role: Mapped[str] = mapped_column(String(16), nullable=False, default="user")
    raw_text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    intent: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    structured_patch: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict
    )
    dify_run_id: Mapped[str] = mapped_column(String(191), nullable=False, default="")
    idempotency_key: Mapped[str] = mapped_column(
        String(191), nullable=False, unique=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )

    draft: Mapped[BugDraft] = relationship(back_populates="turns")

    __table_args__ = (Index("ix_bug_turns_draft_created", "draft_id", "created_at"),)


class BugAttachment(Base):
    __tablename__ = "bug_attachments"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    draft_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("bug_drafts.id", ondelete="CASCADE"), nullable=False
    )
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    storage_key: Mapped[str] = mapped_column(String(512), nullable=False)
    original_name: Mapped[str] = mapped_column(String(255), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(127), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    source_file_id: Mapped[str] = mapped_column(
        String(191), nullable=False, default=""
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="staged")
    feishu_file_token: Mapped[str] = mapped_column(
        String(255), nullable=False, default=""
    )
    last_error: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    synced_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    draft: Mapped[BugDraft] = relationship(back_populates="attachments")

    __table_args__ = (
        UniqueConstraint("draft_id", "sha256", name="uq_bug_attachment_draft_sha"),
        Index("ix_bug_attachments_draft_status", "draft_id", "status"),
    )


class BugStateEvent(Base):
    __tablename__ = "bug_state_events"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    draft_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("bug_drafts.id", ondelete="CASCADE"), nullable=False
    )
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    from_state: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    to_state: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    actor: Mapped[str] = mapped_column(String(64), nullable=False, default="system")
    event_data: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )

    __table_args__ = (Index("ix_bug_events_draft_created", "draft_id", "created_at"),)


class BugOutbox(Base):
    __tablename__ = "bug_outbox"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    draft_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("bug_drafts.id", ondelete="CASCADE"), nullable=False
    )
    operation: Mapped[str] = mapped_column(String(32), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(
        String(191), nullable=False, unique=True
    )
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    __table_args__ = (Index("ix_bug_outbox_status_created", "status", "created_at"),)


class BugRouteSession(Base):
    __tablename__ = "bug_route_sessions"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    channel: Mapped[str] = mapped_column(String(32), nullable=False)
    session_id: Mapped[str] = mapped_column(String(191), nullable=False)
    active_app: Mapped[str] = mapped_column(String(8), nullable=False, default="A")
    conv_a: Mapped[str] = mapped_column(String(191), nullable=False, default="")
    conv_b: Mapped[str] = mapped_column(String(191), nullable=False, default="")
    route_data: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )

    __mapper_args__ = {"version_id_col": version}
    __table_args__ = (
        UniqueConstraint("channel", "session_id", name="uq_bug_route_session"),
        Index("ix_bug_route_expires", "expires_at"),
    )
