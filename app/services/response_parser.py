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


def _extract_from_parsed_dict(parsed: dict, depth: int = 0) -> str | None:
    """从一个已解析的 dict 里提取用户回复文本。

    Dify 工作流真实结构 (二期):
        {"scene": "fallback", "payload": {"text": "您好..."}}
        或 {"text": "..."}
        或 {"text": "...", "media": [...]}

    优先级:
        1. payload.text / payload.content / payload.output    (Dify 嵌套模式)
        2. text / content / output / answer / message / result (顶层)
        3. 递归第一个非空字符串 (兜底, 跳过 enum 短字段)
    """
    if depth >= 6:
        return None

    # 1) 优先 payload.* 嵌套
    payload = parsed.get("payload")
    if isinstance(payload, dict):
        for k in ("text", "content", "output", "answer", "message", "result"):
            v = payload.get(k)
            if isinstance(v, str) and v.strip() and len(v.strip()) >= 2:
                return v.strip()
        # payload 本身是 dict 但没有文本字段, 继续深钻
        nested = _extract_from_parsed_dict(payload, depth + 1)
        if nested:
            return nested

    # 2) 顶层常见字段
    for k in ("text", "content", "output", "answer", "message", "result"):
        v = parsed.get(k)
        if isinstance(v, str) and v.strip() and len(v.strip()) >= 2:
            return v.strip()
        if isinstance(v, dict):
            # 顶层字段是 dict 时再深钻 (兼容 {"text": {...}} 形式)
            nested = _extract_from_parsed_dict(v, depth + 1)
            if nested:
                return nested

    # 3) 兜底: 递归 values, 但跳过 enum/短字段 (避免返回 "fallback" 这种)
    SKIP_KEYS = {"scene", "type", "kind", "mode", "status", "code", "intent", "category"}
    for k, v in parsed.items():
        if k in SKIP_KEYS:
            continue
        if isinstance(v, str) and v.strip() and len(v.strip()) >= 2:
            return v.strip()
        if isinstance(v, dict):
            nested = _extract_from_parsed_dict(v, depth + 1)
            if nested:
                return nested
        if isinstance(v, list):
            for item in v:
                if isinstance(item, str) and item.strip() and len(item.strip()) >= 2:
                    return item.strip()
                if isinstance(item, dict):
                    nested = _extract_from_parsed_dict(item, depth + 1)
                    if nested:
                        return nested
    return None


def _first_nonempty_string(value: Any, depth: int = 0) -> str | None:
    if depth >= 8:
        return None
    if isinstance(value, str):
        # 一期修复: Dify 工作流 output 经常是 LLM 输出的 JSON 字符串
        # 如 {"payload": {"text": "..."}} 或 {"text": "..."}, 二次解析才能拿到真正的文本
        stripped = value.strip()
        if stripped and stripped[0] in ("{", "["):
            try:
                parsed = json.loads(stripped)
                if isinstance(parsed, dict):
                    return _extract_from_parsed_dict(parsed, depth + 1)
            except (json.JSONDecodeError, ValueError):
                pass
        return stripped or None
    if isinstance(value, dict):
        return _extract_from_parsed_dict(value, depth + 1)
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

    # 0) 快速路径: Dify 工作流 v8+ 直接提供 outputs.text 纯文本字段 (二期)
    # 命中即返回, 跳过 JSON 解析, 节省 CPU + 避免嵌套 JSON 误解析
    direct_text = outputs.get("text")
    if isinstance(direct_text, str) and direct_text.strip() and len(direct_text.strip()) >= 2:
        return _strip_thinking(direct_text)

    # 1) Preferred key (configurable, default "output")
    text = _strip_thinking(_first_nonempty_string(outputs.get(preferred_key)) or "")
    if text:
        return text

    # 2) Common fallbacks
    for k in ("output", "answer", "result", "message", "content", "text"):
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
