"""Structured top-level intent contract for the charge customer service."""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any


@dataclass(frozen=True)
class IntentDecision:
    intent: str
    confidence: float
    entities: dict[str, str] = field(default_factory=dict)
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "intent": self.intent,
            "confidence": round(max(0.0, min(float(self.confidence), 1.0)), 2),
            "entities": dict(self.entities),
            "reason": self.reason,
        }


def _entities(text: str) -> dict[str, str]:
    query = (text or "").strip()
    lowered = query.lower()
    entities = {"module": "", "record_id": "", "environment": ""}
    module_terms = (
        ("订单管理", ("订单", "结算", "退款", "支付")),
        ("计费管理", ("计费", "费率", "价格模板")),
        ("设备管理", ("设备", "充电桩", "白名单")),
        ("场地管理", ("场地", "站点")),
    )
    for module, terms in module_terms:
        if any(term in lowered for term in terms):
            entities["module"] = module
            break
    if any(term in lowered for term in ("pc后台", "web后台", "网页后台", "chrome")):
        entities["environment"] = "Web后台"
    elif any(term in lowered for term in ("小程序", "微信端")):
        entities["environment"] = "小程序"
    elif any(term in lowered for term in ("app", "应用端", "手机端")):
        entities["environment"] = "App"
    record = re.search(r"\b(?:BUG[-_]?\d+|rec[A-Za-z0-9_-]+)\b", query, re.IGNORECASE)
    if record:
        entities["record_id"] = record.group(0)
    return entities


def classify_customer_intent(
    policy: Any,
    *,
    text: str,
    language: str = "",
    has_attachments: bool = False,
) -> IntentDecision:
    """Classify only the stable top-level routes; downstream state stays deterministic."""

    query = (text or "").strip()
    entities = _entities(query)
    if not query and has_attachments:
        return IntentDecision("bug_report", 0.72, entities, "attachment_only")

    policy_reply = policy.evaluate(
        text=query,
        language=language,
        active_app="A",
        has_attachments=has_attachments,
        vague_count=0,
        vague_exhausted=False,
    )
    target = policy.route_target(
        text=query, active_app="A", has_attachments=has_attachments
    )
    if policy.is_progress_query(query):
        return IntentDecision("bug_progress", 0.97, entities, "progress_terms")

    verified_qa = bool(
        policy_reply is not None
        and (
            policy_reply.route.startswith("verified_")
            or policy_reply.route in {
                "security_refusal",
                "non_bug_code_info",
            }
        )
    )
    explicit_qa = policy.blocks_bug_route(query)
    if target == "B" and verified_qa:
        return IntentDecision("mixed", 0.88, entities, "verified_qa_and_bug")
    if target == "B":
        return IntentDecision("bug_report", 0.96, entities, "fault_terms")
    if verified_qa:
        return IntentDecision("qa", 0.99, entities, "verified_qa")
    if explicit_qa:
        return IntentDecision("qa", 0.92, entities, "knowledge_question")
    return IntentDecision("qa", 0.64, entities, "rag_default")


def action(action_id: str, label: str, style: str = "secondary") -> dict[str, str]:
    return {"id": action_id, "label": label, "style": style}


__all__ = ["IntentDecision", "action", "classify_customer_intent"]
