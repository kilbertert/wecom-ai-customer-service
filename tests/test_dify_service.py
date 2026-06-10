"""DifyService 单元测试 (mock DifyClient,不发起真实 HTTP 请求)。"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.exceptions import AIBackendError
from app.services.dify import DifyService
from app.services.dify_client import DifyClient, DifyError


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_dify_client_cls():
    """Patch DifyService 内部的 DifyClient 构造,返回可注入 AsyncMock 的替身。"""
    with patch("app.services.dify.DifyClient") as cls:
        instance = MagicMock(spec=DifyClient)
        instance.upload_file = AsyncMock(return_value="dify-file-uuid-xxx")
        instance.run_workflow = AsyncMock(
            return_value={
                "task_id": "t1",
                "workflow_run_id": "r1",
                "data": {
                    "status": "succeeded",
                    "outputs": {"output": "你好,我是 Dify 智能体"},
                },
            }
        )
        instance.file_ref = staticmethod(
            lambda upload_file_id, file_type: {
                "type": file_type,
                "transfer_method": "local_file",
                "upload_file_id": upload_file_id,
            }
        )
        instance.api_base = "https://api.dify.ai/v1"
        instance.api_key = "app-test"
        instance.end_user = "default-end-user"
        instance.upload_timeout = 60.0
        instance.workflow_timeout = 120.0
        cls.return_value = instance
        yield cls, instance


@pytest.fixture
def service(mock_dify_client_cls):
    _cls, _instance = mock_dify_client_cls
    return DifyService(), _instance


# ---------------------------------------------------------------------------
# upload_file
# ---------------------------------------------------------------------------

async def test_upload_file_passes_through_dify_client(service):
    svc, client = service
    result = await svc.upload_file(b"\x89PNG\r\n\x1a\n", "wechat_image_abc.jpg")
    assert result == "dify-file-uuid-xxx"
    client.upload_file.assert_awaited_once()
    kwargs = client.upload_file.await_args.kwargs
    assert kwargs["filename"] == "wechat_image_abc.jpg"
    assert kwargs["content"] == b"\x89PNG\r\n\x1a\n"
    assert kwargs["content_type"] == "image/jpeg"


@pytest.mark.parametrize("filename,expected_mime", [
    ("a.jpg", "image/jpeg"),
    ("a.jpeg", "image/jpeg"),
    ("a.PNG", "image/png"),
    ("a.webp", "image/webp"),
    ("a.gif", "image/gif"),
    ("a.wav", "audio/wav"),
    ("a.mp3", "audio/mpeg"),
    ("a.m4a", "audio/mp4"),
    ("a.webm", "audio/webm"),
    ("a.amr", "audio/amr"),
    ("a.unknown_ext", "application/octet-stream"),
])
async def test_upload_file_mime_inference(service, filename, expected_mime):
    svc, client = service
    await svc.upload_file(b"x", filename)
    assert client.upload_file.await_args.kwargs["content_type"] == expected_mime


async def test_upload_file_wraps_dify_error(service):
    svc, client = service
    client.upload_file.side_effect = DifyError("upload 401")
    with pytest.raises(AIBackendError) as ei:
        await svc.upload_file(b"x", "a.jpg")
    assert "Dify 文件上传失败" in str(ei.value)
    assert isinstance(ei.value.__cause__, DifyError)


# ---------------------------------------------------------------------------
# run_workflow — input shape variants
# ---------------------------------------------------------------------------

async def test_run_workflow_text_only(service):
    svc, client = service
    result = await svc.run_workflow({"text": "你好"}, user_id="wx-user-1")
    sent_inputs = client.run_workflow.await_args.kwargs["inputs"]
    assert sent_inputs == {"input_text": "你好"}
    assert result["content"] == "你好,我是 Dify 智能体"
    assert result["content_type"] == "text"
    assert result["node_type"] == "dify_workflow"
    assert "raw" in result


async def test_run_workflow_text_plus_image(service):
    svc, client = service
    await svc.upload_file(b"img-bytes", "x.jpg")
    await svc.run_workflow(
        {"text": "看图", "file_image_id": "dify-file-uuid-xxx"},
        user_id="wx-user-1",
    )
    sent_inputs = client.run_workflow.await_args.kwargs["inputs"]
    assert sent_inputs["input_text"] == "看图"
    assert sent_inputs["input_img_id"] == [
        {"type": "image", "transfer_method": "local_file", "upload_file_id": "dify-file-uuid-xxx"}
    ]


async def test_run_workflow_text_plus_voice(service):
    svc, client = service
    await svc.run_workflow(
        {"text": "听声音", "file_voice_id": "voice-uuid"},
        user_id="wx-user-1",
    )
    sent_inputs = client.run_workflow.await_args.kwargs["inputs"]
    assert sent_inputs["input_text"] == "听声音"
    assert sent_inputs["input_audio_id"] == [
        {"type": "audio", "transfer_method": "local_file", "upload_file_id": "voice-uuid"}
    ]


async def test_run_workflow_image_only_no_text(service):
    svc, client = service
    await svc.run_workflow({"file_image_id": "img-uuid"}, user_id="u")
    sent_inputs = client.run_workflow.await_args.kwargs["inputs"]
    assert "input_text" not in sent_inputs
    assert sent_inputs["input_img_id"][0]["upload_file_id"] == "img-uuid"


async def test_run_workflow_empty_input_uses_default_text(service):
    svc, client = service
    await svc.run_workflow({}, user_id="u")
    sent_inputs = client.run_workflow.await_args.kwargs["inputs"]
    assert sent_inputs == {"input_text": "收到您的消息"}


async def test_run_workflow_passthrough_dict(service):
    svc, client = service
    await svc.run_workflow({"some_custom_key": "v", "another": 1}, user_id="u")
    sent_inputs = client.run_workflow.await_args.kwargs["inputs"]
    assert sent_inputs == {"some_custom_key": "v", "another": 1}


async def test_run_workflow_non_dict_input_falls_back_to_text(service):
    svc, client = service
    await svc.run_workflow(12345, user_id="u")
    sent_inputs = client.run_workflow.await_args.kwargs["inputs"]
    assert sent_inputs == {"input_text": "12345"}


# ---------------------------------------------------------------------------
# run_workflow — error wrapping
# ---------------------------------------------------------------------------

async def test_run_workflow_wraps_dify_error(service):
    svc, client = service
    client.run_workflow.side_effect = DifyError("workflow failed: node X")
    with pytest.raises(AIBackendError) as ei:
        await svc.run_workflow({"text": "x"}, user_id="u")
    assert "Dify workflow 失败" in str(ei.value)
    assert isinstance(ei.value.__cause__, DifyError)


# ---------------------------------------------------------------------------
# run_workflow — thinking block + 深度回退
# ---------------------------------------------------------------------------

async def test_run_workflow_strips_thinking_block(service):
    svc, client = service
    client.run_workflow.return_value = {
        "data": {
            "status": "succeeded",
            "outputs": {"output": "<think>\nchain-of-thought\n</think>\nFinal answer"},
        }
    }
    result = await svc.run_workflow({"text": "x"}, user_id="u")
    assert result["content"] == "Final answer"
    assert "<think>" not in result["content"]


async def test_run_workflow_falls_back_to_other_output_keys(service):
    svc, client = service
    client.run_workflow.return_value = {
        "data": {"status": "succeeded", "outputs": {"answer": "兜底命中"}}
    }
    result = await svc.run_workflow({"text": "x"}, user_id="u")
    assert result["content"] == "兜底命中"


async def test_run_workflow_no_outputs_returns_nonempty_string(service):
    svc, client = service
    client.run_workflow.return_value = {"data": {"status": "succeeded", "outputs": {}}}
    result = await svc.run_workflow({"text": "x"}, user_id="u")
    assert isinstance(result["content"], str)
    assert len(result["content"]) > 0


# ---------------------------------------------------------------------------
# close
# ---------------------------------------------------------------------------

async def test_close_does_not_raise(service):
    svc, _ = service
    await svc.close()


# ---------------------------------------------------------------------------
# Factory 路由
# ---------------------------------------------------------------------------

def test_factory_returns_dify_when_backend_is_dify(mock_dify_client_cls):
    from app.core.config import settings
    original = settings.app.ai_backend
    settings.app.ai_backend = "dify"
    try:
        from app.services import get_ai_service
        svc = get_ai_service()
        assert isinstance(svc, DifyService)
    finally:
        settings.app.ai_backend = original


def test_factory_rejects_unknown_backend():
    from app.core.config import settings
    original = settings.app.ai_backend
    settings.app.ai_backend = "bogus"
    try:
        from app.services import get_ai_service
        with pytest.raises(ValueError):
            get_ai_service()
    finally:
        settings.app.ai_backend = original
