"""M2 active new-Bug submission, attachment and fallback regression tests."""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from sqlalchemy import select

import app.services.bug_issue_sync_service as sync_module
from app.core.database import Base, engine, session_scope
from app.models.bugtrack_db import (
    BugAttachment,
    BugDraft,
    BugIssue,
    BugOutbox,
    BugReport,
    BugSubscription,
)
from app.services.bug_assistant_message_service import BugAssistantMessageService
from app.services.bug_assistant_orchestrator import BugAssistantOrchestrator
from app.services.bug_issue_sync_service import BugIssueSyncService
from app.services.bugtrack_attachment_storage import attachment_storage


PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"m2-image"


class FakeCandidateService:
    def __init__(self, records: list[dict] | None = None) -> None:
        self.records = records or []

    async def search_by_feedback(
        self, keyword: str, module: str = "", op_desc: str = "", limit: int = 20
    ) -> list[dict]:
        return list(self.records[:limit])

    @staticmethod
    def record_to_summary(record: dict) -> dict[str, str]:
        return {
            "record_id": record.get("record_id", ""),
            "module": record.get("module", ""),
            "op_desc": record.get("op_desc", ""),
            "dev_status": record.get("dev_status", ""),
            "reply": "",
            "result": "",
        }

    @staticmethod
    def feedback_score(
        record: dict, *, keyword: str, module: str, op_desc: str
    ) -> float:
        return float(record.get("score", 0))


class FailingCandidateService(FakeCandidateService):
    async def search_by_feedback(
        self, keyword: str, module: str = "", op_desc: str = "", limit: int = 20
    ) -> list[dict]:
        raise RuntimeError("candidate backend unavailable")


@pytest_asyncio.fixture(autouse=True)
async def relational_schema(tmp_path, monkeypatch):
    monkeypatch.setattr(attachment_storage, "root", tmp_path.resolve())
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
        await connection.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)


def _message_service(records: list[dict] | None = None) -> BugAssistantMessageService:
    return BugAssistantMessageService(
        orchestrator=BugAssistantOrchestrator(FakeCandidateService(records)),
        sync_service=BugIssueSyncService(),
    )


@pytest.mark.asyncio
async def test_candidate_search_failure_keeps_v2_draft_without_legacy_fallback() -> None:
    service = BugAssistantMessageService(
        orchestrator=BugAssistantOrchestrator(FailingCandidateService()),
        sync_service=BugIssueSyncService(),
    )

    result = await service.process(
        channel="h5",
        user_key="user-1",
        session_id="session-1",
        text="Web 后台订单结算失败",
        message_id="msg-search-failed",
    )

    assert result.state == "collecting"
    assert result.continue_session is True
    assert result.fallback_required is False
    assert "反馈已保留" in result.assistant_text
    async with session_scope() as session:
        draft = (await session.execute(select(BugDraft))).scalar_one()
    assert draft.flow_state == "collecting"
    assert draft.status != "abandoned"


@pytest.mark.asyncio
async def test_new_bug_is_confirmed_synced_with_attachment_and_subscription(
    monkeypatch,
) -> None:
    captured: dict = {}
    add_calls = 0

    monkeypatch.setattr(sync_module, "feishu_search_records", lambda *_args: [])
    monkeypatch.setattr(
        sync_module, "feishu_upload_attachment", lambda *_args: "file-token-1"
    )

    def fake_add(fields):
        nonlocal add_calls
        add_calls += 1
        captured.update(fields)
        return "rec-v2-new"

    monkeypatch.setattr(sync_module, "feishu_add_record", fake_add)

    service = _message_service()
    first = await service.process(
        channel="h5",
        user_key="user-1",
        session_id="session-1",
        text="Web 后台订单结算失败",
        message_id="msg-1",
        image_bytes=PNG_BYTES,
        image_name="screen.png",
        image_mime="image/png",
    )
    assert first.state == "ready_to_submit"
    assert first.continue_session is True
    assert "确认提交" in first.assistant_text

    confirmed = await service.process(
        channel="h5",
        user_key="user-1",
        session_id="session-1",
        text="确认提交",
        message_id="msg-2",
    )
    assert confirmed.state == "submitted"
    assert confirmed.record_id == "rec-v2-new"
    assert confirmed.continue_session is False
    assert captured["业务草稿ID"] == first.draft_id
    assert captured["Bug截图"] == [{"file_token": "file-token-1"}]
    assert add_calls == 1

    async with session_scope() as session:
        draft = (
            await session.execute(
                select(BugDraft).where(BugDraft.id == uuid.UUID(first.draft_id))
            )
        ).scalar_one()
        attachment = (await session.execute(select(BugAttachment))).scalar_one()
        issue = (await session.execute(select(BugIssue))).scalar_one()
        report = (await session.execute(select(BugReport))).scalar_one()
        subscription = (await session.execute(select(BugSubscription))).scalar_one()
        outbox = (await session.execute(select(BugOutbox))).scalar_one()
    assert draft.status == "submitted" and draft.flow_state == "submitted"
    assert attachment.status == "synced"
    assert issue.external_record_id == "rec-v2-new"
    assert report.external_record_id == "rec-v2-new"
    assert subscription.status == "active"
    assert outbox.status == "succeeded"

    repeated = await BugIssueSyncService().sync(first.draft_id)
    assert repeated.record_id == "rec-v2-new"
    assert repeated.idempotent is True
    assert add_calls == 1


@pytest.mark.asyncio
async def test_candidate_path_is_confirmed_natively_with_independent_report() -> None:
    service = _message_service(
        [
            {
                "record_id": "rec-existing",
                "module": "订单管理",
                "op_desc": "Web 后台订单结算失败",
                "dev_status": "开发中",
                "score": 140,
            }
        ]
    )
    result = await service.process(
        channel="h5",
        user_key="user-1",
        session_id="session-1",
        text="Web 后台订单结算失败",
        message_id="msg-1",
    )

    assert result.fallback_required is False
    assert result.state == "awaiting_match_confirmation"
    assert result.continue_session is True
    assert "确认相同" in result.assistant_text

    confirmed = await service.process(
        channel="h5",
        user_key="user-1",
        session_id="session-1",
        text="确认相同",
        message_id="msg-2",
    )

    assert confirmed.state == "linked_existing"
    assert confirmed.continue_session is False
    assert confirmed.record_id == "rec-existing"
    assert "独立报告" in confirmed.assistant_text
    async with session_scope() as session:
        draft = (await session.execute(select(BugDraft))).scalar_one()
        issue = (await session.execute(select(BugIssue))).scalar_one()
        report = (await session.execute(select(BugReport))).scalar_one()
        subscription = (await session.execute(select(BugSubscription))).scalar_one()
    assert draft.status == "submitted" and draft.flow_state == "linked_existing"
    assert issue.external_record_id == "rec-existing"
    assert report.issue_id == issue.id
    assert report.link_type == "confirmed_duplicate"
    assert report.external_record_id == "rec-existing"
    assert subscription.issue_id == issue.id


@pytest.mark.asyncio
async def test_rejected_candidate_returns_to_new_issue_confirmation() -> None:
    service = _message_service(
        [
            {
                "record_id": "rec-existing",
                "module": "订单管理",
                "op_desc": "Web 后台订单结算失败",
                "dev_status": "开发中",
                "score": 140,
            }
        ]
    )
    first = await service.process(
        channel="h5",
        user_key="user-1",
        session_id="session-1",
        text="Web 后台订单结算失败",
        message_id="msg-1",
    )
    rejected = await service.process(
        channel="h5",
        user_key="user-1",
        session_id="session-1",
        text="不是同一个，这是另一个问题",
        message_id="msg-2",
    )

    assert first.state == "awaiting_match_confirmation"
    assert rejected.state == "ready_to_submit"
    assert rejected.continue_session is True
    assert "确认提交" in rejected.assistant_text


@pytest.mark.asyncio
async def test_followup_text_is_appended_before_confirmation() -> None:
    service = _message_service()
    first = await service.process(
        channel="h5",
        user_key="user-1",
        session_id="session-1",
        text="订单结算失败",
        message_id="msg-1",
    )
    second = await service.process(
        channel="h5",
        user_key="user-1",
        session_id="session-1",
        text="只在 Web 后台发生",
        message_id="msg-2",
    )

    assert first.state == second.state == "ready_to_submit"
    async with session_scope() as session:
        draft = (
            await session.execute(
                select(BugDraft).where(BugDraft.id == uuid.UUID(first.draft_id))
            )
        ).scalar_one()
    assert draft.operation_description == "订单结算失败\n补充：只在 Web 后台发生"


@pytest.mark.asyncio
async def test_feishu_failure_stays_queued_and_retry_submits_later(
    monkeypatch,
) -> None:
    scheduled: list[str] = []

    def fail_add(*_args):
        raise RuntimeError("Feishu unavailable")

    monkeypatch.setattr(sync_module, "feishu_search_records", lambda *_args: [])
    monkeypatch.setattr(sync_module, "feishu_add_record", fail_add)
    monkeypatch.setattr(
        BugAssistantMessageService,
        "_schedule_retry",
        staticmethod(scheduled.append),
    )

    service = _message_service()
    first = await service.process(
        channel="h5",
        user_key="user-retry",
        session_id="session-retry",
        text="订单结算失败",
        message_id="msg-retry-1",
    )
    pending = await service.process(
        channel="h5",
        user_key="user-retry",
        session_id="session-retry",
        text="确认提交",
        message_id="msg-retry-2",
    )

    assert pending.state == "queued_for_submission"
    assert pending.sync_pending is True
    assert pending.fallback_required is False
    assert scheduled == [first.draft_id]
    async with session_scope() as session:
        draft = (
            await session.execute(
                select(BugDraft).where(BugDraft.id == uuid.UUID(first.draft_id))
            )
        ).scalar_one()
        issue = (await session.execute(select(BugIssue))).scalar_one()
        report = (await session.execute(select(BugReport))).scalar_one()
        outbox = (await session.execute(select(BugOutbox))).scalar_one()
    assert draft.flow_state == "queued_for_submission"
    assert issue.status == "pending_sync"
    assert report.status == "queued_for_submission"
    assert outbox.status == "pending"
    assert "Feishu unavailable" in outbox.last_error

    monkeypatch.setattr(sync_module, "feishu_add_record", lambda *_args: "rec-retried")
    synced = await BugIssueSyncService().sync(first.draft_id)

    assert synced.record_id == "rec-retried"
    async with session_scope() as session:
        draft = (
            await session.execute(
                select(BugDraft).where(BugDraft.id == uuid.UUID(first.draft_id))
            )
        ).scalar_one()
        outbox = (await session.execute(select(BugOutbox))).scalar_one()
    assert draft.flow_state == "submitted"
    assert outbox.status == "succeeded"
