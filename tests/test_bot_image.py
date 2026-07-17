"""智能机器人 bot 接收 image / voice 消息的编排测试。"""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.services.dify import DifyService
from app.services.dify_client import DifyClient


# ---------------------------------------------------------------------------
# dify.py run_workflow (chatflow) 接受 file_image_bytes (上传延后到发送点)
# ---------------------------------------------------------------------------

class TestDifyRunWorkflowAcceptsFileInputs:
    @pytest.mark.asyncio
    async def test_no_file_inputs_only_text(self, monkeypatch):
        """纯文本消息 file_*_id 缺失, 不构造 file_ref"""
        with patch("app.services.dify.DifyClient") as MockClientCls:
            mock_client = MagicMock()
            mock_client.run_workflow = AsyncMock(return_value={
                "task_id": "t1",
                "data": {"status": "succeeded", "outputs": {"output": "ok"}},
            })
            MockClientCls.return_value = mock_client

            from app.core.config import settings
            monkeypatch.setattr(settings.dify, "input_text", "input_text")
            monkeypatch.setattr(settings.dify, "input_image", "input_img")
            monkeypatch.setattr(settings.dify, "input_audio", "input_audio")

            svc = DifyService()

            await svc.run_workflow(
                {"text": "纯文本", "user_id": "u1"},
                user_id="u1",
            )

            call_kwargs = mock_client.run_workflow.call_args.kwargs
            inputs = call_kwargs["inputs"]
            # input_text 必须传, user_id 透传是正常的 (一期改造: _NON_FILE_KEYS)
            assert inputs["input_text"] == "纯文本"
            assert "input_img_id" not in inputs
            assert "input_audio_id" not in inputs

