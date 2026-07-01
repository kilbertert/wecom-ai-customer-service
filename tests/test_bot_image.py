"""智能机器人 bot 接收 image / voice 消息的编排测试。"""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.services.dify import DifyService
from app.services.dify_client import DifyClient


# ---------------------------------------------------------------------------
# _upload_to_dify_file_store
# ---------------------------------------------------------------------------

class TestUploadToDifyFileStore:
    @pytest.mark.asyncio
    async def test_uploads_image_bytes(self):
        """image 字节流调 DifyClient.upload_file, 返回 Dify file_id"""
        fake_bytes = b"\xff\xd8\xff\xe0fake_jpeg_bytes"
        fake_dify_id = "dify-uuid-abc-123"

        mock_client = MagicMock(spec=DifyClient)
        mock_client.upload_file = AsyncMock(return_value=fake_dify_id)
        mock_ai = MagicMock()
        mock_ai.client = mock_client

        from app.services.message_processor import _upload_to_dify_file_store
        result = await _upload_to_dify_file_store(
            mock_ai, fake_bytes, "wechat_media_xxx", "image",
        )

        assert result == fake_dify_id
        mock_client.upload_file.assert_called_once()
        call_kwargs = mock_client.upload_file.call_args.kwargs
        assert call_kwargs["content"] == fake_bytes
        assert call_kwargs["content_type"] == "image/jpeg"
        assert call_kwargs["filename"].endswith(".jpg")

    @pytest.mark.asyncio
    async def test_uploads_audio_bytes(self):
        """voice 字节流以 audio mime 上传"""
        fake_bytes = b"#!AMR\nfake_audio"
        fake_dify_id = "dify-uuid-audio-456"

        mock_client = MagicMock(spec=DifyClient)
        mock_client.upload_file = AsyncMock(return_value=fake_dify_id)
        mock_ai = MagicMock()
        mock_ai.client = mock_client

        from app.services.message_processor import _upload_to_dify_file_store
        result = await _upload_to_dify_file_store(
            mock_ai, fake_bytes, "wechat_voice_yyy", "audio",
        )

        assert result == fake_dify_id
        call_kwargs = mock_client.upload_file.call_args.kwargs
        assert call_kwargs["content_type"] == "audio/amr"
        assert call_kwargs["filename"].endswith(".amr")

    @pytest.mark.asyncio
    async def test_raises_when_no_client(self):
        """非 Dify 后端 (无 client.upload_file) 应抛 RuntimeError"""
        mock_ai = MagicMock(spec=[])  # 没有 client 属性

        from app.services.message_processor import _upload_to_dify_file_store
        with pytest.raises(RuntimeError, match="不支持文件上传"):
            await _upload_to_dify_file_store(mock_ai, b"x", "m", "image")


# ---------------------------------------------------------------------------
# dify.py run_workflow 接受 file_image_id / file_voice_id
# ---------------------------------------------------------------------------

class TestDifyRunWorkflowAcceptsFileInputs:
    @pytest.mark.asyncio
    async def test_file_image_id_constructed_as_file_ref(self, monkeypatch):
        """dify.py run_workflow 接受 {text, file_image_id} 形态,
        内部构造 file_ref 喂给 workflow inputs[input_image]"""
        # 通过 patch "app.services.dify.DifyClient" 替换 svc.client 的实例化
        with patch("app.services.dify.DifyClient") as MockClientCls:
            mock_client = MagicMock()
            mock_client.file_ref = staticmethod(
                lambda fid, t: {"type": t, "transfer_method": "local_file", "upload_file_id": fid}
            )
            mock_client.run_workflow = AsyncMock(return_value={
                "task_id": "t1",
                "data": {"status": "succeeded", "outputs": {"output": "ok"}},
            })
            MockClientCls.return_value = mock_client

            from app.core.config import settings
            monkeypatch.setattr(settings.dify, "input_text", "input_text")
            monkeypatch.setattr(settings.dify, "input_image", "input_img")
            monkeypatch.setattr(settings.dify, "input_audio", "input_audio")

            svc = DifyService()  # 会用 MockClientCls 实例化 client

            await svc.run_workflow(
                {"text": "看图", "file_image_id": "dify-uuid-xyz", "user_id": "u1"},
                user_id="u1",
            )

            call_kwargs = mock_client.run_workflow.call_args.kwargs
            inputs = call_kwargs["inputs"]
            assert inputs["input_text"] == "看图"
            assert "input_img" in inputs
            assert inputs["input_img"] == [{
                "type": "image",
                "transfer_method": "local_file",
                "upload_file_id": "dify-uuid-xyz",
            }]

    @pytest.mark.asyncio
    async def test_file_voice_id_constructed_as_file_ref(self, monkeypatch):
        with patch("app.services.dify.DifyClient") as MockClientCls:
            mock_client = MagicMock()
            mock_client.file_ref = staticmethod(
                lambda fid, t: {"type": t, "transfer_method": "local_file", "upload_file_id": fid}
            )
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
                {"text": "听音", "file_voice_id": "dify-uuid-audio-789", "user_id": "u1"},
                user_id="u1",
            )

            call_kwargs = mock_client.run_workflow.call_args.kwargs
            inputs = call_kwargs["inputs"]
            assert inputs["input_audio"] == [{
                "type": "audio",
                "transfer_method": "local_file",
                "upload_file_id": "dify-uuid-audio-789",
            }]

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

    @pytest.mark.asyncio
    async def test_file_image_url_remote_url_mode(self, monkeypatch):
        """file_image_url 触发 Dify remote_url 模式 (跳过 /v1/files/upload)"""
        with patch("app.services.dify.DifyClient") as MockClientCls:
            mock_client = MagicMock()
            mock_client.file_ref = staticmethod(
                lambda fid, t: {"type": t, "transfer_method": "local_file", "upload_file_id": fid}
            )
            mock_client.run_workflow = AsyncMock(return_value={
                "task_id": "t1",
                "data": {"status": "succeeded", "outputs": {"output": "ok"}},
            })
            MockClientCls.return_value = mock_client

            from app.core.config import settings
            monkeypatch.setattr(settings.dify, "input_text", "input_text")
            monkeypatch.setattr(settings.dify, "input_image", "input_img_id")
            monkeypatch.setattr(settings.dify, "input_audio", "input_audio_id")

            svc = DifyService()

            await svc.run_workflow(
                {"text": "看图", "file_image_url": "https://ww-aibot-img.cos.ap-guangzhou/xxx.jpg", "user_id": "u1"},
                user_id="u1",
            )

            call_kwargs = mock_client.run_workflow.call_args.kwargs
            inputs = call_kwargs["inputs"]
            assert inputs["input_text"] == "看图"
            assert inputs["input_img_id"] == [{
                "type": "image",
                "transfer_method": "remote_url",
                "url": "https://ww-aibot-img.cos.ap-guangzhou/xxx.jpg",
            }]

    @pytest.mark.asyncio
    async def test_file_image_url_priority_over_id(self, monkeypatch):
        """file_image_url 优先于 file_image_id (避免重复)"""
        with patch("app.services.dify.DifyClient") as MockClientCls:
            mock_client = MagicMock()
            mock_client.run_workflow = AsyncMock(return_value={
                "task_id": "t1",
                "data": {"status": "succeeded", "outputs": {"output": "ok"}},
            })
            MockClientCls.return_value = mock_client

            from app.core.config import settings
            monkeypatch.setattr(settings.dify, "input_text", "input_text")
            monkeypatch.setattr(settings.dify, "input_image", "input_img_id")
            monkeypatch.setattr(settings.dify, "input_audio", "input_audio_id")

            svc = DifyService()

            await svc.run_workflow(
                {
                    "text": "看图",
                    "file_image_url": "https://cdn.example.com/a.jpg",  # 优先
                    "file_image_id": "should-be-ignored",
                    "user_id": "u1",
                },
                user_id="u1",
            )

            call_kwargs = mock_client.run_workflow.call_args.kwargs
            inputs = call_kwargs["inputs"]
            # 只应该走 remote_url, 不应该有 upload_file_id (file_ref)
            assert inputs["input_img_id"] == [{
                "type": "image",
                "transfer_method": "remote_url",
                "url": "https://cdn.example.com/a.jpg",
            }]