"""智能机器人决策日志 (Bot Decision-Log Trace) 渲染模块。

记录一条消息从接收到推送所经过的关键阶段 (含知识库检索与思考过程), 按
``settings.app.bot_trace_mode`` 渲染为不同形式的 markdown 文本:

- ``"off"``      : 完全不输出, 对现有流程零侵入
- ``"inline"``   : 拼到 AI 回复文本末尾, 单次 POST
- ``"separate"`` : 再单独 POST 一次, 推一条 "🔧 决策日志" 消息

知识库/思考阶段支持"多行 detail" (sub_lines), inline 模式下用 ``>   `` 缩进
渲染, separate 模式下用 ``  `` 缩进渲染, 既能展示检索片段/思考步骤, 又不刷屏。

设计原则: 纯数据 + 纯函数, 无 I/O, 无外部依赖, 便于单测。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional

# 阶段状态 → emoji
# WeChat markdown 只支持 ✅ ❌ ➖ 等基础 emoji, 不支持 📥 🔁 🖼️ 等 unicode 字符
# (会渲染成方块), 故 stage 标签统一用加粗中文, 不加图标
_STATUS_EMOJI = {
    "ok": "✅",
    "skip": "➖",
    "fail": "❌",
}

# 阶段名 → 加粗中文标签 (markdown bold 在 WeChat 渲染稳定)
_STAGE_LABEL = {
    "receive": "**接收**",
    "prefilter": "**预过滤**",
    "dedup": "**去重**",
    "context": "**上下文**",
    "media": "**媒体**",
    "knowledge": "**知识库**",
    "thinking": "**思考**",
    "ai": "**AI**",
    "push": "**推送**",
}

# 知识库 sub-line: 每个 chunk 最多展示的内容字符数 (避免刷屏)
KNOWLEDGE_CONTENT_PREVIEW = 60

# 思考过程 sub-line: 最多切分为多少步
THINKING_MAX_STEPS = 5

# 思考过程: 每步最多展示的字符数
THINKING_STEP_MAX_LEN = 80


@dataclass
class TraceEvent:
    """单阶段事件。

    ``sub_lines`` 用于承载多行扩展信息 (如知识库每条 chunk、思考每一步),
    渲染时以缩进形式跟随主行。空列表或 None 表示无扩展。
    """

    stage: str
    status: str
    detail: str = ""
    sub_lines: List[str] = field(default_factory=list)


@dataclass
class BotTrace:
    """一条消息的全链路 trace 容器。

    使用方法::

        trace = BotTrace(chat_type="group", msg_type="text")
        trace.event("receive", "ok", "from=u1")
        trace.event("ai",      "ok", "text=48字")
        text = render_trace(trace, "inline", max_len=1500)
    """

    events: List[TraceEvent] = field(default_factory=list)
    chat_type: str = "single"  # "single" | "group"
    msg_type: str = ""

    def event(self, stage: str, status: str, detail: str = "", sub_lines: Optional[List[str]] = None) -> None:
        """追加一个阶段事件。

        Args:
            stage: 阶段名 (``_STAGE_LABEL`` 的 key, 未知时回退原字符串)
            status: ``"ok"`` | ``"skip"`` | ``"fail"``
            detail: 主行描述 (简短, 如 "text 8字")
            sub_lines: 可选多行扩展 (知识库 chunk 列表、思考步骤列表)
        """
        self.events.append(TraceEvent(
            stage=stage,
            status=status,
            detail=detail,
            sub_lines=list(sub_lines) if sub_lines else [],
        ))

    def render(self, mode: str, max_len: int = 1500) -> str:
        """便捷方法, 等价于 ``render_trace(self, mode, max_len)``。"""
        return render_trace(self, mode, max_len)


# ---------------------------------------------------------------------------
# 知识库 / 思考 格式化器
# ---------------------------------------------------------------------------


def format_knowledge_lines(knowledge: object) -> tuple[str, List[str]]:
    """把 Dify 知识库检索结果格式化为 (主行detail, sub_lines)。

    主行: ``2 chunks (467字)`` — 整体统计
    sub_lines: 每行 ``📄 <文档名> (<score>, <片段字数>) "<内容预览>"``

    Args:
        knowledge: 来自 Dify outputs 的 knowledge 字段 (list of dict 或 str)

    Returns:
        (main_detail, sub_lines) — sub_lines 为空表示无知识库
    """
    if not knowledge:
        return "无知识库检索", []

    if isinstance(knowledge, str):
        # 字符串形式: 当成单条内容
        preview = knowledge[:KNOWLEDGE_CONTENT_PREVIEW]
        if len(knowledge) > KNOWLEDGE_CONTENT_PREVIEW:
            preview += "..."
        return f"text={len(knowledge)}字", [f'"{preview}"']

    if not isinstance(knowledge, list):
        return f"type={type(knowledge).__name__}", []

    chunks: List[dict] = []
    for item in knowledge:
        if isinstance(item, dict):
            chunks.append(item)
        else:
            chunks.append({"content": str(item)})

    if not chunks:
        return "0 chunks", []

    total_chars = sum(len(str(c.get("content", ""))) for c in chunks)
    main = f"{len(chunks)} chunks ({total_chars}字)"

    sub: List[str] = []
    for i, c in enumerate(chunks, 1):
        title = str(c.get("title") or c.get("document_name") or f"chunk-{i}")
        score = c.get("metadata", {}).get("score") if isinstance(c.get("metadata"), dict) else None
        wc = c.get("metadata", {}).get("segment_word_count") if isinstance(c.get("metadata"), dict) else None
        content = str(c.get("content", ""))

        head = f"📄 {title}"
        if score is not None or wc is not None:
            meta_parts = []
            if score is not None:
                meta_parts.append(f"score={float(score):.2f}")
            if wc is not None:
                meta_parts.append(f"{wc}字")
            head += f" ({', '.join(meta_parts)})"

        preview = content[:KNOWLEDGE_CONTENT_PREVIEW].replace("\n", " ").replace("  ", " ").strip()
        if len(content) > KNOWLEDGE_CONTENT_PREVIEW:
            preview += "..."

        sub.append(f'{head} "{preview}"')

    return main, sub


def format_thinking_lines(thinking: str) -> tuple[str, List[str]]:
    """把 LLM 思考过程拆成步骤, 返回 (主行detail, sub_lines)。

    主行: ``249字 (3步)``
    sub_lines: 每行 ``• <该步内容>`` (单步超长时截断到 THINKING_STEP_MAX_LEN)

    Args:
        thinking: 来自 Dify outputs 的 reasoning_content 字段

    Returns:
        (main_detail, sub_lines) — sub_lines 为空表示无思考过程
    """
    if not thinking or not thinking.strip():
        return "无思考过程", []

    text = thinking.strip()
    # 按中英文句号/换行切分 (兼容 .\n \n  ? 等)
    raw_steps = re.split(r"(?<=[。\.\n])\s*", text)
    # 过滤空段
    steps = [s.strip() for s in raw_steps if s and s.strip()]

    if not steps:
        return f"{len(text)}字", []

    # 限步数: 超过 THINKING_MAX_STEPS 截断, 末尾追加 "(+N 步省略)"
    if len(steps) > THINKING_MAX_STEPS:
        shown = steps[:THINKING_MAX_STEPS]
        omitted = len(steps) - THINKING_MAX_STEPS
        sub = [f"• {s[:THINKING_STEP_MAX_LEN]}{'...' if len(s) > THINKING_STEP_MAX_LEN else ''}" for s in shown]
        sub.append(f"... (+{omitted} 步省略)")
    else:
        sub = [f"• {s[:THINKING_STEP_MAX_LEN]}{'...' if len(s) > THINKING_STEP_MAX_LEN else ''}" for s in steps]

    main = f"{len(text)}字 ({len(steps)}步)"
    return main, sub


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------


def _format_line(ev: TraceEvent) -> str:
    """单个事件渲染为 ``<status_emoji> <stage_label> | <detail>``。"""
    stage_label = _STAGE_LABEL.get(ev.stage, ev.stage)
    status_emoji = _STATUS_EMOJI.get(ev.status, "❔")
    line = f"{status_emoji} {stage_label}"
    if ev.detail:
        line += f" | {ev.detail}"
    return line


def _chat_header(trace: BotTrace) -> str:
    """头部: 会话类型 + 消息类型, 形如 ``群聊 · text``。

    不用 emoji (👥/💬), WeChat 不支持, 渲染成方块。
    """
    chat_label = "群聊" if trace.chat_type == "group" else "单聊"
    if trace.msg_type:
        return f"{chat_label} · {trace.msg_type}"
    return chat_label


def render_trace(trace: BotTrace, mode: str, max_len: int = 1500) -> str:
    """按模式渲染 trace 文本。

    Args:
        trace: 已收集好事件的 BotTrace 实例
        mode: ``"off"`` | ``"inline"`` | ``"separate"``
        max_len: 输出文本最大字符数, 超出时截断并追加 ``…(已截断)``

    Returns:
        渲染好的 markdown 文本。``"off"`` 或未知 mode 一律返回 ``""``。
    """
    if mode not in ("inline", "separate"):
        return ""

    header = _chat_header(trace)
    body_lines: List[str] = [header]

    for ev in trace.events:
        body_lines.append(_format_line(ev))
        # sub_lines: 跟在该事件主行后, inline 模式加 "> " 引用前缀, separate 模式加 "  " 缩进
        for sub in ev.sub_lines:
            body_lines.append(sub)

    if mode == "inline":
        # 引用块形式, 紧接主回复末尾; 开头是 --- 分隔线
        quoted = "\n".join(f"> {ln}" for ln in body_lines)
        text = f"\n\n---\n> 🔧 **决策日志**\n{quoted}"
    else:  # separate
        text = "🔧 **决策日志**\n─────────\n" + "\n".join(body_lines)

    # 截断保护: 留 20 字符给 "…(已截断)" 标记
    if len(text) > max_len:
        text = text[: max_len - 20] + "\n…(已截断)"

    return text


__all__ = [
    "BotTrace",
    "TraceEvent",
    "render_trace",
    "format_knowledge_lines",
    "format_thinking_lines",
]
