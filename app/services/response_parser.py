"""Dify workflow 响应文本提取器 (可被 DifyService / 测试共用)。"""
from __future__ import annotations

import json
import re
from typing import Any

# Strip reasoning blocks leaked by thinking-enabled models (e.g. doubao-seed-2-0-lite).
# Matched non-greedy with DOTALL so multi-line / empty <think>\n\n</think> cases are handled.
_THINK_BLOCK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)


def _strip_thinking(text: str) -> str:
    if not text:
        return ""
    return _THINK_BLOCK_RE.sub("", text).strip()


def _first_nonempty_string(value: Any, depth: int = 0) -> str | None:
    if depth >= 8:
        return None
    if isinstance(value, str):
        s = value.strip()
        return s or None
    if isinstance(value, dict):
        for v in value.values():
            found = _first_nonempty_string(v, depth + 1)
            if found:
                return found
        return None
    if isinstance(value, list):
        for item in value:
            found = _first_nonempty_string(item, depth + 1)
            if found:
                return found
        return None
    return None


def extract_assistant_text(raw: dict, preferred_key: str = "output") -> str:
    """
    Pull the user-facing reply out of a Dify workflow blocking response.

    Dify structure is regular:
        raw["data"]["outputs"][<var_name>] = <text>

    We still fall back to a deep search because some workflows dump the entire
    answer into a single generic key like "result"/"text"/"answer".

    Reasoning blocks (<think>...</think>) leaked by thinking-enabled models are
    stripped before returning, so the frontend never sees chain-of-thought.
    """
    data = (raw or {}).get("data") or {}
    outputs = data.get("outputs") or {}

    # 1) Preferred key (configurable, default "output")
    text = _strip_thinking(_first_nonempty_string(outputs.get(preferred_key)) or "")
    if text:
        return text

    # 2) Common fallbacks
    for k in ("output", "answer", "result", "text", "message", "content"):
        text = _strip_thinking(_first_nonempty_string(outputs.get(k)) or "")
        if text:
            return text

    # 3) Deep search across outputs
    text = _strip_thinking(_first_nonempty_string(outputs) or "")
    if text:
        return text

    # 4) Last resort: pretty-print the raw for debug visibility
    try:
        return json.dumps(raw, ensure_ascii=False, indent=2)
    except Exception:
        return str(raw)
