"""Deterministic Bug assistant state machine for the v2 migration path."""

from __future__ import annotations

from dataclasses import dataclass, field
import logging
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.bugtrack_db import (
    BugDraft,
    BugIssue,
    BugReport,
    BugStateEvent,
    BugSubscription,
    utcnow,
)
from app.services.bugtrack_service import (
    DraftIdentity,
    TERMINAL_STATUSES,
    bugtrack_service,
)
from app.services.smartsheet_query_service import (
    EXPLICIT_CONFIRMATION_MIN_COMMON_CHARS,
    EXPLICIT_CONFIRMATION_SIMILARITY_THRESHOLD,
    EXISTING_ISSUE_SCORE_THRESHOLD,
    SmartSheetQueryService,
)


logger = logging.getLogger(__name__)

SUPPORTED_EVENTS = {
    "START_REPORT",
    "PATCH_REPORT",
    "CONFIRM_MATCH",
    "REJECT_MATCH",
    "CONFIRM_SUBMIT",
    "SUSPEND",
    "RESUME",
    "CANCEL",
}
IMMUTABLE_FLOW_STATES = {
    "linked_existing",
    "queued_for_submission",
    "submitted",
    "abandoned",
}


class InvalidBugAssistantEvent(ValueError):
    pass


class InvalidBugAssistantTransition(RuntimeError):
    def __init__(self, event: str, state: str) -> None:
        self.event = event
        self.state = state
        super().__init__(f"event {event} is not allowed from state {state}")


@dataclass(frozen=True)
class BugAssistantDecision:
    draft_id: str
    state: str
    next_action: str
    missing_fields: list[str] = field(default_factory=list)
    candidate: dict[str, Any] = field(default_factory=dict)
    issue_id: str = ""
    report_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "success": True,
            "draft_id": self.draft_id,
            "state": self.state,
            "next_action": self.next_action,
            "missing_fields": list(self.missing_fields),
            "candidate": dict(self.candidate),
            "issue_id": self.issue_id,
            "report_id": self.report_id,
        }


def _draft_snapshot(draft: BugDraft) -> dict[str, Any]:
    business_channel = draft.channel.removesuffix("_v2")
    return {
        "draft_id": str(draft.id),
        "channel": business_channel,
        "storage_channel": draft.channel,
        "user_key": draft.user_key,
        "session_id": draft.session_id,
        "conversation_id": draft.dify_conversation_id,
        "module": draft.module,
        "operation_description": draft.operation_description,
        "environment": draft.environment,
        "issue_type": draft.issue_type,
        "search_keyword": draft.search_keyword,
    }


def _missing_fields(draft: BugDraft) -> list[str]:
    missing: list[str] = []
    if not draft.operation_description.strip():
        missing.append("operation_description")
    return missing


def v2_storage_identity(identity: DraftIdentity) -> DraftIdentity:
    """Keep v2 drafts isolated from legacy Dify/H5 conversation bindings."""

    channel = (identity.channel or "unknown").strip()
    return DraftIdentity(
        channel=f"{channel}_v2",
        user_key=identity.user_key,
        session_id=identity.session_id,
        conversation_id="",
    )


class BugAssistantOrchestrator:
    def __init__(
        self, candidate_service: Optional[SmartSheetQueryService] = None
    ) -> None:
        self._candidate_service = candidate_service or SmartSheetQueryService()

    async def handle(
        self,
        session: AsyncSession,
        *,
        event: str,
        identity: DraftIdentity,
        draft_id: str = "",
        fields_patch: Optional[dict[str, Any]] = None,
        source_text: str = "",
        idempotency_key: str = "",
        force_new: bool = False,
    ) -> BugAssistantDecision:
        normalized_event = (event or "").strip().upper()
        if normalized_event not in SUPPORTED_EVENTS:
            raise InvalidBugAssistantEvent(normalized_event or "<empty>")

        if normalized_event in {"START_REPORT", "PATCH_REPORT"}:
            return await self._collect_and_match(
                session,
                event=normalized_event,
                identity=identity,
                draft_id=draft_id,
                fields_patch=fields_patch or {},
                source_text=source_text,
                idempotency_key=idempotency_key,
                force_new=force_new,
            )

        storage_identity = v2_storage_identity(identity)
        draft = await bugtrack_service.resolve_draft(
            session, identity=storage_identity, draft_id=draft_id
        )
        if draft is None:
            raise InvalidBugAssistantTransition(normalized_event, "missing_draft")
        if source_text:
            draft = await bugtrack_service.ensure_draft(
                session,
                identity=storage_identity,
                draft_id=draft.id,
                fields_patch={},
                source_text=source_text,
                intent=normalized_event,
                idempotency_key=idempotency_key,
                event_type="assistant_event_received",
            )

        if normalized_event == "CONFIRM_MATCH":
            return await self._confirm_match(session, draft, identity)
        if normalized_event == "REJECT_MATCH":
            return await self._reject_match(session, draft)
        if normalized_event == "CONFIRM_SUBMIT":
            return await self._confirm_submit(session, draft, identity)
        if normalized_event == "SUSPEND":
            return await self._suspend(session, draft)
        if normalized_event == "RESUME":
            return await self._resume(session, draft)
        return await self._cancel(session, draft)

    async def _collect_and_match(
        self,
        session: AsyncSession,
        *,
        event: str,
        identity: DraftIdentity,
        draft_id: str,
        fields_patch: dict[str, Any],
        source_text: str,
        idempotency_key: str,
        force_new: bool,
    ) -> BugAssistantDecision:
        storage_identity = v2_storage_identity(identity)
        existing = await bugtrack_service.resolve_draft(
            session, identity=storage_identity, draft_id=draft_id
        )
        if event == "START_REPORT" and existing is not None:
            if (
                existing.status in TERMINAL_STATUSES
                or existing.flow_state in IMMUTABLE_FLOW_STATES
            ):
                force_new = True

        normalized_patch = dict(fields_patch)
        if (
            event == "PATCH_REPORT"
            and existing is not None
            and source_text.strip()
            and not str(normalized_patch.get("operation_description") or "").strip()
        ):
            current = existing.operation_description.strip()
            supplement = source_text.strip()
            normalized_patch["operation_description"] = (
                f"{current}\n补充：{supplement}" if current else supplement
            )

        draft = await bugtrack_service.ensure_draft(
            session,
            identity=storage_identity,
            draft_id=draft_id,
            force_new=force_new,
            fields_patch=normalized_patch,
            source_text=source_text,
            intent=event,
            idempotency_key=idempotency_key,
            event_type="assistant_report_started"
            if event == "START_REPORT"
            else "assistant_report_patched",
        )
        if draft.flow_state in IMMUTABLE_FLOW_STATES:
            raise InvalidBugAssistantTransition(event, draft.flow_state)

        missing = _missing_fields(draft)
        if missing:
            await bugtrack_service.transition(
                session,
                draft,
                event_type="assistant_information_required",
                flow_state="collecting",
                actor="bug_assistant_v2",
                data={"missing_fields": missing},
            )
            return BugAssistantDecision(
                draft_id=str(draft.id),
                state="collecting",
                next_action="REQUEST_INFORMATION",
                missing_fields=missing,
            )

        await bugtrack_service.transition(
            session,
            draft,
            event_type="assistant_candidate_search_started",
            flow_state="matching",
            actor="bug_assistant_v2",
        )
        try:
            candidate = await self._best_candidate(draft)
        except Exception as exc:
            logger.warning(
                "[bug-assistant-v2] candidate search failed draft=%s error=%s",
                str(draft.id),
                str(exc)[:200],
            )
            await bugtrack_service.transition(
                session,
                draft,
                event_type="assistant_candidate_search_failed",
                flow_state="collecting",
                actor="bug_assistant_v2",
                data={"error": str(exc)[:500]},
            )
            return BugAssistantDecision(
                draft_id=str(draft.id),
                state="collecting",
                next_action="RETRY_MATCHING",
            )

        if candidate:
            draft.matched_record_id = str(candidate.get("external_record_id") or "")
            draft.matched_snapshot = dict(candidate)
            await bugtrack_service.transition(
                session,
                draft,
                event_type="assistant_candidate_presented",
                flow_state="awaiting_match_confirmation",
                actor="bug_assistant_v2",
                data={"candidate": candidate},
            )
            return BugAssistantDecision(
                draft_id=str(draft.id),
                state="awaiting_match_confirmation",
                next_action="CONFIRM_MATCH",
                candidate=candidate,
            )

        draft.matched_record_id = ""
        draft.matched_snapshot = {}
        await bugtrack_service.transition(
            session,
            draft,
            event_type="assistant_no_candidate",
            flow_state="ready_to_submit",
            actor="bug_assistant_v2",
        )
        return BugAssistantDecision(
            draft_id=str(draft.id),
            state="ready_to_submit",
            next_action="CONFIRM_SUBMIT",
        )

    async def _best_candidate(self, draft: BugDraft) -> dict[str, Any]:
        records = await self._candidate_service.search_by_feedback(
            draft.search_keyword,
            module=draft.module,
            op_desc=draft.operation_description,
            limit=5,
        )
        candidates: list[dict[str, Any]] = []
        for record in records:
            summary = self._candidate_service.record_to_summary(record)
            score = round(
                self._candidate_service.feedback_score(
                    record,
                    keyword=draft.search_keyword,
                    module=draft.module,
                    op_desc=draft.operation_description,
                ),
                2,
            )
            similarity_method = getattr(
                self._candidate_service, "operation_similarity", None
            )
            common_method = getattr(
                self._candidate_service, "operation_common_chars", None
            )
            operation_similarity = (
                round(
                    float(
                        similarity_method(record, op_desc=draft.operation_description)
                    ),
                    4,
                )
                if callable(similarity_method)
                else 0.0
            )
            operation_common_chars = (
                int(common_method(record, op_desc=draft.operation_description))
                if callable(common_method)
                else 0
            )
            candidates.append(
                {
                    "external_record_id": str(summary.get("record_id") or ""),
                    "module": str(summary.get("module") or ""),
                    "operation_description": str(summary.get("op_desc") or ""),
                    "status": str(summary.get("dev_status") or ""),
                    "reply": str(summary.get("reply") or ""),
                    "result": str(summary.get("result") or ""),
                    "match_score": score,
                    "match_threshold": EXISTING_ISSUE_SCORE_THRESHOLD,
                    "operation_similarity": operation_similarity,
                    "operation_common_chars": operation_common_chars,
                }
            )
        qualified = [
            item
            for item in candidates
            if item["external_record_id"]
            and (
                item["match_score"] >= EXISTING_ISSUE_SCORE_THRESHOLD
                or (
                    not draft.module.strip()
                    and len(
                        "".join(
                            character
                            for character in draft.operation_description.lower()
                            if character.isalnum()
                        )
                    )
                    >= EXPLICIT_CONFIRMATION_MIN_COMMON_CHARS
                    and item["operation_similarity"]
                    >= EXPLICIT_CONFIRMATION_SIMILARITY_THRESHOLD
                    and item["operation_common_chars"]
                    >= EXPLICIT_CONFIRMATION_MIN_COMMON_CHARS
                )
            )
        ]
        return max(qualified, key=lambda item: item["match_score"], default={})

    async def _confirm_match(
        self, session: AsyncSession, draft: BugDraft, identity: DraftIdentity
    ) -> BugAssistantDecision:
        if draft.flow_state == "linked_existing":
            return await self._existing_report_decision(session, draft, "COMPLETED")
        if (
            draft.flow_state != "awaiting_match_confirmation"
            or not draft.matched_record_id
        ):
            raise InvalidBugAssistantTransition("CONFIRM_MATCH", draft.flow_state)

        issue = await self._ensure_external_issue(session, draft)
        report = await self._ensure_report(
            session,
            draft=draft,
            issue=issue,
            status="linked_existing",
            link_type="confirmed_duplicate",
            external_record_id=draft.matched_record_id,
            submitted=True,
        )
        await self._ensure_subscription(session, issue, identity)
        draft.submitted_at = draft.submitted_at or utcnow()
        await bugtrack_service.transition(
            session,
            draft,
            event_type="assistant_existing_issue_linked",
            flow_state="linked_existing",
            status="submitted",
            actor="bug_assistant_v2",
            data={"issue_id": str(issue.id), "report_id": str(report.id)},
        )
        return BugAssistantDecision(
            draft_id=str(draft.id),
            state="linked_existing",
            next_action="COMPLETED",
            candidate=dict(draft.matched_snapshot or {}),
            issue_id=str(issue.id),
            report_id=str(report.id),
        )

    async def _reject_match(
        self, session: AsyncSession, draft: BugDraft
    ) -> BugAssistantDecision:
        if draft.flow_state != "awaiting_match_confirmation":
            raise InvalidBugAssistantTransition("REJECT_MATCH", draft.flow_state)
        draft.matched_record_id = ""
        draft.matched_snapshot = {}
        await bugtrack_service.transition(
            session,
            draft,
            event_type="assistant_candidate_rejected",
            flow_state="ready_to_submit",
            actor="bug_assistant_v2",
        )
        return BugAssistantDecision(
            draft_id=str(draft.id),
            state="ready_to_submit",
            next_action="CONFIRM_SUBMIT",
        )

    async def _confirm_submit(
        self, session: AsyncSession, draft: BugDraft, identity: DraftIdentity
    ) -> BugAssistantDecision:
        if draft.flow_state in {"queued_for_submission", "submitted"}:
            return await self._existing_report_decision(session, draft, "WAIT_FOR_SYNC")
        if draft.flow_state != "ready_to_submit":
            raise InvalidBugAssistantTransition("CONFIRM_SUBMIT", draft.flow_state)

        issue = BugIssue(
            source_system="local",
            title=(draft.operation_description or draft.module or "New issue")[:255],
            module=draft.module,
            normalized_description=draft.operation_description,
            environment=draft.environment,
            issue_type=draft.issue_type or "bug",
            status="pending_sync",
            external_snapshot={},
        )
        session.add(issue)
        await session.flush()
        report = await self._ensure_report(
            session,
            draft=draft,
            issue=issue,
            status="queued_for_submission",
            link_type="new_issue",
        )
        await self._ensure_subscription(session, issue, identity)
        await bugtrack_service.prepare_outbox(
            session,
            draft=draft,
            operation="create_issue_v2",
            payload={
                "issue_id": str(issue.id),
                "report_id": str(report.id),
                "draft": _draft_snapshot(draft),
            },
        )
        await bugtrack_service.transition(
            session,
            draft,
            event_type="assistant_submission_queued",
            flow_state="queued_for_submission",
            status="active",
            actor="bug_assistant_v2",
            data={"issue_id": str(issue.id), "report_id": str(report.id)},
        )
        return BugAssistantDecision(
            draft_id=str(draft.id),
            state="queued_for_submission",
            next_action="WAIT_FOR_SYNC",
            issue_id=str(issue.id),
            report_id=str(report.id),
        )

    async def _suspend(
        self, session: AsyncSession, draft: BugDraft
    ) -> BugAssistantDecision:
        if draft.flow_state == "suspended":
            return BugAssistantDecision(
                draft_id=str(draft.id),
                state="suspended",
                next_action="HANDOFF_QA",
            )
        if draft.flow_state in IMMUTABLE_FLOW_STATES:
            raise InvalidBugAssistantTransition("SUSPEND", draft.flow_state)
        await bugtrack_service.transition(
            session,
            draft,
            event_type="assistant_suspended",
            flow_state="suspended",
            actor="bug_assistant_v2",
        )
        return BugAssistantDecision(
            draft_id=str(draft.id), state="suspended", next_action="HANDOFF_QA"
        )

    async def _resume(
        self, session: AsyncSession, draft: BugDraft
    ) -> BugAssistantDecision:
        if draft.flow_state != "suspended":
            raise InvalidBugAssistantTransition("RESUME", draft.flow_state)
        event = (
            await session.execute(
                select(BugStateEvent)
                .where(
                    BugStateEvent.draft_id == draft.id,
                    BugStateEvent.event_type == "assistant_suspended",
                )
                .order_by(BugStateEvent.created_at.desc(), BugStateEvent.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        previous = (event.from_state if event is not None else "collecting") or "collecting"
        next_actions = {
            "collecting": "REQUEST_INFORMATION",
            "matching": "RETRY_MATCHING",
            "awaiting_match_confirmation": "CONFIRM_MATCH",
            "ready_to_submit": "CONFIRM_SUBMIT",
        }
        if previous not in next_actions:
            previous = "collecting"
        await bugtrack_service.transition(
            session,
            draft,
            event_type="assistant_resumed",
            flow_state=previous,
            actor="bug_assistant_v2",
        )
        return BugAssistantDecision(
            draft_id=str(draft.id),
            state=previous,
            next_action=next_actions[previous],
            candidate=dict(draft.matched_snapshot or {}),
        )

    async def _cancel(
        self, session: AsyncSession, draft: BugDraft
    ) -> BugAssistantDecision:
        if draft.flow_state == "abandoned":
            return BugAssistantDecision(
                draft_id=str(draft.id), state="abandoned", next_action="COMPLETED"
            )
        if draft.flow_state in IMMUTABLE_FLOW_STATES:
            raise InvalidBugAssistantTransition("CANCEL", draft.flow_state)
        await bugtrack_service.transition(
            session,
            draft,
            event_type="assistant_abandoned",
            flow_state="abandoned",
            status="abandoned",
            actor="bug_assistant_v2",
        )
        return BugAssistantDecision(
            draft_id=str(draft.id), state="abandoned", next_action="COMPLETED"
        )

    async def _ensure_external_issue(
        self, session: AsyncSession, draft: BugDraft
    ) -> BugIssue:
        statement = select(BugIssue).where(
            BugIssue.source_system == "feishu",
            BugIssue.external_record_id == draft.matched_record_id,
        )
        issue = (await session.execute(statement)).scalar_one_or_none()
        candidate = dict(draft.matched_snapshot or {})
        if issue is None:
            description = str(candidate.get("operation_description") or "")
            issue = BugIssue(
                source_system="feishu",
                external_record_id=draft.matched_record_id,
                title=(description or str(candidate.get("module") or "Existing issue"))[
                    :255
                ],
                module=str(candidate.get("module") or ""),
                normalized_description=description,
                environment=draft.environment,
                issue_type=draft.issue_type or "bug",
                status=str(candidate.get("status") or "unknown"),
                external_snapshot=candidate,
            )
            session.add(issue)
            await session.flush()
        else:
            issue.external_snapshot = candidate
            issue.status = str(candidate.get("status") or issue.status)
        return issue

    async def _ensure_report(
        self,
        session: AsyncSession,
        *,
        draft: BugDraft,
        issue: BugIssue,
        status: str,
        link_type: str,
        external_record_id: str = "",
        submitted: bool = False,
    ) -> BugReport:
        statement = select(BugReport).where(BugReport.draft_id == draft.id)
        report = (await session.execute(statement)).scalar_one_or_none()
        if report is None:
            report = BugReport(draft_id=draft.id)
            session.add(report)
        report.issue_id = issue.id
        report.status = status
        report.link_type = link_type
        report.external_record_id = external_record_id
        report.report_snapshot = _draft_snapshot(draft)
        if submitted:
            report.submitted_at = report.submitted_at or utcnow()
        await session.flush()
        return report

    async def _ensure_subscription(
        self,
        session: AsyncSession,
        issue: BugIssue,
        identity: DraftIdentity,
    ) -> Optional[BugSubscription]:
        subscriber_key = (identity.user_key or identity.session_id).strip()
        if not subscriber_key:
            return None
        channel = (identity.channel or "unknown").strip()
        statement = select(BugSubscription).where(
            BugSubscription.issue_id == issue.id,
            BugSubscription.channel == channel,
            BugSubscription.subscriber_key == subscriber_key,
        )
        subscription = (await session.execute(statement)).scalar_one_or_none()
        if subscription is None:
            subscription = BugSubscription(
                issue_id=issue.id,
                channel=channel,
                subscriber_key=subscriber_key,
                user_key=identity.user_key,
                session_id=identity.session_id,
            )
            session.add(subscription)
        else:
            subscription.status = "active"
            subscription.user_key = identity.user_key or subscription.user_key
            subscription.session_id = identity.session_id or subscription.session_id
        await session.flush()
        return subscription

    async def _existing_report_decision(
        self, session: AsyncSession, draft: BugDraft, next_action: str
    ) -> BugAssistantDecision:
        statement = select(BugReport).where(BugReport.draft_id == draft.id)
        report = (await session.execute(statement)).scalar_one_or_none()
        return BugAssistantDecision(
            draft_id=str(draft.id),
            state=draft.flow_state,
            next_action=next_action,
            candidate=dict(draft.matched_snapshot or {}),
            issue_id=str(report.issue_id) if report and report.issue_id else "",
            report_id=str(report.id) if report else "",
        )


bug_assistant_orchestrator = BugAssistantOrchestrator()


__all__ = [
    "BugAssistantDecision",
    "BugAssistantOrchestrator",
    "InvalidBugAssistantEvent",
    "InvalidBugAssistantTransition",
    "bug_assistant_orchestrator",
    "v2_storage_identity",
]
