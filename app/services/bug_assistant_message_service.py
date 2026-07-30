"""Raw channel-message adapter for the active Bug assistant v2 path."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
import hashlib
import logging
from typing import Any

from app.core.database import session_scope
from app.models.bugtrack_db import BugDraft
from app.services.bug_assistant_orchestrator import (
    BugAssistantDecision,
    bug_assistant_orchestrator,
    v2_storage_identity,
)
from app.services.bug_issue_sync_service import (
    BugIssueSyncError,
    bug_issue_sync_service,
)
from app.services.bugtrack_service import DraftIdentity, bugtrack_service
from app.services.charge_reply_policy import get_charge_reply_policy
from app.services.customer_intent import classify_customer_intent


logger = logging.getLogger(__name__)

_CONFIRM_TEXTS = {
    "确认",
    "确认提交",
    "提交",
    "是",
    "是的",
    "对",
    "对的",
    "可以",
    "没问题",
    "确认记录",
    "确认相同",
    "是同一个",
    "是同一个问题",
    "同一个",
    "同一个问题",
}
_REJECT_MATCH_TEXTS = {
    "不是",
    "不是的",
    "不一样",
    "不是同一个",
    "不是同一个问题",
    "不同问题",
    "新问题",
    "no",
}
_REJECT_MATCH_MARKERS = ("不是同一个", "不是这个", "不一样", "不同问题", "新问题")
_CONFIRM_MATCH_MARKERS = ("确认相同", "是同一个", "同一个问题")
_CANCEL_WORDS = ("取消", "算了", "不报了", "不用提交", "放弃")
_QA_SWITCH_WORDS = {
    "先查询解决方法",
    "查询解决方法",
    "先查解决方法",
    "转知识库",
    "问一下怎么解决",
}
_FINAL_STATES = {"submitted", "linked_existing", "abandoned"}


@dataclass(frozen=True)
class BugAssistantMessageResult:
    assistant_text: str
    state: str
    draft_id: str = ""
    issue_id: str = ""
    report_id: str = ""
    record_id: str = ""
    continue_session: bool = False
    fallback_required: bool = False
    fallback_text: str = ""
    sync_pending: bool = False
    candidate: dict[str, Any] = field(default_factory=dict)
    intent: dict[str, Any] = field(default_factory=dict)
    actions: list[dict[str, str]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "success": True,
            "assistant_text": self.assistant_text,
            "state": self.state,
            "draft_id": self.draft_id,
            "issue_id": self.issue_id,
            "report_id": self.report_id,
            "record_id": self.record_id,
            "continue_session": self.continue_session,
            "fallback_required": self.fallback_required,
            "fallback_text": self.fallback_text,
            "sync_pending": self.sync_pending,
            "candidate": dict(self.candidate),
            "intent": dict(self.intent),
            "actions": [dict(item) for item in self.actions],
        }


class BugAssistantMessageService:
    def __init__(self, orchestrator=None, sync_service=None) -> None:
        self._orchestrator = orchestrator or bug_assistant_orchestrator
        self._sync_service = sync_service or bug_issue_sync_service

    async def process(self, **kwargs: Any) -> BugAssistantMessageResult:
        """Process one message and attach the stable top-level intent contract."""

        intent = classify_customer_intent(
            get_charge_reply_policy(),
            text=str(kwargs.get("text") or ""),
            language=str(kwargs.get("language") or ""),
            has_attachments=bool(kwargs.get("image_bytes")),
        )
        result = await self._process(**kwargs)
        if result.intent:
            return result
        return replace(result, intent=intent.to_dict())

    async def _process(
        self,
        *,
        channel: str,
        user_key: str,
        session_id: str,
        text: str = "",
        language: str = "",
        message_id: str = "",
        image_bytes: bytes | None = None,
        image_name: str = "",
        image_mime: str = "",
        source_file_id: str = "",
        event: str = "",
    ) -> BugAssistantMessageResult:
        identity = DraftIdentity(
            channel=(channel or "unknown").strip(),
            user_key=(user_key or "").strip(),
            session_id=(session_id or "").strip(),
        )
        storage_identity = v2_storage_identity(identity)
        current = await self._current_draft(storage_identity)
        current_state = current.flow_state if current is not None else ""
        event = (event or "").strip().upper() or self._event_for(current_state, text)
        idempotency_key = message_id.strip() or self._idempotency_key(
            identity=identity,
            event=event,
            text=text,
            image_bytes=image_bytes,
            current_state=current_state,
        )

        if event in {"CONFIRM_SUBMIT", "CONFIRM_MATCH", "REJECT_MATCH"} and image_bytes:
            if current is None:
                return self._retry_attachment_result(language, state=current_state)
            try:
                await self._attach(
                    current,
                    image_bytes=image_bytes,
                    image_name=image_name,
                    image_mime=image_mime,
                    source_file_id=source_file_id,
                )
            except Exception as exc:
                logger.warning(
                    "[bug-assistant-v2] confirmation attachment failed draft=%s: %s",
                    str(current.id),
                    str(exc)[:200],
                )
                return self._retry_attachment_result(
                    language, str(current.id), state=current_state
                )

        fields_patch: dict[str, str] = {}
        if event == "START_REPORT" and text.strip():
            fields_patch = {
                "operation_description": text.strip(),
                "search_keyword": text.strip()[:80],
            }

        async with session_scope() as session:
            decision = await self._orchestrator.handle(
                session,
                event=event,
                identity=identity,
                fields_patch=fields_patch,
                source_text=text.strip(),
                idempotency_key=idempotency_key,
            )

        if event in {"START_REPORT", "PATCH_REPORT"} and image_bytes:
            try:
                draft = await self._draft_by_id(decision.draft_id)
                if draft is None:
                    raise RuntimeError("draft not found after message")
                await self._attach(
                    draft,
                    image_bytes=image_bytes,
                    image_name=image_name,
                    image_mime=image_mime,
                    source_file_id=source_file_id,
                )
            except Exception as exc:
                logger.warning(
                    "[bug-assistant-v2] attachment failed draft=%s: %s",
                    decision.draft_id,
                    str(exc)[:200],
                )
                return BugAssistantMessageResult(
                    assistant_text=self._text(
                        language,
                        zh="截图未保存成功，本次尚未提交。请重新上传，或稍后重试。",
                        en="The screenshot was not saved. Please upload it again.",
                        vi="Ảnh chụp chưa được lưu. Vui lòng tải lên lại.",
                    ),
                    state=decision.state,
                    draft_id=decision.draft_id,
                    continue_session=True,
                )

        if decision.next_action == "RETRY_MATCHING":
            return BugAssistantMessageResult(
                assistant_text=self._text(
                    language,
                    zh="暂时无法完成已有问题查重，本次反馈已保留。请稍后重试。",
                    en="Matching is temporarily unavailable. Your report is saved; please retry later.",
                    vi="Tạm thời chưa thể đối chiếu. Phản hồi đã được lưu; vui lòng thử lại sau.",
                ),
                state=decision.state,
                draft_id=decision.draft_id,
                continue_session=True,
            )
        if decision.next_action == "HANDOFF_QA":
            return BugAssistantMessageResult(
                assistant_text=self._text(
                    language,
                    zh="问题反馈草稿已暂停并保留。你可以先查询解决方法，稍后再继续反馈。",
                    en="The Bug draft is paused and saved. You can ask a knowledge question and resume later.",
                    vi="Bản nháp báo lỗi đã được tạm dừng và lưu lại. Bạn có thể hỏi trước rồi tiếp tục sau.",
                ),
                state=decision.state,
                draft_id=decision.draft_id,
                actions=self._draft_actions(language, suspended=True),
            )
        if decision.state == "queued_for_submission":
            return await self._sync_submission(decision, language)
        return await self._render(decision, language)

    async def _current_draft(self, identity: DraftIdentity) -> BugDraft | None:
        async with session_scope() as session:
            return await bugtrack_service.resolve_draft(session, identity=identity)

    async def _draft_by_id(self, draft_id: str) -> BugDraft | None:
        async with session_scope() as session:
            return await bugtrack_service.get_draft(session, draft_id)

    async def _draft_description(self, draft_id: str) -> str:
        draft = await self._draft_by_id(draft_id)
        return draft.operation_description if draft is not None else ""

    async def _attach(
        self,
        draft: BugDraft,
        *,
        image_bytes: bytes,
        image_name: str,
        image_mime: str,
        source_file_id: str,
    ) -> None:
        async with session_scope() as session:
            current = await bugtrack_service.get_draft(session, str(draft.id))
            if current is None:
                raise RuntimeError("draft not found")
            await bugtrack_service.add_attachment(
                session,
                draft=current,
                content=image_bytes,
                original_name=image_name or "bug-screenshot.png",
                mime_type=image_mime or "application/octet-stream",
                source_file_id=source_file_id,
            )

    async def _sync_submission(
        self, decision: BugAssistantDecision, language: str
    ) -> BugAssistantMessageResult:
        try:
            synced = await self._sync_service.sync(decision.draft_id)
        except BugIssueSyncError as exc:
            self._schedule_retry(decision.draft_id)
            logger.warning(
                "[bug-assistant-v2] queued sync pending draft=%s error=%s",
                decision.draft_id,
                str(exc)[:200],
            )
            return BugAssistantMessageResult(
                assistant_text=self._text(
                    language,
                    zh="反馈已安全保存，正在同步问题表。同步完成后会继续跟进。",
                    en="Your report is safely saved and is being synchronized.",
                    vi="Phản hồi đã được lưu và đang được đồng bộ.",
                ),
                state="queued_for_submission",
                draft_id=decision.draft_id,
                issue_id=decision.issue_id,
                report_id=decision.report_id,
                sync_pending=True,
            )
        return BugAssistantMessageResult(
            assistant_text=self._text(
                language,
                zh=f"反馈已记录，编号：{synced.record_id}。我们会继续跟进处理进度。",
                en=f"Your report has been recorded as {synced.record_id}.",
                vi=f"Phản hồi đã được ghi nhận với mã {synced.record_id}.",
            ),
            state="submitted",
            draft_id=synced.draft_id,
            issue_id=synced.issue_id,
            report_id=synced.report_id,
            record_id=synced.record_id,
        )

    async def _render(
        self, decision: BugAssistantDecision, language: str
    ) -> BugAssistantMessageResult:
        if decision.state == "collecting":
            text = self._text(
                language,
                zh="请补充具体出现了什么问题，以及执行什么操作后出现。",
                en="Please describe what happened and which action triggered it.",
                vi="Vui lòng mô tả sự cố và thao tác đã gây ra sự cố.",
            )
            return BugAssistantMessageResult(
                assistant_text=text,
                state=decision.state,
                draft_id=decision.draft_id,
                continue_session=True,
                actions=self._draft_actions(language),
            )
        if decision.state == "ready_to_submit":
            description = await self._draft_description(decision.draft_id)
            text = self._text(
                language,
                zh=f"我已整理本次问题：{description}\n确认提交吗？请回复“确认提交”，或继续补充信息。",
                en=(
                    f"Issue summary: {description}\n"
                    "Reply 'confirm' to submit or add more details."
                ),
                vi=(
                    f"Tóm tắt sự cố: {description}\n"
                    "Trả lời 'xác nhận' để gửi hoặc bổ sung thông tin."
                ),
            )
            return BugAssistantMessageResult(
                assistant_text=text,
                state=decision.state,
                draft_id=decision.draft_id,
                continue_session=True,
                actions=self._confirm_submit_actions(language),
            )
        if decision.state == "awaiting_match_confirmation":
            candidate = dict(decision.candidate or {})
            module = str(candidate.get("module") or "未标注模块")
            description = str(candidate.get("operation_description") or "")
            status = str(candidate.get("status") or "待确认")
            reply = str(candidate.get("reply") or "")
            result = str(candidate.get("result") or "")
            progress = "；".join(part for part in (status, reply, result) if part)
            text = self._text(
                language,
                zh=(
                    "我找到一个可能相同的已有问题：\n"
                    f"模块：{module}\n问题：{description}\n当前进度：{progress or '待确认'}\n"
                    "如果是同一个问题，请回复“确认相同”；如果不是，请回复“不是同一个”。"
                ),
                en=(
                    "I found a possibly matching issue:\n"
                    f"Module: {module}\nIssue: {description}\nStatus: {progress or 'Pending'}\n"
                    "Reply 'confirm' if it is the same issue, or 'no' if it is different."
                ),
                vi=(
                    "Tôi tìm thấy một sự cố có thể trùng khớp:\n"
                    f"Mô-đun: {module}\nSự cố: {description}\nTrạng thái: {progress or 'Đang chờ'}\n"
                    "Trả lời 'xác nhận' nếu cùng một sự cố, hoặc 'no' nếu khác."
                ),
            )
            return BugAssistantMessageResult(
                assistant_text=text,
                state=decision.state,
                draft_id=decision.draft_id,
                continue_session=True,
                candidate=candidate,
                actions=self._confirm_match_actions(language),
            )
        if decision.state == "linked_existing":
            candidate = dict(decision.candidate or {})
            status = str(candidate.get("status") or "待处理")
            record_id = str(candidate.get("external_record_id") or "")
            return BugAssistantMessageResult(
                assistant_text=self._text(
                    language,
                    zh=(
                        "已关联到现有问题，本次反馈已作为独立报告保存，不会覆盖原问题记录。" f"当前进度：{status}。后续状态变化会通知您。"
                    ),
                    en=(
                        "Linked to the existing issue. Your occurrence was saved as a "
                        f"separate report. Current status: {status}. You will receive updates."
                    ),
                    vi=(
                        "Đã liên kết với sự cố hiện có và lưu phản hồi này thành báo cáo riêng. "
                        f"Trạng thái hiện tại: {status}. Bạn sẽ nhận được cập nhật."
                    ),
                ),
                state=decision.state,
                draft_id=decision.draft_id,
                issue_id=decision.issue_id,
                report_id=decision.report_id,
                record_id=record_id,
                candidate=candidate,
            )
        if decision.state == "abandoned":
            return BugAssistantMessageResult(
                assistant_text=self._text(
                    language,
                    zh="已取消本次问题反馈。",
                    en="The report has been cancelled.",
                    vi="Phản hồi đã được hủy.",
                ),
                state=decision.state,
                draft_id=decision.draft_id,
            )
        return BugAssistantMessageResult(
            assistant_text=self._text(
                language,
                zh="本次反馈状态已更新。",
                en="The report status has been updated.",
                vi="Trạng thái phản hồi đã được cập nhật.",
            ),
            state=decision.state,
            draft_id=decision.draft_id,
            continue_session=decision.state not in _FINAL_STATES,
            actions=(
                self._confirm_submit_actions(language)
                if decision.next_action == "CONFIRM_SUBMIT"
                else self._confirm_match_actions(language)
                if decision.next_action == "CONFIRM_MATCH"
                else self._draft_actions(language)
            ),
        )

    @staticmethod
    def _event_for(state: str, text: str) -> str:
        normalized = "".join((text or "").strip().lower().split())
        if normalized in _QA_SWITCH_WORDS:
            return "SUSPEND"
        if state == "awaiting_match_confirmation":
            if any(word in normalized for word in _CANCEL_WORDS):
                return "CANCEL"
            if normalized in _REJECT_MATCH_TEXTS or any(
                marker in normalized for marker in _REJECT_MATCH_MARKERS
            ):
                return "REJECT_MATCH"
            if (
                normalized in _CONFIRM_TEXTS
                or any(marker in normalized for marker in _CONFIRM_MATCH_MARKERS)
                or normalized
                in {
                    "confirm",
                    "yes",
                    "xácnhận",
                }
            ):
                return "CONFIRM_MATCH"
            return "PATCH_REPORT"
        if state == "ready_to_submit":
            if any(word in normalized for word in _CANCEL_WORDS):
                return "CANCEL"
            if normalized in _CONFIRM_TEXTS or normalized in {
                "confirm",
                "submit",
                "yes",
                "xácnhận",
            }:
                return "CONFIRM_SUBMIT"
            return "PATCH_REPORT"
        if state in {"collecting", "matching", "suspended"}:
            if state == "suspended" and normalized in {
                "继续反馈",
                "恢复反馈",
                "继续提交",
                "resume",
            }:
                return "RESUME"
            return "PATCH_REPORT"
        return "START_REPORT"

    @staticmethod
    def _idempotency_key(
        *,
        identity: DraftIdentity,
        event: str,
        text: str,
        image_bytes: bytes | None,
        current_state: str,
    ) -> str:
        image_digest = hashlib.sha256(image_bytes or b"").hexdigest()
        material = "|".join(
            [
                identity.channel,
                identity.user_key,
                identity.session_id,
                current_state,
                event,
                text.strip(),
                image_digest,
            ]
        )
        return "message-v2-" + hashlib.sha256(material.encode("utf-8")).hexdigest()

    @staticmethod
    def _schedule_retry(draft_id: str) -> None:
        try:
            from app.tasks.bugtrack_tasks import bugtrack_sync_v2_issue

            bugtrack_sync_v2_issue.apply_async(args=[draft_id], queue="wecom_timers")
        except Exception as exc:
            logger.error(
                "[bug-assistant-v2] failed to schedule retry draft=%s: %s",
                draft_id,
                str(exc)[:200],
            )

    @staticmethod
    def _retry_attachment_result(
        language: str, draft_id: str = "", state: str = "ready_to_submit"
    ) -> BugAssistantMessageResult:
        return BugAssistantMessageResult(
            assistant_text=BugAssistantMessageService._text(
                language,
                zh="截图未保存成功，本次尚未提交。请重新上传后再确认。",
                en="The screenshot was not saved. Upload it again before confirming.",
                vi="Ảnh chưa được lưu. Vui lòng tải lại trước khi xác nhận.",
            ),
            state=state or "ready_to_submit",
            draft_id=draft_id,
            continue_session=True,
        )

    @staticmethod
    def _text(language: str, *, zh: str, en: str, vi: str) -> str:
        normalized = (language or "").strip().lower()
        if normalized.startswith("en") or "英文" in normalized:
            return en
        if normalized.startswith("vi") or "越南" in normalized:
            return vi
        return zh

    @classmethod
    def _draft_actions(cls, language: str, *, suspended: bool = False) -> list[dict[str, str]]:
        if suspended:
            return [
                {"id": "bug.resume", "label": "继续反馈", "style": "primary"},
                {"id": "bug.cancel", "label": "取消反馈", "style": "secondary"},
            ]
        return [
            {"id": "bug.suspend", "label": "先查询解决方法", "style": "secondary"},
            {"id": "bug.cancel", "label": "取消反馈", "style": "secondary"},
        ]

    @staticmethod
    def _confirm_submit_actions(language: str) -> list[dict[str, str]]:
        return [
            {"id": "bug.confirm_submit", "label": "确认提交", "style": "primary"},
            {"id": "bug.suspend", "label": "先查询解决方法", "style": "secondary"},
            {"id": "bug.cancel", "label": "取消", "style": "secondary"},
        ]

    @staticmethod
    def _confirm_match_actions(language: str) -> list[dict[str, str]]:
        return [
            {"id": "bug.confirm_match", "label": "是同一个问题", "style": "primary"},
            {"id": "bug.reject_match", "label": "不是同一个", "style": "secondary"},
            {"id": "bug.suspend", "label": "先查询解决方法", "style": "secondary"},
        ]


bug_assistant_message_service = BugAssistantMessageService()


__all__ = [
    "BugAssistantMessageResult",
    "BugAssistantMessageService",
    "bug_assistant_message_service",
]
