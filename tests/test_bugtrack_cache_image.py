"""Relational Bug draft/attachment ownership regression tests."""

from __future__ import annotations

from io import BytesIO
import json

import pytest
import pytest_asyncio
from fastapi import UploadFile
from sqlalchemy import select
from starlette.requests import Request

import app.routes.bugtrack_internal as route
from app.core.config import settings
from app.core.database import Base, engine, session_scope
from app.models.bugtrack_db import BugAttachment, BugDraft
from app.services.bugtrack_attachment_storage import attachment_storage
from app.services.bugtrack_service import DraftIdentity, bugtrack_service


PNG_A = b"\x89PNG\r\n\x1a\n" + b"problem-a-image"
PNG_B = b"\x89PNG\r\n\x1a\n" + b"problem-b-image"


def _request(ip: str = "124.243.178.156") -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/internal/bugtrack/cache-image",
            "headers": [],
            "client": (ip, 12345),
            "server": ("testserver", 80),
            "scheme": "http",
        }
    )


@pytest_asyncio.fixture(autouse=True)
async def relational_schema(tmp_path, monkeypatch):
    monkeypatch.setattr(settings.bugtrack, "allowed_ips", "124.243.178.156")
    monkeypatch.setattr(settings.bugtrack, "enabled", True)
    monkeypatch.setattr(attachment_storage, "root", tmp_path.resolve())
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
        await connection.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)


async def _cache(conversation_id: str, content: bytes, filename: str):
    return await route.cache_bug_image(
        request=_request(),
        conversation_id=conversation_id,
        draft_id="",
        session_id="h5-session-1",
        channel="h5",
        user_key="h5-session-1",
        source_file_id="",
        image=UploadFile(filename=filename, file=BytesIO(content)),
    )


@pytest.mark.asyncio
async def test_multiple_images_bind_to_one_concrete_draft():
    first = await _cache("conv-b-123", PNG_A, "a.png")
    second = await _cache("conv-b-123", PNG_B, "b.png")

    assert first.status_code == second.status_code == 200
    first_body = json.loads(first.body)
    second_body = json.loads(second.body)
    assert first_body["draft_id"] == second_body["draft_id"]

    async with session_scope() as session:
        attachments = list(
            (
                await session.execute(
                    select(BugAttachment).order_by(BugAttachment.created_at)
                )
            ).scalars()
        )
    assert [item.original_name for item in attachments] == ["a.png", "b.png"]
    assert len({item.draft_id for item in attachments}) == 1


@pytest.mark.asyncio
async def test_new_problem_in_same_conversation_does_not_reuse_old_images():
    cached_a = json.loads((await _cache("conv-shared", PNG_A, "a.png")).body)

    async with session_scope() as session:
        draft_b = await bugtrack_service.ensure_draft(
            session,
            identity=DraftIdentity(channel="dify", conversation_id="conv-shared"),
            force_new=True,
            fields_patch={"operation_description": "问题 B"},
            event_type="search_requested",
        )
        draft_b_id = str(draft_b.id)

    cached_b = json.loads((await _cache("conv-shared", PNG_B, "b.png")).body)
    assert cached_a["draft_id"] != draft_b_id
    assert cached_b["draft_id"] == draft_b_id

    async with session_scope() as session:
        rows = list(
            (
                await session.execute(
                    select(BugAttachment).order_by(BugAttachment.created_at)
                )
            ).scalars()
        )
        old = await session.get(BugDraft, rows[0].draft_id)
    assert rows[0].draft_id != rows[1].draft_id
    assert old is not None and old.status == "superseded"


@pytest.mark.asyncio
async def test_fake_image_is_rejected_before_database_write():
    response = await _cache("conv-b-123", b"not-an-image", "fake.jpg")
    assert response.status_code == 400
    async with session_scope() as session:
        count = len(list((await session.execute(select(BugAttachment))).scalars()))
    assert count == 0


@pytest.mark.asyncio
async def test_persisted_image_is_uploaded_on_confirmed_add(monkeypatch):
    conv_id = "conv-h5-attachment-e2e"
    cached = await _cache(conv_id, PNG_A, "screen.png")
    draft_id = json.loads(cached.body)["draft_id"]
    captured_fields = {}

    monkeypatch.setattr(
        route, "feishu_upload_attachment", lambda *_args: "file-token-1"
    )
    monkeypatch.setattr(
        route, "_find_existing_record_for_draft", lambda *_args: _async_value("")
    )

    def fake_add(fields):
        captured_fields.update(fields)
        return "rec-cache-e2e"

    monkeypatch.setattr(route, "feishu_add_record", fake_add)

    response = await route.add_record_endpoint(
        req=route.AddRecordRequest(
            fields={"模块/功能点": "计费模板管理", "操作描述": "模板保存后未生效"},
            conversation_id=conv_id,
            session_id="h5-session-1",
            channel="h5",
        ),
        request=_request(),
    )

    assert response.status_code == 200
    body = json.loads(response.body)
    assert body["record_id"] == "rec-cache-e2e"
    assert body["draft_id"] == draft_id
    assert captured_fields["Bug截图"] == [{"file_token": "file-token-1"}]
    assert captured_fields["业务草稿ID"] == draft_id

    async with session_scope() as session:
        draft = await bugtrack_service.get_draft(
            session, draft_id, include_attachments=True
        )
    assert draft is not None and draft.status == "submitted"
    assert draft.attachments[0].status == "synced"


async def _async_value(value):
    return value


@pytest.mark.asyncio
async def test_h5_route_session_normalizes_legacy_b_write_to_a():
    async with session_scope() as session:
        await bugtrack_service.put_route_session(
            session,
            channel="h5",
            session_id="h5-restart-case",
            active_app="B",
            conv_a="conv-a",
            conv_b="conv-b",
        )

    async with session_scope() as session:
        restored = await bugtrack_service.get_route_session(
            session, channel="h5", session_id="h5-restart-case"
        )
    assert restored is not None
    assert (restored.active_app, restored.conv_a, restored.conv_b) == (
        "A",
        "conv-a",
        "",
    )
