from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.dialects import postgresql

from app.core.database import Base, engine, session_scope
from app.models.bugtrack_db import (
    BugDraft,
    BugIssue,
    BugIssueStatusEvent,
    BugNotificationDelivery,
    BugReport,
    BugSubscription,
)
from app.services.bug_issue_status_service import (
    BugIssueStatusService,
    _subscribed_issue_query,
)


class FakeCandidateService:
    def __init__(self, records: dict[str, dict]) -> None:
        self.records = records

    async def get_record(self, record_id: str):
        return self.records.get(record_id)

    @staticmethod
    def record_to_summary(record: dict) -> dict[str, str]:
        return dict(record)


class FakeWeChatService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    async def send_message_simple(
        self, external_userid: str, open_kfid: str, text: str
    ) -> dict:
        self.calls.append((external_userid, open_kfid, text))
        return {"errcode": 0}


def test_reconcile_query_is_valid_for_postgresql_distinct_ordering() -> None:
    sql = str(
        _subscribed_issue_query(100).compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )
    assert "SELECT DISTINCT bug_issues.id, bug_issues.external_record_id" in sql
    assert "ORDER BY bug_issues.id" in sql


@pytest_asyncio.fixture(autouse=True)
async def relational_schema():
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
        await connection.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)


async def _seed_issue() -> tuple[str, str]:
    async with session_scope() as session:
        draft = BugDraft(
            status="submitted",
            flow_state="linked_existing",
            channel="h5_v2",
            user_key="h5-user",
            session_id="h5-session",
            operation_description="订单结算失败",
        )
        issue = BugIssue(
            source_system="feishu",
            external_record_id="rec-existing",
            title="订单结算失败",
            module="订单管理",
            normalized_description="订单结算失败",
            status="开发中",
            external_snapshot={
                "status": "开发中",
                "reply": "已定位",
                "result": "",
            },
        )
        session.add_all([draft, issue])
        await session.flush()
        session.add(
            BugReport(
                draft_id=draft.id,
                issue_id=issue.id,
                status="linked_existing",
                link_type="confirmed_duplicate",
                external_record_id="rec-existing",
                report_snapshot={"operation_description": "订单结算失败"},
            )
        )
        session.add_all(
            [
                BugSubscription(
                    issue_id=issue.id,
                    channel="h5",
                    subscriber_key="h5-user",
                    user_key="h5-user",
                    session_id="h5-session",
                ),
                BugSubscription(
                    issue_id=issue.id,
                    channel="wecom_kf",
                    subscriber_key="wm-user",
                    user_key="wm-user",
                    session_id="wm-user:wk-kfid",
                ),
            ]
        )
        return str(issue.id), str(draft.id)


@pytest.mark.asyncio
async def test_reconcile_creates_one_event_and_idempotent_deliveries() -> None:
    issue_id, _ = await _seed_issue()
    service = BugIssueStatusService(
        FakeCandidateService(
            {
                "rec-existing": {
                    "record_id": "rec-existing",
                    "module": "订单管理",
                    "op_desc": "订单结算失败",
                    "dev_status": "已完成",
                    "reply": "已发布修复",
                    "result": "请刷新页面后重试",
                }
            }
        )
    )

    first = await service.reconcile()
    second = await service.reconcile()

    assert first.checked == 1
    assert first.changed == 1
    assert len(first.delivery_ids) == 1
    assert second.changed == 0
    async with session_scope() as session:
        issue = await session.get(BugIssue, uuid.UUID(issue_id))
        event_count = (
            await session.execute(select(func.count(BugIssueStatusEvent.id)))
        ).scalar_one()
        deliveries = list(
            (
                await session.execute(
                    select(BugNotificationDelivery).order_by(
                        BugNotificationDelivery.channel
                    )
                )
            ).scalars()
        )
    assert issue is not None and issue.status == "已完成"
    assert issue.external_snapshot["progress"]["result"] == "请刷新页面后重试"
    assert event_count == 1
    assert [(item.channel, item.status) for item in deliveries] == [
        ("h5", "available"),
        ("wecom_kf", "pending"),
    ]


@pytest.mark.asyncio
async def test_h5_notifications_are_listed_and_acknowledged() -> None:
    await _seed_issue()
    service = BugIssueStatusService(
        FakeCandidateService(
            {
                "rec-existing": {
                    "record_id": "rec-existing",
                    "module": "订单管理",
                    "op_desc": "订单结算失败",
                    "dev_status": "测试中",
                    "reply": "修复版本已进入测试",
                    "result": "",
                }
            }
        )
    )
    await service.reconcile()

    items = await service.list_notifications(
        channel="h5",
        user_key="h5-user",
        session_id="h5-session",
    )
    assert len(items) == 1
    assert "测试中" in items[0].message

    acknowledged = await service.acknowledge(
        channel="h5",
        user_key="h5-user",
        session_id="h5-session",
        notification_ids=[items[0].notification_id],
    )
    assert acknowledged == 1
    assert (
        await service.list_notifications(
            channel="h5",
            user_key="h5-user",
            session_id="h5-session",
        )
        == []
    )


@pytest.mark.asyncio
async def test_wecom_delivery_and_issue_impact() -> None:
    issue_id, _ = await _seed_issue()
    service = BugIssueStatusService(
        FakeCandidateService(
            {
                "rec-existing": {
                    "record_id": "rec-existing",
                    "module": "订单管理",
                    "op_desc": "订单结算失败",
                    "dev_status": "已完成",
                    "reply": "",
                    "result": "已修复",
                }
            }
        )
    )
    result = await service.reconcile()
    fake_wechat = FakeWeChatService()

    delivered = await service.deliver(
        result.delivery_ids[0], wechat_service=fake_wechat
    )
    repeated = await service.deliver(result.delivery_ids[0], wechat_service=fake_wechat)
    impact = await service.issue_impact(issue_id)
    external_impact = await service.issue_impact("rec-existing")

    assert delivered["status"] == repeated["status"] == "sent"
    assert fake_wechat.calls == [("wm-user", "wk-kfid", fake_wechat.calls[0][2])]
    assert "已完成" in fake_wechat.calls[0][2]
    assert impact["report_count"] == 1
    assert impact["subscriber_count"] == 2
    assert external_impact["issue_id"] == issue_id


@pytest.mark.asyncio
async def test_progress_lookup_is_read_only_and_scoped_to_subscriber() -> None:
    await _seed_issue()
    service = BugIssueStatusService(FakeCandidateService({}))

    items = await service.progress_for_subscriber(
        channel="h5",
        user_key="h5-user",
        session_id="h5-session",
    )
    other = await service.progress_for_subscriber(
        channel="h5",
        user_key="another-user",
        session_id="another-session",
    )

    assert len(items) == 1
    assert items[0]["title"] == "订单结算失败"
    assert items[0]["progress"]["status"] == "开发中"
    assert other == []
    async with session_scope() as session:
        assert (
            await session.execute(select(func.count(BugIssueStatusEvent.id)))
        ).scalar_one() == 0
