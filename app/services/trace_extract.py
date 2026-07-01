"""从 Dify workflow outputs 中提取 trace 阶段所需的辅助数据。

纯函数, 无 I/O。供 ``MessageProcessor`` 在 bot 决策日志的"知识库检索"与
"思考过程"两个阶段调用。

从历史 route 层 ``_extract_knowledge_from_outputs`` /
``_extract_thinking_from_outputs`` 原样搬迁, 逻辑零变化。
"""

from __future__ import annotations

from typing import Any, Optional

# 知识库检索结果常见变量名 (按优先级)
_KNOWLEDGE_KEYS = (
    "knowledge",
    "retrieved_chunks",
    "retrieval_result",
    "context",
    "knowledge_result",
    "kb_result",
)

# LLM 思考过程常见变量名 (按优先级)
_THINKING_KEYS = (
    "reasoning_content",
    "thinking",
    "reasoning",
    "thought_process",
    "thought",
    "cot",
)


def extract_knowledge(outputs: Any) -> Optional[Any]:
    """从 Dify workflow outputs 中提取知识库检索结果。

    Returns:
        检索结果 (list / str / dict), 未找到返回 None。
    """
    if not isinstance(outputs, dict):
        return None
    for key in _KNOWLEDGE_KEYS:
        val = outputs.get(key)
        if val is not None and val != "" and val != []:
            return val
    return None


def extract_thinking(outputs: Any) -> str:
    """从 Dify workflow outputs 中提取 LLM 思考过程文本。

    Returns:
        思考文本 (已 strip), 未找到返回空字符串。
    """
    if not isinstance(outputs, dict):
        return ""
    for key in _THINKING_KEYS:
        val = outputs.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return ""


__all__ = ["extract_knowledge", "extract_thinking"]
