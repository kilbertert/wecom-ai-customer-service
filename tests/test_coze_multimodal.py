"""CozeService.run_workflow 多模态字段解析测试。

mock httpx.AsyncClient.stream,构造 SSE 流验证:
    - content 嵌套 JSON 含 images/videos/files 时,run_workflow 返回值能带出
    - 多 message 事件的 images 数组去重合并
"""
from __future__ import annotations

import json
from contextlib import asynccontextmanager
from unittest.mock import MagicMock, patch

import pytest

from app.services.coze import CozeService


def _build_sse_lines(events: list[dict]) -> list[str]:
    """把事件列表 [{type, data}] 转成 SSE 行格式 (event:/data:/空行)。"""
    lines: list[str] = []
    for evt in events:
        lines.append(f"event: {evt['type']}")
        lines.append(f"data: {json.dumps(evt['data'], ensure_ascii=False)}")
        lines.append("")  # 空行 = 事件分隔符
    return lines


@pytest.fixture
def coze_service():
    """构造 CozeService 实例。"""
    return CozeService()


class TestCozeRunWorkflowMultimodal:
    @pytest.mark.asyncio
    async def test_message_event_with_images(self, coze_service):
        """单个 message 事件, content 嵌套 JSON 里有 images 数组"""
        events = [
            {
                "type": "Message",
                "data": {
                    "node_type": "End",
                    "node_title": "End",
                    "content": json.dumps({
                        "output": "这是产品图",
                        "images": ["https://oss.example.com/a.jpg", "https://oss.example.com/b.jpg"],
                    }, ensure_ascii=False),
                    "content_type": "text",
                },
            },
            {"type": "Done", "data": {"debug_url": "https://coze.cn/x", "node_execute_uuid": ""}},
        ]
        lines = _build_sse_lines(events)

        @asynccontextmanager
        async def _stream(*args, **kwargs):
            mock_resp = MagicMock()
            mock_resp.raise_for_status = MagicMock()
            mock_resp.aiter_lines = _make_aiter_lines(lines)
            yield mock_resp

        with patch.object(coze_service.client, "stream", side_effect=_stream):
            result = await coze_service.run_workflow({"text": "hi"}, user_id="u1")

        assert result["text"] == "这是产品图"
        assert result["images"] == ["https://oss.example.com/a.jpg", "https://oss.example.com/b.jpg"]
        assert result["videos"] == []
        assert result["files"] == []
        # 兼容旧字段
        assert result["reply_content"]["text"]["content"] == "这是产品图"

    @pytest.mark.asyncio
    async def test_message_event_with_all_modalities(self, coze_service):
        """message 事件同时含 images + videos + files"""
        events = [
            {
                "type": "Message",
                "data": {
                    "content": json.dumps({
                        "output": "完整多模态",
                        "images": ["https://oss/a.jpg"],
                        "videos": ["https://oss/v.mp4"],
                        "files": ["https://oss/d.pdf"],
                    }, ensure_ascii=False),
                },
            },
            {"type": "Done", "data": {}},
        ]
        lines = _build_sse_lines(events)

        @asynccontextmanager
        async def _stream(*args, **kwargs):
            mock_resp = MagicMock()
            mock_resp.raise_for_status = MagicMock()
            mock_resp.aiter_lines = _make_aiter_lines(lines)
            yield mock_resp

        with patch.object(coze_service.client, "stream", side_effect=_stream):
            result = await coze_service.run_workflow({"text": "hi"}, user_id="u1")

        assert result["text"] == "完整多模态"
        assert result["images"] == ["https://oss/a.jpg"]
        assert result["videos"] == ["https://oss/v.mp4"]
        assert result["files"] == ["https://oss/d.pdf"]

    @pytest.mark.asyncio
    async def test_multiple_message_events_dedup(self, coze_service):
        """多个 message 事件, images 数组要去重"""
        events = [
            {
                "type": "Message",
                "data": {"content": json.dumps({"output": "第一段", "images": ["https://oss/a.jpg"]})},
            },
            {
                "type": "Message",
                "data": {"content": json.dumps({"output": "第二段", "images": ["https://oss/a.jpg", "https://oss/b.jpg"]})},
            },
            {"type": "Done", "data": {}},
        ]
        lines = _build_sse_lines(events)

        @asynccontextmanager
        async def _stream(*args, **kwargs):
            mock_resp = MagicMock()
            mock_resp.raise_for_status = MagicMock()
            mock_resp.aiter_lines = _make_aiter_lines(lines)
            yield mock_resp

        with patch.object(coze_service.client, "stream", side_effect=_stream):
            result = await coze_service.run_workflow({"text": "hi"}, user_id="u1")

        # images 去重合并
        assert result["images"] == ["https://oss/a.jpg", "https://oss/b.jpg"]
        # text 聚合两段
        assert "第一段" in result["text"]
        assert "第二段" in result["text"]

    @pytest.mark.asyncio
    async def test_message_event_no_multimodal(self, coze_service):
        """message 事件只有 text, 返回值 images/videos/files 都是空列表"""
        events = [
            {
                "type": "Message",
                "data": {"content": json.dumps({"output": "纯文本回答"})},
            },
            {"type": "Done", "data": {}},
        ]
        lines = _build_sse_lines(events)

        @asynccontextmanager
        async def _stream(*args, **kwargs):
            mock_resp = MagicMock()
            mock_resp.raise_for_status = MagicMock()
            mock_resp.aiter_lines = _make_aiter_lines(lines)
            yield mock_resp

        with patch.object(coze_service.client, "stream", side_effect=_stream):
            result = await coze_service.run_workflow({"text": "hi"}, user_id="u1")

        assert result["text"] == "纯文本回答"
        assert result["images"] == []
        assert result["videos"] == []
        assert result["files"] == []

    @pytest.mark.asyncio
    async def test_message_event_images_only_no_text(self, coze_service):
        """只有 images 没文本也能正确返回 (一期设计: markdown 内嵌图无文本场景)"""
        events = [
            {
                "type": "Message",
                "data": {"content": json.dumps({"images": ["https://oss/a.jpg"]})},
            },
            {"type": "Done", "data": {}},
        ]
        lines = _build_sse_lines(events)

        @asynccontextmanager
        async def _stream(*args, **kwargs):
            mock_resp = MagicMock()
            mock_resp.raise_for_status = MagicMock()
            mock_resp.aiter_lines = _make_aiter_lines(lines)
            yield mock_resp

        with patch.object(coze_service.client, "stream", side_effect=_stream):
            result = await coze_service.run_workflow({"text": "hi"}, user_id="u1")

        assert result["text"] == ""
        assert result["images"] == ["https://oss/a.jpg"]

    @pytest.mark.asyncio
    async def test_no_message_events_empty_arrays(self, coze_service):
        """没有任何 message 事件, 返回值依然带空数组字段"""
        events = [
            {"type": "Done", "data": {"debug_url": "https://coze.cn/x"}},
        ]
        lines = _build_sse_lines(events)

        @asynccontextmanager
        async def _stream(*args, **kwargs):
            mock_resp = MagicMock()
            mock_resp.raise_for_status = MagicMock()
            mock_resp.aiter_lines = _make_aiter_lines(lines)
            yield mock_resp

        with patch.object(coze_service.client, "stream", side_effect=_stream):
            result = await coze_service.run_workflow({"text": "hi"}, user_id="u1")

        # 没有 message 事件时, 走 done_data 兜底分支, 仍带空数组字段
        # (但此时 done_data 是 SSE meta, 不含 reply_content)
        assert result["text"] == ""
        assert result["images"] == []
        assert result["videos"] == []
        assert result["files"] == []


def _make_aiter_lines(lines):
    """构造 aiter_lines 方法, 调用后返回 async generator。"""
    async def aiter_lines():
        for line in lines:
            yield line

    return aiter_lines