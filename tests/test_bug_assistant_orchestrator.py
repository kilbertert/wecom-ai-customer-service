"""Deterministic Bug assistant v2 domain and transition tests."""

from __future__ import annotations

import json
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from starlette.requests import Request

import app.routes.bugtrack_internal as route
from app.core.config import settings
from app.core.database import Base, engine, session_scope
from app.models.bugtrack_db import (
    BugDraft,
    BugIssue,
    BugOutbox,
    BugReport,
    BugSubscription,
)
from app.services.bug_assistant_orchestrator import (
    BugAssistantOrchestrator,
    InvalidBugAssistantTransition,
)
from app.services.bugtrack_service import DraftIdentity, bugtrack_service


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
            "reply": record.get("reply", ""),
            "result": record.get("result", ""),
        }

    @staticmethod
    def feedback_score(
        record: dict, *, keyword: str, module: str, op_desc: str
    ) -> float:
        return float(record.get("score", 0))

    @staticmethod
    def operation_similarity(record: dict, *, op_desc: str) -> float:
        return float(record.get("similarity", 0))

    @staticmethod
    def operation_common_chars(record: dict, *, op_desc: str) -> int:
        return int(record.get("common_chars", 0))


@pytest_asyncio.fixture(autouse=True)
async def relational_schema():
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
        await connection.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)


def _identity() -> DraftIdentity:
    return DraftIdentity(
        channel="h5",
        user_key="user-1",
        session_id="session-1",
        conversation_id="conv-b-1",
    )


def _request(ip: str = "127.0.0.1") -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/internal/bugtrack/v2/turn",
            "headers": [],
            "client": (ip, 12345),
            "server": ("testserver", 80),
            "scheme": "http",
        }
    )


def _fields() -> dict[str, str]:
    return {
        "module": "订单管理",
        "operation_description": "Web 后台订单结算失败",
        "environment": "Web后台",
        "issue_type": "bug",
        "search_keyword": "结算失败",
    }


async def _count(model) -> int:
    async with session_scope() as session:
        return int(
            (
                await session.execute(select(func.count()).select_from(model))
            ).scalar_one()
        )


@pytest.mark.asyncio
async def test_incomplete_report_stays_collecting_and_requests_one_field() -> None:
    orchestrator = BugAssistantOrchestrator(FakeCandidateService())
    async with session_scope() as session:
        decision = await orchestrator.handle(
            session,
            event="START_REPORT",
            identity=_identity(),
            fields_patch={"module": "订单管理"},
            source_text="订单有问题",
            idempotency_key="msg-1",
        )

    assert decision.state == "collecting"
    assert decision.next_action == "REQUEST_INFORMATION"
    assert decision.missing_fields == ["operation_description"]


@pytest.mark.asyncio
async def test_confirmed_candidate_creates_report_and_subscription() -> None:
    candidate = {
        "record_id": "rec-existing",
        "module": "订单管理",
        "op_desc": "Web 后台订单结算失败",
        "dev_status": "开发中",
        "score": 139,
    }
    orchestrator = BugAssistantOrchestrator(FakeCandidateService([candidate]))
    async with session_scope() as session:
        start = await orchestrator.handle(
            session,
            event="START_REPORT",
            identity=_identity(),
            fields_patch=_fields(),
            source_text="Web 后台订单结算失败",
            idempotency_key="msg-1",
        )
    assert start.state == "awaiting_match_confirmation"
    assert start.candidate["external_record_id"] == "rec-existing"

    async with session_scope() as session:
        confirmed = await orchestrator.handle(
            session,
            event="CONFIRM_MATCH",
            identity=_identity(),
            draft_id=start.draft_id,
            source_text="是同一个问题",
            idempotency_key="msg-2",
        )
    assert confirmed.state == "linked_existing"
    assert confirmed.next_action == "COMPLETED"
    assert confirmed.issue_id and confirmed.report_id
    assert await _count(BugIssue) == 1
    assert await _count(BugReport) == 1
    assert await _count(BugSubscription) == 1
    assert await _count(BugOutbox) == 0

    async with session_scope() as session:
        repeated = await orchestrator.handle(
            session,
            event="CONFIRM_MATCH",
            identity=_identity(),
            draft_id=start.draft_id,
        )
    assert repeated.issue_id == confirmed.issue_id
    assert repeated.report_id == confirmed.report_id
    assert await _count(BugIssue) == 1
    assert await _count(BugReport) == 1
    assert await _count(BugSubscription) == 1


@pytest.mark.asyncio
async def test_rejected_candidate_queues_new_issue_once() -> None:
    candidate = {
        "record_id": "rec-other",
        "module": "订单管理",
        "op_desc": "Web 后台订单结算失败",
        "score": 130,
    }
    orchestrator = BugAssistantOrchestrator(FakeCandidateService([candidate]))
    async with session_scope() as session:
        start = await orchestrator.handle(
            session,
            event="START_REPORT",
            identity=_identity(),
            fields_patch=_fields(),
            source_text="Web 后台订单结算失败",
            idempotency_key="msg-1",
        )
    async with session_scope() as session:
        rejected = await orchestrator.handle(
            session,
            event="REJECT_MATCH",
            identity=_identity(),
            draft_id=start.draft_id,
            source_text="不是这个问题",
            idempotency_key="msg-2",
        )
    assert rejected.state == "ready_to_submit"

    async with session_scope() as session:
        queued = await orchestrator.handle(
            session,
            event="CONFIRM_SUBMIT",
            identity=_identity(),
            draft_id=start.draft_id,
            source_text="确认提交",
            idempotency_key="msg-3",
        )
    assert queued.state == "queued_for_submission"
    assert queued.next_action == "WAIT_FOR_SYNC"
    assert await _count(BugIssue) == 1
    assert await _count(BugReport) == 1
    assert await _count(BugSubscription) == 1
    assert await _count(BugOutbox) == 1

    async with session_scope() as session:
        repeated = await orchestrator.handle(
            session,
            event="CONFIRM_SUBMIT",
            identity=_identity(),
            draft_id=start.draft_id,
        )
    assert repeated.issue_id == queued.issue_id
    assert repeated.report_id == queued.report_id
    assert await _count(BugIssue) == 1
    assert await _count(BugReport) == 1
    assert await _count(BugSubscription) == 1
    assert await _count(BugOutbox) == 1


@pytest.mark.asyncio
async def test_low_score_candidate_never_enters_match_confirmation() -> None:
    candidate = {
        "record_id": "rec-low-score",
        "module": "订单管理",
        "op_desc": "用户资料导出按钮样式异常",
        "score": 101,
    }
    orchestrator = BugAssistantOrchestrator(FakeCandidateService([candidate]))
    async with session_scope() as session:
        decision = await orchestrator.handle(
            session,
            event="START_REPORT",
            identity=_identity(),
            fields_patch=_fields(),
            source_text="Web 后台订单结算失败",
            idempotency_key="msg-1",
        )

    assert decision.state == "ready_to_submit"
    assert decision.candidate == {}


@pytest.mark.asyncio
async def test_no_module_high_similarity_candidate_requires_user_confirmation() -> None:
    candidate = {
        "record_id": "rec-similar",
        "module": "订单管理",
        "op_desc": "后台订单结算时提示失败，无法完成结算",
        "score": 74,
        "similarity": 0.78,
        "common_chars": 8,
    }
    orchestrator = BugAssistantOrchestrator(FakeCandidateService([candidate]))
    async with session_scope() as session:
        decision = await orchestrator.handle(
            session,
            event="START_REPORT",
            identity=_identity(),
            fields_patch={
                "operation_description": "后台订单结算操作失败，订单不能正常结算",
                "search_keyword": "订单结算失败",
            },
            source_text="后台订单结算操作失败，订单不能正常结算",
            idempotency_key="msg-similar",
        )

    assert decision.state == "awaiting_match_confirmation"
    assert decision.candidate["external_record_id"] == "rec-similar"


@pytest.mark.asyncio
async def test_short_generic_candidate_is_not_presented_without_module() -> None:
    candidate = {
        "record_id": "rec-generic",
        "module": "订单管理",
        "op_desc": "保存失败",
        "score": 74,
        "similarity": 1.0,
        "common_chars": 4,
    }
    orchestrator = BugAssistantOrchestrator(FakeCandidateService([candidate]))
    async with session_scope() as session:
        decision = await orchestrator.handle(
            session,
            event="START_REPORT",
            identity=_identity(),
            fields_patch={
                "operation_description": "保存失败",
                "search_keyword": "保存失败",
            },
            source_text="保存失败",
            idempotency_key="msg-generic",
        )

    assert decision.state == "ready_to_submit"


@pytest.mark.asyncio
async def test_illegal_confirmation_is_rejected() -> None:
    orchestrator = BugAssistantOrchestrator(FakeCandidateService())
    async with session_scope() as session:
        start = await orchestrator.handle(
            session,
            event="START_REPORT",
            identity=_identity(),
            fields_patch={"module": "订单管理"},
            source_text="订单有问题",
            idempotency_key="msg-1",
        )

    with pytest.raises(InvalidBugAssistantTransition) as exc_info:
        async with session_scope() as session:
            await orchestrator.handle(
                session,
                event="CONFIRM_SUBMIT",
                identity=_identity(),
                draft_id=start.draft_id,
            )
    assert exc_info.value.state == "collecting"


@pytest.mark.asyncio
async def test_v2_binding_is_isolated_from_legacy_draft() -> None:
    identity = _identity()
    async with session_scope() as session:
        legacy = await bugtrack_service.ensure_draft(
            session,
            identity=identity,
            fields_patch={"operation_description": "legacy problem"},
            flow_state="await_confirm_new",
            source_text="legacy problem",
            event_type="search_requested",
        )
        legacy_id = str(legacy.id)

    orchestrator = BugAssistantOrchestrator(FakeCandidateService())
    async with session_scope() as session:
        decision = await orchestrator.handle(
            session,
            event="START_REPORT",
            identity=identity,
            fields_patch=_fields(),
            source_text="Web 后台订单结算失败",
            idempotency_key="v2-msg-1",
        )

    assert decision.draft_id != legacy_id
    async with session_scope() as session:
        legacy = await session.get(BugDraft, uuid.UUID(legacy_id))
        v2 = await session.get(BugDraft, uuid.UUID(decision.draft_id))
    assert legacy is not None and legacy.flow_state == "await_confirm_new"
    assert v2 is not None and v2.channel == "h5_v2"


@pytest.mark.asyncio
async def test_v2_route_returns_stable_structured_contract(monkeypatch) -> None:
    monkeypatch.setattr(settings.bugtrack, "allowed_ips", "127.0.0.1")
    monkeypatch.setattr(
        route,
        "bug_assistant_orchestrator",
        BugAssistantOrchestrator(FakeCandidateService()),
    )
    response = await route.bug_assistant_turn(
        req=route.BugAssistantTurnRequest(
            event="START_REPORT",
            channel="h5",
            user_key="user-1",
            session_id="session-1",
            source_text="Web 后台订单结算失败",
            idempotency_key="route-msg-1",
            fields=route.BugAssistantFields(**_fields()),
        ),
        request=_request(),
    )
    body = json.loads(response.body)

    assert response.status_code == 200
    assert set(body) == {
        "success",
        "draft_id",
        "state",
        "next_action",
        "missing_fields",
        "candidate",
        "issue_id",
        "report_id",
    }
    assert body["state"] == "ready_to_submit"
    assert body["next_action"] == "CONFIRM_SUBMIT"


@pytest.mark.asyncio
async def test_explicit_unknown_draft_id_never_falls_back_to_bound_draft() -> None:
    orchestrator = BugAssistantOrchestrator(FakeCandidateService())
    async with session_scope() as session:
        await orchestrator.handle(
            session,
            event="START_REPORT",
            identity=_identity(),
            fields_patch=_fields(),
            source_text="Web 后台订单结算失败",
            idempotency_key="msg-1",
        )

    with pytest.raises(InvalidBugAssistantTransition) as exc_info:
        async with session_scope() as session:
            await orchestrator.handle(
                session,
                event="CONFIRM_SUBMIT",
                identity=_identity(),
                draft_id=str(uuid.uuid4()),
            )
    assert exc_info.value.state == "missing_draft"


@pytest.mark.asyncio
async def test_suspend_then_resume_restores_previous_confirmation_state() -> None:
    orchestrator = BugAssistantOrchestrator(FakeCandidateService())
    async with session_scope() as session:
        started = await orchestrator.handle(
            session,
            event="START_REPORT",
            identity=_identity(),
            fields_patch=_fields(),
            source_text="Web 后台订单结算失败",
            idempotency_key="pause-start",
        )
        paused = await orchestrator.handle(
            session,
            event="SUSPEND",
            identity=_identity(),
            draft_id=started.draft_id,
            idempotency_key="pause-event",
        )
        resumed = await orchestrator.handle(
            session,
            event="RESUME",
            identity=_identity(),
            draft_id=started.draft_id,
            idempotency_key="resume-event",
        )

    assert started.state == "ready_to_submit"
    assert paused.state == "suspended"
    assert paused.next_action == "HANDOFF_QA"
    assert resumed.state == "ready_to_submit"
    assert resumed.next_action == "CONFIRM_SUBMIT"
