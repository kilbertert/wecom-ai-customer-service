"""BotTrace 渲染模块单测 + _process_bot_message_background 集成测试。

参照 tests/test_bot_image.py 风格:
- 纯函数/类: 直接 import + 调用
- 路由层: monkeypatch 注入配置 + mock httpx
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.config import settings
from app.routes.wechat import (
    _extract_knowledge_from_outputs,
    _extract_thinking_from_outputs,
)
from app.services.bot_trace import (
    BotTrace,
    format_knowledge_lines,
    format_thinking_lines,
    render_trace,
)

# ---------------------------------------------------------------------------
# BotTrace.event() — 基础数据收集
# ---------------------------------------------------------------------------


class TestBotTraceEvent:
    def test_event_appends_in_order(self):
        trace = BotTrace()
        trace.event("receive", "ok", "from=u1")
        trace.event("ai", "ok", "text=10字")
        assert len(trace.events) == 2
        assert trace.events[0].stage == "receive"
        assert trace.events[1].stage == "ai"

    def test_event_default_detail_is_empty(self):
        trace = BotTrace()
        trace.event("context", "skip")
        assert trace.events[0].detail == ""

    def test_event_unknown_stage_does_not_raise(self):
        """未知 stage 不会抛异常, render 时 fallback。"""
        trace = BotTrace()
        trace.event("unknown_stage", "ok")
        # 不抛异常即通过
        out = trace.render("inline", max_len=500)
        assert "unknown_stage" in out


# ---------------------------------------------------------------------------
# render_trace() — 纯函数 (顶层 helper)
# ---------------------------------------------------------------------------


class TestRenderTrace:
    def _full_trace(self) -> BotTrace:
        trace = BotTrace(chat_type="single", msg_type="text")
        trace.event("receive", "ok", "from=u1 id=abc123def")
        trace.event("prefilter", "ok", "text 支持")
        trace.event("dedup", "ok", "首次处理")
        trace.event("context", "skip", "单轮模式")
        trace.event("media", "skip", "无媒体")
        trace.event("knowledge", "ok", "chunks=3, 共456字")
        trace.event("thinking", "ok", "thinking=128字")
        trace.event("ai", "ok", "text=48字")
        trace.event("push", "ok", "HTTP 200 errcode=0")
        return trace

    def test_off_returns_empty(self):
        trace = self._full_trace()
        assert render_trace(trace, "off", max_len=1500) == ""

    def test_inline_contains_header_and_quote_prefix(self):
        """inline 模式: 灰色块引用 (>) 形式, 紧跟主消息末尾。"""
        trace = self._full_trace()
        out = render_trace(trace, "inline", max_len=1500)
        # 关键标识
        assert "🔧" in out
        assert "决策日志" in out
        assert "---" in out
        # 引用块前缀
        assert "> " in out
        # 阶段标签 (加粗中文, 不依赖 emoji — WeChat 不支持)
        assert "**接收**" in out
        assert "**预过滤**" in out
        assert "**去重**" in out
        assert "**上下文**" in out
        assert "**媒体**" in out
        assert "**知识库**" in out
        assert "**思考**" in out
        assert "**AI**" in out
        assert "**推送**" in out
        # 状态 emoji
        assert "✅" in out
        assert "➖" in out

    def test_separate_contains_header_and_divider(self):
        """separate 模式: 独立消息, 用 ─── 分隔线。"""
        trace = self._full_trace()
        out = render_trace(trace, "separate", max_len=1500)
        assert "🔧" in out
        assert "决策日志" in out
        assert "─────────" in out
        # 单独消息不应用引用块前缀
        assert "\n> " not in out
        # 所有阶段 (含新增的 知识库/思考)
        assert "**接收**" in out
        assert "**知识库**" in out
        assert "**思考**" in out
        assert "**AI**" in out
        assert "**推送**" in out

    def test_status_emoji_mapping(self):
        """状态 → emoji 映射: ok=✅, skip=➖, fail=❌"""
        trace = BotTrace(chat_type="single", msg_type="text")
        trace.event("ai", "ok", "成功")
        trace.event("media", "skip", "跳过")
        trace.event("push", "fail", "HTTP 500")
        out = render_trace(trace, "inline", max_len=1500)
        assert "✅ **AI** | 成功" in out
        assert "➖ **媒体** | 跳过" in out
        assert "❌ **推送** | HTTP 500" in out

    def test_unknown_status_uses_question_mark(self):
        """未知 status fallback 到 ❔, 不抛异常。"""
        trace = BotTrace()
        trace.event("ai", "weird_status")
        out = render_trace(trace, "inline", max_len=500)
        assert "❔" in out

    def test_group_chat_marker(self):
        """群聊标记: chat_type='group' → '群聊' (无 emoji, 兼容 WeChat)"""
        trace = BotTrace(chat_type="group", msg_type="text")
        trace.event("receive", "ok", "u1")
        out_inline = render_trace(trace, "inline", max_len=1500)
        out_sep = render_trace(trace, "separate", max_len=1500)
        assert "群聊" in out_inline
        assert "群聊" in out_sep
        # 不应包含 emoji (WeChat 不支持)
        assert "👥" not in out_inline

    def test_single_chat_marker(self):
        trace = BotTrace(chat_type="single", msg_type="text")
        trace.event("receive", "ok", "u1")
        out = render_trace(trace, "inline", max_len=1500)
        assert "单聊" in out
        assert "群聊" not in out
        # 不应包含 emoji
        assert "💬" not in out

    def test_truncates_at_max_len(self):
        """max_len=100 时输出不超过 100 字符 + 截断标记。"""
        trace = BotTrace(chat_type="group", msg_type="text")
        for i in range(20):
            trace.event("ai", "ok", f"step-{i}-padding-padding-padding-padding")
        out = render_trace(trace, "inline", max_len=120)
        assert len(out) <= 150  # 留余量给截断标记
        assert "已截断" in out

    def test_empty_events_renders_header_only(self):
        """无事件时只渲染头部 (单聊/群聊 + msgtype), 不抛异常。"""
        trace = BotTrace(chat_type="single", msg_type="image")
        out = render_trace(trace, "inline", max_len=1500)
        assert "单聊" in out
        assert "image" in out

    def test_inline_appends_separator_at_start(self):
        """inline 模式开头是 \\n\\n--- 分隔线, 方便接在主回复后。"""
        trace = BotTrace(chat_type="single", msg_type="text")
        trace.event("ai", "ok", "ok")
        out = render_trace(trace, "inline", max_len=1500)
        assert out.startswith("\n\n---\n")

    def test_detail_truncation_not_applied_per_event(self):
        """详情字段不单独截断, 整体超过 max_len 时才截。"""
        trace = BotTrace()
        trace.event("ai", "ok", "a" * 200)  # 单条 detail 较长
        out = render_trace(trace, "inline", max_len=1500)
        # max_len=1500 够长, 不应截断
        assert "已截断" not in out


# ---------------------------------------------------------------------------
# _process_bot_message_background — 三种模式 + 容错 (route 层)
# ---------------------------------------------------------------------------


def _build_decrypted_msg(
    msg_type: str = "text",
    content: str = "你好",
    chattype: str = "single",
    msgid: str = "msg-001",
    response_url: str = "https://example.com/response",
):
    """构造解密后的内层 JSON 字符串 (智能机器人协议)。"""
    obj = {
        "msgid": msgid,
        "chattype": chattype,
        "from": {"userid": "u-test"},
        "msgtype": msg_type,
        "response_url": response_url,
    }
    if msg_type == "text":
        obj["text"] = {"content": content}
    return obj


def _patch_decrypt(monkeypatch, decrypted_obj: dict):
    """patch WeChatService.decrypt_message_custom 直接返回解密后 JSON 字符串。"""
    import json as _json

    from app.routes import wechat as wechat_routes

    monkeypatch.setattr(
        wechat_routes.WeChatService,
        "decrypt_message_custom",
        staticmethod(
            lambda *args, **kwargs: _json.dumps(decrypted_obj, ensure_ascii=False)
        ),
    )


def _patch_verify_ok(monkeypatch):
    """让 verify_bot_signature 永远返回 True。"""
    from app.routes import wechat as wechat_routes

    monkeypatch.setattr(
        wechat_routes.WeChatService,
        "verify_bot_signature",
        lambda self, *args, **kwargs: True,
    )


def _patch_ai_workflow(monkeypatch, wf_result: dict | Exception):
    """让 get_ai_service() 返回的 ai.run_workflow 直接返回 wf_result。

    注意: 必须在 app.routes.wechat 命名空间 patch,
    因为 route 模块 import 时已建立函数引用。
    """
    from app.routes import wechat as wechat_routes

    ai = MagicMock()
    if isinstance(wf_result, Exception):
        ai.run_workflow = AsyncMock(side_effect=wf_result)
    else:
        ai.run_workflow = AsyncMock(return_value=wf_result)
    ai.close = AsyncMock()
    monkeypatch.setattr(wechat_routes, "get_ai_service", lambda: ai)
    return ai


def _patch_trace_mode(monkeypatch, mode: str, max_len: int = 1500):
    monkeypatch.setattr(settings.app, "bot_trace_mode", mode)
    monkeypatch.setattr(settings.app, "bot_trace_max_len", max_len)


class TestProcessBotBackgroundTrace:
    @pytest.mark.asyncio
    async def test_off_mode_pushes_only_reply_once(self, monkeypatch):
        """off 模式: 1 次 POST, payload 是纯 reply_text, 无 trace 块。"""
        _patch_verify_ok(monkeypatch)
        _patch_trace_mode(monkeypatch, "off")

        wf = {"text": "AI 回复", "images": [], "videos": [], "files": []}
        _patch_ai_workflow(monkeypatch, wf)

        captured: list[tuple[str, dict]] = []

        class _Resp:
            status_code = 200
            text = '{"errcode": 0, "errmsg": "ok"}'

        class _Client:
            def __init__(self, *a, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, url, json=None, headers=None):
                captured.append((url, json))
                return _Resp()

        import httpx as _httpx

        monkeypatch.setattr(_httpx, "AsyncClient", _Client)

        # 解密 + 入口
        msg = _build_decrypted_msg(msg_type="text", content="hi", chattype="single")
        _patch_decrypt(monkeypatch, msg)

        # 走 handler, 因为 _process_bot_message_background 是被 asyncio.create_task 调起
        # 这里直接 await 任务主体
        from app.routes import wechat as wechat_routes

        await wechat_routes._process_bot_message_background(
            msg=msg,
            from_user="u-test",
            msg_type="text",
            effective_media_type="",
            content="hi",
            msg_id="msg-001",
            response_url="https://example.com/response",
            wechat_media_ref="",
            wechat_media_kind="",
            timestamp="1700000000",
            nonce="nonce",
            encoding_aes_key="dummy",
            kf_token="dummy",
            chattype="single",
        )

        # 断言: 1 次 POST, payload 是纯 reply_text
        assert len(captured) == 1
        url, body = captured[0]
        assert url == "https://example.com/response"
        assert body["msgtype"] == "markdown"
        content_sent = body["markdown"]["content"]
        assert "AI 回复" in content_sent
        assert "🔧 决策日志" not in content_sent
        assert "---" not in content_sent

    @pytest.mark.asyncio
    async def test_inline_mode_pushes_combined_payload(self, monkeypatch):
        """inline 模式: 1 次 POST, payload 末尾追加 trace 块。"""
        _patch_verify_ok(monkeypatch)
        _patch_trace_mode(monkeypatch, "inline")

        wf = {"text": "AI 回复内容", "images": [], "videos": [], "files": []}
        _patch_ai_workflow(monkeypatch, wf)

        captured: list[dict] = []

        class _Resp:
            status_code = 200
            text = '{"errcode": 0, "errmsg": "ok"}'

        class _Client:
            def __init__(self, *a, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, url, json=None, headers=None):
                captured.append(json)
                return _Resp()

        import httpx as _httpx

        monkeypatch.setattr(_httpx, "AsyncClient", _Client)

        msg = _build_decrypted_msg(msg_type="text", content="hi", chattype="group")
        from app.routes import wechat as wechat_routes

        await wechat_routes._process_bot_message_background(
            msg=msg,
            from_user="u-test",
            msg_type="text",
            effective_media_type="",
            content="hi",
            msg_id="msg-002",
            response_url="https://example.com/response",
            wechat_media_ref="",
            wechat_media_kind="",
            timestamp="1700000000",
            nonce="nonce",
            encoding_aes_key="dummy",
            kf_token="dummy",
            chattype="group",
        )

        # 1 次 POST, payload 末尾含 trace
        assert len(captured) == 1
        body = captured[0]
        content_sent = body["markdown"]["content"]
        assert content_sent.startswith("AI 回复内容")
        assert "🔧" in content_sent
        assert "决策日志" in content_sent
        assert "群聊" in content_sent
        assert content_sent.find("AI 回复内容") < content_sent.find("决策日志")

    @pytest.mark.asyncio
    async def test_separate_mode_pushes_twice(self, monkeypatch):
        """separate 模式: 主 push 后再 POST 一次 trace。"""
        _patch_verify_ok(monkeypatch)
        _patch_trace_mode(monkeypatch, "separate")

        wf = {"text": "AI 主回复", "images": [], "videos": [], "files": []}
        _patch_ai_workflow(monkeypatch, wf)

        captured: list[dict] = []

        class _Resp:
            status_code = 200
            text = '{"errcode": 0, "errmsg": "ok"}'

        class _Client:
            def __init__(self, *a, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, url, json=None, headers=None):
                captured.append({"url": url, "body": json})
                return _Resp()

        import httpx as _httpx

        monkeypatch.setattr(_httpx, "AsyncClient", _Client)

        msg = _build_decrypted_msg(msg_type="text", content="hi", chattype="single")
        from app.routes import wechat as wechat_routes

        await wechat_routes._process_bot_message_background(
            msg=msg,
            from_user="u-test",
            msg_type="text",
            effective_media_type="",
            content="hi",
            msg_id="msg-003",
            response_url="https://example.com/response",
            wechat_media_ref="",
            wechat_media_kind="",
            timestamp="1700000000",
            nonce="nonce",
            encoding_aes_key="dummy",
            kf_token="dummy",
            chattype="single",
        )

        # 2 次 POST: 第一次是主回复, 第二次是 trace
        assert len(captured) == 2
        first, second = captured
        # 第一次: 纯 AI 回复, 无 trace
        assert first["body"]["markdown"]["content"] == "AI 主回复"
        assert "决策日志" not in first["body"]["markdown"]["content"]
        # 第二次: trace 文本
        assert "决策日志" in second["body"]["markdown"]["content"]
        assert "─────────" in second["body"]["markdown"]["content"]
        # URL 一致
        assert first["url"] == second["url"] == "https://example.com/response"

    @pytest.mark.asyncio
    async def test_separate_mode_trace_post_failure_does_not_propagate(
        self, monkeypatch
    ):
        """separate 模式: 第二次 POST 抛异常, 主消息已发, 不应传播异常。"""
        _patch_verify_ok(monkeypatch)
        _patch_trace_mode(monkeypatch, "separate")
        _patch_ai_workflow(
            monkeypatch, {"text": "ok", "images": [], "videos": [], "files": []}
        )

        posted_count = 0

        class _Resp:
            status_code = 200
            text = '{"errcode": 0}'

        class _Client:
            def __init__(self, *a, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, url, json=None, headers=None):
                nonlocal posted_count
                posted_count += 1
                if posted_count == 1:
                    return _Resp()
                # 第二次抛异常
                raise RuntimeError("network down")

        import httpx as _httpx

        monkeypatch.setattr(_httpx, "AsyncClient", _Client)

        msg = _build_decrypted_msg()
        from app.routes import wechat as wechat_routes

        # 不应抛异常
        await wechat_routes._process_bot_message_background(
            msg=msg,
            from_user="u-test",
            msg_type="text",
            effective_media_type="",
            content="hi",
            msg_id="msg-004",
            response_url="https://example.com/response",
            wechat_media_ref="",
            wechat_media_kind="",
            timestamp="1700000000",
            nonce="nonce",
            encoding_aes_key="dummy",
            kf_token="dummy",
            chattype="single",
        )
        # 验证: 主消息已发 (1 次成功), 第二次失败但不影响
        assert posted_count == 2

    @pytest.mark.asyncio
    async def test_ai_failure_recorded_in_trace(self, monkeypatch):
        """AI workflow 抛异常, trace.ai 标记为 fail, 主消息仍发出 (带错误提示)。"""
        _patch_verify_ok(monkeypatch)
        _patch_trace_mode(monkeypatch, "separate")
        _patch_ai_workflow(monkeypatch, RuntimeError("Dify 502"))

        captured: list[dict] = []

        class _Resp:
            status_code = 200
            text = '{"errcode": 0}'

        class _Client:
            def __init__(self, *a, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, url, json=None, headers=None):
                captured.append(json)
                return _Resp()

        import httpx as _httpx

        monkeypatch.setattr(_httpx, "AsyncClient", _Client)

        msg = _build_decrypted_msg()
        from app.routes import wechat as wechat_routes

        await wechat_routes._process_bot_message_background(
            msg=msg,
            from_user="u-test",
            msg_type="text",
            effective_media_type="",
            content="hi",
            msg_id="msg-005",
            response_url="https://example.com/response",
            wechat_media_ref="",
            wechat_media_kind="",
            timestamp="1700000000",
            nonce="nonce",
            encoding_aes_key="dummy",
            kf_token="dummy",
            chattype="single",
        )
        assert len(captured) == 2
        # 主消息是错误提示
        assert "AI 处理失败" in captured[0]["markdown"]["content"]
        # trace 中 ai 阶段标记为 fail
        trace_text = captured[1]["markdown"]["content"]
        assert "❌" in trace_text
        assert "Dify 502" in trace_text

    @pytest.mark.asyncio
    async def test_unsupported_msgtype_prefilter_fail(self, monkeypatch):
        """不支持的 msgtype: prefilter=fail, ai=skip。"""
        _patch_verify_ok(monkeypatch)
        _patch_trace_mode(monkeypatch, "separate")
        _patch_ai_workflow(monkeypatch, {"text": "ok"})

        captured: list[dict] = []

        class _Resp:
            status_code = 200
            text = '{"errcode": 0}'

        class _Client:
            def __init__(self, *a, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, url, json=None, headers=None):
                captured.append(json)
                return _Resp()

        import httpx as _httpx

        monkeypatch.setattr(_httpx, "AsyncClient", _Client)

        msg = _build_decrypted_msg(msg_type="video")  # video 不在白名单
        from app.routes import wechat as wechat_routes

        await wechat_routes._process_bot_message_background(
            msg=msg,
            from_user="u-test",
            msg_type="video",
            effective_media_type="",
            content="",
            msg_id="msg-006",
            response_url="https://example.com/response",
            wechat_media_ref="",
            wechat_media_kind="",
            timestamp="1700000000",
            nonce="nonce",
            encoding_aes_key="dummy",
            kf_token="dummy",
            chattype="single",
        )
        trace_text = captured[1]["markdown"]["content"]
        # prefilter 失败 (❌)
        assert "❌" in trace_text
        assert "**预过滤**" in trace_text
        # AI 是 skip (➖), 不应被调用
        assert "➖ **AI**" in trace_text
        # 知识库/思考 也应 skip (因为 prefilter fail, 没调 AI)
        assert "➖ **知识库**" in trace_text
        assert "➖ **思考**" in trace_text


# ---------------------------------------------------------------------------
# _extract_knowledge_from_outputs — 知识库检索结果提取
# ---------------------------------------------------------------------------


class TestExtractKnowledgeFromOutputs:
    def test_finds_list_of_chunks(self):
        outputs = {
            "output": "reply text",
            "knowledge": [
                {"content": "chunk1 text", "score": 0.95},
                {"content": "chunk2 text", "score": 0.87},
            ],
        }
        result = _extract_knowledge_from_outputs(outputs)
        assert isinstance(result, list)
        assert len(result) == 2
        assert result[0]["content"] == "chunk1 text"

    def test_finds_string_knowledge(self):
        outputs = {"retrieved_chunks": "检索到的上下文文本"}
        result = _extract_knowledge_from_outputs(outputs)
        assert result == "检索到的上下文文本"

    def test_returns_none_when_empty(self):
        assert _extract_knowledge_from_outputs({}) is None
        assert _extract_knowledge_from_outputs({"output": "only reply"}) is None

    def test_returns_none_when_none_input(self):
        assert _extract_knowledge_from_outputs(None) is None

    def test_skips_empty_list(self):
        outputs = {"knowledge": [], "output": "reply"}
        result = _extract_knowledge_from_outputs(outputs)
        assert result is None

    def test_finds_by_priority_order(self):
        """多个候选 key 时, 按优先级返回第一个非空值。"""
        outputs = {
            "knowledge": "first",
            "retrieved_chunks": "second",
            "context": "third",
        }
        result = _extract_knowledge_from_outputs(outputs)
        assert result == "first"


# ---------------------------------------------------------------------------
# _extract_thinking_from_outputs — 思考过程提取
# ---------------------------------------------------------------------------


class TestExtractThinkingFromOutputs:
    def test_finds_reasoning_content(self):
        outputs = {"reasoning_content": "  模型思考: 用户可能想了解...  "}
        result = _extract_thinking_from_outputs(outputs)
        assert result == "模型思考: 用户可能想了解..."

    def test_finds_thinking_key(self):
        outputs = {"thinking": "Let me analyze this step by step..."}
        result = _extract_thinking_from_outputs(outputs)
        assert "step by step" in result

    def test_returns_empty_when_not_found(self):
        assert _extract_thinking_from_outputs({}) == ""
        assert _extract_thinking_from_outputs({"output": "reply only"}) == ""

    def test_returns_empty_when_none_input(self):
        assert _extract_thinking_from_outputs(None) == ""

    def test_finds_by_priority_order(self):
        outputs = {
            "reasoning_content": "first reasoning",
            "thinking": "second thinking",
            "reasoning": "third",
        }
        result = _extract_thinking_from_outputs(outputs)
        assert result == "first reasoning"


# ---------------------------------------------------------------------------
# render_trace — 知识库/思考阶段渲染
# ---------------------------------------------------------------------------


class TestKnowledgeThinkingRender:
    def test_knowledge_skip_shows_in_output(self):
        trace = BotTrace(chat_type="single", msg_type="text")
        trace.event("knowledge", "skip", "无知识库检索")
        out = render_trace(trace, "inline", max_len=1500)
        assert "➖ **知识库** | 无知识库检索" in out

    def test_thinking_skip_shows_in_output(self):
        trace = BotTrace(chat_type="single", msg_type="text")
        trace.event("thinking", "skip", "无思考过程")
        out = render_trace(trace, "inline", max_len=1500)
        assert "➖ **思考** | 无思考过程" in out

    def test_knowledge_with_chunks_shows_count(self):
        trace = BotTrace(chat_type="single", msg_type="text")
        trace.event("knowledge", "ok", "chunks=5, 共1024字")
        out = render_trace(trace, "inline", max_len=1500)
        assert "✅ **知识库** | chunks=5, 共1024字" in out

    def test_thinking_with_text_shows_length(self):
        trace = BotTrace(chat_type="single", msg_type="text")
        trace.event("thinking", "ok", "thinking=256字")
        out = render_trace(trace, "inline", max_len=1500)
        assert "✅ **思考** | thinking=256字" in out


# ---------------------------------------------------------------------------
# _process_bot_message_background — 知识库/思考 数据流集成
# ---------------------------------------------------------------------------


class TestBotBackgroundKnowledgeThinking:
    @pytest.mark.asyncio
    async def test_knowledge_and_thinking_appear_in_trace(self, monkeypatch):
        """Dify 返回知识库和思考数据时, trace 中包含对应阶段。"""
        _patch_verify_ok(monkeypatch)
        _patch_trace_mode(monkeypatch, "separate")

        wf = {
            "text": "AI 回复",
            "images": [],
            "videos": [],
            "files": [],
            "raw": {
                "data": {
                    "outputs": {
                        "text": "AI 回复",
                        "knowledge": [
                            {"content": "知识片段1", "score": 0.9},
                            {"content": "知识片段2", "score": 0.8},
                        ],
                        "reasoning_content": "让我分析一下用户的问题...",
                    }
                }
            },
        }
        _patch_ai_workflow(monkeypatch, wf)

        captured: list[dict] = []

        class _Resp:
            status_code = 200
            text = '{"errcode": 0}'

        class _Client:
            def __init__(self, *a, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, url, json=None, headers=None):
                captured.append(json)
                return _Resp()

        import httpx as _httpx

        monkeypatch.setattr(_httpx, "AsyncClient", _Client)

        msg = _build_decrypted_msg(msg_type="text", content="你好", chattype="single")
        from app.routes import wechat as wechat_routes

        await wechat_routes._process_bot_message_background(
            msg=msg,
            from_user="u-test",
            msg_type="text",
            effective_media_type="",
            content="你好",
            msg_id="msg-kb-001",
            response_url="https://example.com/response",
            wechat_media_ref="",
            wechat_media_kind="",
            timestamp="1700000000",
            nonce="nonce",
            encoding_aes_key="dummy",
            kf_token="dummy",
            chattype="single",
        )

        assert len(captured) == 2
        trace_text = captured[1]["markdown"]["content"]
        # 知识库阶段: 主行 + 每条 chunk 详情
        assert "✅ **知识库** | 2 chunks" in trace_text
        assert "📄 知识片段1" in trace_text or "chunk-1" in trace_text
        # 思考阶段: 主行 + 步骤列表
        assert "✅ **思考** | 14字" in trace_text
        assert "(3步)" in trace_text
        assert "• 让我分析一下用户的问题" in trace_text

    @pytest.mark.asyncio
    async def test_knowledge_and_thinking_skip_when_no_data(self, monkeypatch):
        """Dify 不返回知识库/思考数据时, trace 中显示 skip。"""
        _patch_verify_ok(monkeypatch)
        _patch_trace_mode(monkeypatch, "separate")

        wf = {
            "text": "AI 回复",
            "images": [],
            "videos": [],
            "files": [],
            "raw": {
                "data": {
                    "outputs": {
                        "text": "AI 回复",
                    }
                }
            },
        }
        _patch_ai_workflow(monkeypatch, wf)

        captured: list[dict] = []

        class _Resp:
            status_code = 200
            text = '{"errcode": 0}'

        class _Client:
            def __init__(self, *a, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, url, json=None, headers=None):
                captured.append(json)
                return _Resp()

        import httpx as _httpx

        monkeypatch.setattr(_httpx, "AsyncClient", _Client)

        msg = _build_decrypted_msg(msg_type="text", content="hi", chattype="group")
        from app.routes import wechat as wechat_routes

        await wechat_routes._process_bot_message_background(
            msg=msg,
            from_user="u-test",
            msg_type="text",
            effective_media_type="",
            content="hi",
            msg_id="msg-kb-002",
            response_url="https://example.com/response",
            wechat_media_ref="",
            wechat_media_kind="",
            timestamp="1700000000",
            nonce="nonce",
            encoding_aes_key="dummy",
            kf_token="dummy",
            chattype="group",
        )

        assert len(captured) == 2
        trace_text = captured[1]["markdown"]["content"]
        assert "➖ **知识库** | 无知识库检索" in trace_text
        assert "➖ **思考** | 无思考过程" in trace_text

    @pytest.mark.asyncio
    async def test_ai_failure_skips_knowledge_thinking(self, monkeypatch):
        """AI 调用失败时, 知识库/思考阶段应 skip (因为没有 Dify 响应)。"""
        _patch_verify_ok(monkeypatch)
        _patch_trace_mode(monkeypatch, "separate")
        _patch_ai_workflow(monkeypatch, RuntimeError("Dify 502"))

        captured: list[dict] = []

        class _Resp:
            status_code = 200
            text = '{"errcode": 0}'

        class _Client:
            def __init__(self, *a, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, url, json=None, headers=None):
                captured.append(json)
                return _Resp()

        import httpx as _httpx

        monkeypatch.setattr(_httpx, "AsyncClient", _Client)

        msg = _build_decrypted_msg()
        from app.routes import wechat as wechat_routes

        await wechat_routes._process_bot_message_background(
            msg=msg,
            from_user="u-test",
            msg_type="text",
            effective_media_type="",
            content="hi",
            msg_id="msg-kb-003",
            response_url="https://example.com/response",
            wechat_media_ref="",
            wechat_media_kind="",
            timestamp="1700000000",
            nonce="nonce",
            encoding_aes_key="dummy",
            kf_token="dummy",
            chattype="single",
        )

        assert len(captured) == 2
        trace_text = captured[1]["markdown"]["content"]
        # AI 是 fail
        assert "❌ **AI**" in trace_text
        # 知识库/思考 应 skip (因为在 AI call 的 except 分支, 不会提取)
        assert "➖ **知识库**" in trace_text
        assert "➖ **思考**" in trace_text


# ---------------------------------------------------------------------------
# format_knowledge_lines — 知识库多行 detail 格式化
# ---------------------------------------------------------------------------


class TestFormatKnowledgeLines:
    def test_empty_knowledge_returns_no_subs(self):
        main, subs = format_knowledge_lines([])
        assert subs == []
        assert "0" in main or "无" in main

    def test_none_returns_no_subs(self):
        main, subs = format_knowledge_lines(None)
        assert subs == []
        assert "无" in main

    def test_single_chunk_with_title(self):
        knowledge = [
            {
                "title": "操作手册.md",
                "content": "设备故障列表",
                "metadata": {"score": 0.85, "segment_word_count": 100},
            }
        ]
        main, subs = format_knowledge_lines(knowledge)
        assert "1 chunks" in main
        # content 6 字符 + 100 字元数据
        assert "6字" in main
        assert len(subs) == 1
        assert "操作手册.md" in subs[0]
        assert "0.85" in subs[0]
        assert '"设备故障列表"' in subs[0]

    def test_multiple_chunks_show_score_and_word_count(self):
        knowledge = [
            {"title": "A.md", "content": "片段A", "metadata": {"score": 0.9, "segment_word_count": 50}},
            {"title": "B.md", "content": "片段B", "metadata": {"score": 0.7, "segment_word_count": 30}},
        ]
        main, subs = format_knowledge_lines(knowledge)
        assert "2 chunks" in main
        # 主行: 2 chunks (6字) — content 字符数
        assert "6字" in main
        assert len(subs) == 2
        assert "A.md" in subs[0]
        assert "B.md" in subs[1]
        # sub-line 显示 segment_word_count 元数据
        assert "50字" in subs[0]
        assert "30字" in subs[1]

    def test_long_content_truncated_with_ellipsis(self):
        long_content = "x" * 200
        knowledge = [{"title": "Long.md", "content": long_content, "metadata": {"score": 0.5}}]
        main, subs = format_knowledge_lines(knowledge)
        # 预览应在 60 字符左右截断
        assert "..." in subs[0]
        # 主行字数统计应是全量 200
        assert "200" in main

    def test_chunk_without_metadata_uses_fallback_title(self):
        knowledge = [{"content": "无元数据片段"}]
        main, subs = format_knowledge_lines(knowledge)
        assert len(subs) == 1
        assert "chunk-1" in subs[0]

    def test_string_knowledge_treated_as_single(self):
        main, subs = format_knowledge_lines("完整文本内容")
        assert "无" not in main
        assert len(subs) == 1
        assert "完整文本内容" in subs[0]


# ---------------------------------------------------------------------------
# format_thinking_lines — 思考过程多行 detail 格式化
# ---------------------------------------------------------------------------


class TestFormatThinkingLines:
    def test_empty_thinking_returns_no_subs(self):
        main, subs = format_thinking_lines("")
        assert subs == []
        assert "无" in main

    def test_whitespace_only_thinking_returns_no_subs(self):
        main, subs = format_thinking_lines("   \n\n  ")
        assert subs == []

    def test_single_step(self):
        main, subs = format_thinking_lines("用户问的是充电问题")
        assert "9字" in main
        assert "(1步)" in main
        assert len(subs) == 1
        assert "• 用户问的是充电问题" in subs[0]

    def test_split_by_chinese_period(self):
        text = "第一步。 第二步。 第三步。"
        main, subs = format_thinking_lines(text)
        assert "(3步)" in main
        assert len(subs) == 3
        assert "• 第一步" in subs[0]
        assert "• 第二步" in subs[1]
        assert "• 第三步" in subs[2]

    def test_split_by_newline(self):
        text = "步骤A\n步骤B\n步骤C"
        main, subs = format_thinking_lines(text)
        assert "(3步)" in main
        assert len(subs) == 3

    def test_split_by_english_period(self):
        text = "First step. Second step. Third step."
        main, subs = format_thinking_lines(text)
        assert "(3步)" in main
        assert len(subs) == 3

    def test_long_steps_truncated(self):
        long_step = "x" * 200
        text = f"{long_step}。下一步。"
        main, subs = format_thinking_lines(text)
        assert len(subs) == 2
        # 第一步有 "..." 截断
        assert "..." in subs[0]
        # 主行总字数 (200 + 下一步3字 = 203+ 字)
        assert "字" in main

    def test_exceeds_max_steps_omits_remainder(self):
        # 7 步, 限制 5 步, 应显示 5 + "(+2 步省略)"
        text = "。".join([f"第{i}步" for i in range(7)])
        main, subs = format_thinking_lines(text)
        assert "(7步)" in main
        assert len(subs) == 6  # 5 steps + 1 omission note
        assert "(+2 步省略)" in subs[-1]


# ---------------------------------------------------------------------------
# render_trace — sub_lines 多行渲染
# ---------------------------------------------------------------------------


class TestRenderTraceSubLines:
    def test_sub_lines_appear_after_main_line(self):
        trace = BotTrace(chat_type="single", msg_type="text")
        trace.event("knowledge", "ok", "2 chunks (100字)", sub_lines=[
            '📄 A.md "片段A"',
            '📄 B.md "片段B"',
        ])
        out = render_trace(trace, "inline", max_len=1500)
        # sub_lines 紧跟主行, 用 ">   " (引用块 + 缩进) 渲染
        assert "> ✅ **知识库** | 2 chunks (100字)" in out
        assert '> 📄 A.md "片段A"' in out
        assert '> 📄 B.md "片段B"' in out

    def test_sub_lines_in_separate_mode_no_quote_prefix(self):
        trace = BotTrace(chat_type="single", msg_type="text")
        trace.event("thinking", "ok", "10字 (2步)", sub_lines=[
            "• 步骤1",
            "• 步骤2",
        ])
        out = render_trace(trace, "separate", max_len=1500)
        # separate 模式下 sub_lines 直接接在主行后, 没有 "> " 引用前缀
        assert "✅ **思考** | 10字 (2步)" in out
        assert "• 步骤1" in out
        assert "• 步骤2" in out
        # 整行不应用 "> " 引用
        assert "> •" not in out

    def test_empty_sub_lines_no_extra_output(self):
        trace = BotTrace(chat_type="single", msg_type="text")
        trace.event("ai", "ok", "text=5字")  # 无 sub_lines
        out = render_trace(trace, "inline", max_len=1500)
        # 不应出现 sub_lines 渲染
        assert "text=5字" in out
        # 行数不应膨胀
        assert out.count(">") == out.count("> ")  # 全是引用前缀, 无空内容

    def test_inline_sub_lines_preserved_under_max_len(self):
        trace = BotTrace(chat_type="single", msg_type="text")
        # 大量 sub_lines 测试不截断
        trace.event("knowledge", "ok", "3 chunks (300字)", sub_lines=[
            f'📄 doc{i}.md "片段{i}"' for i in range(3)
        ])
        out = render_trace(trace, "inline", max_len=2000)
        assert "已截断" not in out
        assert all(f"📄 doc{i}.md" in out for i in range(3))

    def test_sub_lines_truncated_when_total_exceeds_max_len(self):
        trace = BotTrace(chat_type="single", msg_type="text")
        trace.event("knowledge", "ok", "10 chunks", sub_lines=[
            f'📄 doc{i}.md "非常长的内容' + "x" * 100 + '"' for i in range(20)
        ])
        out = render_trace(trace, "inline", max_len=300)
        assert "已截断" in out
