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


def test_m4_constructs_only_a_client_even_when_b_token_is_configured(
    mock_dify_client_cls,
):
    cls, instance = mock_dify_client_cls
    svc = DifyService()

    assert set(svc._clients) == {"A"}
    assert svc.client is instance
    assert cls.call_count == 1


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


@pytest.mark.parametrize(
    "filename,expected_mime",
    [
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
    ],
)
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
# Chatflow 模式 (Dify advanced-chat / Chatflow app)
# ---------------------------------------------------------------------------

# 真实 chatflow 阻塞响应 (按 Dify 官方文档 /chat-messages 的 ChatCompletionResponse 形态)
CHATFLOW_BLOCKING_RESPONSE = {
    "event": "message",
    "task_id": "cf-task-001",
    "id": "cf-msg-001",
    "message_id": "cf-msg-001",
    "conversation_id": "cf-conv-001",
    "mode": "advanced-chat",
    "answer": "您好,我是充电桩智能客服,请问您想咨询什么?",
    "metadata": {
        "retriever_resources": [
            {
                "position": 1,
                "dataset_id": "ds-1",
                "dataset_name": "充电桩知识库",
                "document_id": "doc-1",
                "document_name": "功能位置FAQ.md",
                "segment_id": "seg-1",
                "score": 0.92,
                "content": "1. 功能位置 2. 业务规则 3. 流程问题 4. Bug反馈",
            }
        ],
        "usage": {"total_tokens": 100},
    },
    "created_at": 1705407629,
}


@pytest.fixture
def chatflow_service(monkeypatch):
    """Patch DifyClient 模拟 chatflow 模式 (mock run_chatflow)。"""
    monkeypatch.setattr("app.services.dify.settings.dify.app_mode", "chatflow")
    with patch("app.services.dify.DifyClient") as cls:
        instance = MagicMock(spec=DifyClient)
        instance.upload_file = AsyncMock(return_value="dify-file-uuid-xxx")
        instance.run_chatflow = AsyncMock(return_value=CHATFLOW_BLOCKING_RESPONSE)
        instance.run_workflow = AsyncMock(return_value=CHATFLOW_BLOCKING_RESPONSE)
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
        instance.app_mode = "chatflow"
        cls.return_value = instance
        yield DifyService(), instance


async def test_chatflow_mode_calls_run_chatflow(chatflow_service):
    """chatflow 模式应调 DifyClient.run_chatflow 而非 run_workflow。"""
    svc, client = chatflow_service
    await svc.run_workflow({"text": "你好"}, user_id="wx-user-1")
    client.run_chatflow.assert_awaited_once()
    client.run_workflow.assert_not_awaited()


async def test_chatflow_passes_conversation_id_for_continuation(chatflow_service):
    """传入 conversation_id 时应透传给 run_chatflow, 续接多轮会话。"""
    svc, client = chatflow_service
    await svc.run_workflow(
        {"text": "第二轮"}, user_id="wx-user-1", conversation_id="cf-conv-001"
    )
    call_kwargs = client.run_chatflow.await_args.kwargs
    assert call_kwargs["conversation_id"] == "cf-conv-001"


async def test_chatflow_first_turn_passes_empty_conversation_id(chatflow_service):
    """首次会话 (conversation_id=None) 应传空串, Dify 新建会话。"""
    svc, client = chatflow_service
    await svc.run_workflow({"text": "首轮"}, user_id="wx-user-1")
    call_kwargs = client.run_chatflow.await_args.kwargs
    assert call_kwargs["conversation_id"] == ""


async def test_chatflow_returns_conversation_id_for_persistence(chatflow_service):
    """chatflow 返回 dict 顶层应携带 conversation_id, 供 ConversationStore 持久化。"""
    svc, _client = chatflow_service
    result = await svc.run_workflow({"text": "首轮"}, user_id="wx-user-1")
    assert result["conversation_id"] == "cf-conv-001"


async def test_chatflow_mode_passes_query_field(chatflow_service):
    """chatflow 请求的 query 字段 = 用户文本 (不是 inputs.input_text)。"""
    svc, client = chatflow_service
    await svc.run_workflow({"text": "充电桩有问题"}, user_id="wx-user-1")
    call_kwargs = client.run_chatflow.await_args.kwargs
    assert call_kwargs["query"] == "充电桩有问题"
    # 不应再用 input_text 之类的 workflow 变量名
    assert "inputs" not in call_kwargs or not call_kwargs["inputs"]


async def test_chatflow_mode_extracts_answer_as_content(chatflow_service):
    """chatflow 阻塞响应的 answer 字段提取为 result['content']。"""
    svc, _ = chatflow_service
    result = await svc.run_workflow({"text": "你好"}, user_id="wx-user-1")
    assert result["content"] == "您好,我是充电桩智能客服,请问您想咨询什么?"
    assert result["content_type"] == "text"
    assert result["node_type"] == "dify_chatflow"


async def test_chatflow_mode_normalizes_retriever_resources_to_knowledge(
    chatflow_service,
):
    """metadata.retriever_resources 归一化到 outputs.knowledge, 让现有 trace formatter 无需改。

    Chatflow 形态: {position, dataset_name, document_name, score, content}
    Workflow 归一形态: {title, content, metadata: {score, segment_word_count}}
    """
    svc, _ = chatflow_service
    result = await svc.run_workflow({"text": "x"}, user_id="u")
    knowledge = result["raw"]["data"]["outputs"]["knowledge"]
    assert len(knowledge) == 1
    chunk = knowledge[0]
    # title 来自 document_name
    assert chunk["title"] == "功能位置FAQ.md"
    assert chunk["content"] == "1. 功能位置 2. 业务规则 3. 流程问题 4. Bug反馈"
    # metadata.score 保留, segment_word_count 用 content 长度填充
    assert chunk["metadata"]["score"] == 0.92
    assert chunk["metadata"]["segment_word_count"] == len(chunk["content"])


async def test_chatflow_mode_answer_normalized_to_outputs_text(chatflow_service):
    """answer 字段同时写到 outputs.text, 让 extract_assistant_text 直接命中。"""
    svc, _ = chatflow_service
    result = await svc.run_workflow({"text": "x"}, user_id="u")
    outputs = result["raw"]["data"]["outputs"]
    assert outputs["text"] == "您好,我是充电桩智能客服,请问您想咨询什么?"


async def test_chatflow_mode_no_retriever_resources(chatflow_service):
    """metadata.retriever_resources 缺失或为空 → outputs.knowledge=[] (不报错)。"""
    svc, client = chatflow_service
    client.run_chatflow.return_value = {
        "event": "message",
        "answer": "纯回答,无检索",
        "metadata": {},
        "conversation_id": "x",
    }
    result = await svc.run_workflow({"text": "x"}, user_id="u")
    assert result["raw"]["data"]["outputs"]["knowledge"] == []
    assert result["content"] == "纯回答,无检索"


async def test_chatflow_inputs_empty_when_no_config_no_passthrough(chatflow_service):
    """F3: 无配置且 input_data 无 hint → inputs 为空 (行为不变, Dify 用字段 default)。"""
    svc, client = chatflow_service
    await svc.run_workflow({"text": "你好"}, user_id="u")
    call_kwargs = client.run_chatflow.await_args.kwargs
    assert call_kwargs["inputs"] == {}


async def test_chatflow_inputs_from_deployment_config(chatflow_service, monkeypatch):
    """F3: 部署级配置 (DIFY_CHATFLOW_INPUT_*) 注入到 chatflow inputs。"""
    monkeypatch.setattr("app.services.dify.settings.dify.chatflow_input_language", "zh")
    monkeypatch.setattr(
        "app.services.dify.settings.dify.chatflow_input_hint_endpoint", "user"
    )
    monkeypatch.setattr(
        "app.services.dify.settings.dify.chatflow_input_hint_region", "cn"
    )
    svc, client = chatflow_service
    await svc.run_workflow({"text": "你好"}, user_id="u")
    call_kwargs = client.run_chatflow.await_args.kwargs
    assert call_kwargs["inputs"] == {
        "input_language": "zh",
        "input_hint_endpoint": "user",
        "input_hint_region": "cn",
    }


async def test_chatflow_inputs_passthrough_overrides_config(
    chatflow_service, monkeypatch
):
    """F3: input_data 透传值 (language/hint_endpoint/hint_region) 覆盖部署级配置。"""
    monkeypatch.setattr("app.services.dify.settings.dify.chatflow_input_language", "zh")
    monkeypatch.setattr(
        "app.services.dify.settings.dify.chatflow_input_hint_region", "cn"
    )
    svc, client = chatflow_service
    await svc.run_workflow(
        {"text": "你好", "language": "en", "hint_endpoint": "butler"},
        user_id="u",
    )
    call_kwargs = client.run_chatflow.await_args.kwargs
    # 透传优先: language=en 覆盖 zh; hint_endpoint=butler (配置未设); region 走配置 cn
    assert call_kwargs["inputs"] == {
        "input_language": "en",
        "input_hint_endpoint": "butler",
        "input_hint_region": "cn",
    }


async def test_chatflow_inputs_only_non_empty_passed(chatflow_service, monkeypatch):
    """F3: 空值不进 inputs (避免覆盖 Dify 字段 default)。"""
    monkeypatch.setattr(
        "app.services.dify.settings.dify.chatflow_input_hint_endpoint", "  "
    )
    svc, client = chatflow_service
    await svc.run_workflow(
        {"text": "你好", "language": "  ", "hint_region": ""},
        user_id="u",
    )
    call_kwargs = client.run_chatflow.await_args.kwargs
    assert call_kwargs["inputs"] == {}


async def test_chatflow_mode_error_wrapped(chatflow_service):
    """chatflow 错误包装成 AIBackendError。"""
    from app.services.dify_client import DifyError

    svc, client = chatflow_service
    client.run_chatflow.side_effect = DifyError("chat-messages HTTP 400: invalid_param")
    with pytest.raises(AIBackendError) as ei:
        await svc.run_workflow({"text": "x"}, user_id="u")
    assert "Dify chatflow 失败" in str(ei.value)


async def test_workflow_mode_backward_compat_when_app_mode_workflow(monkeypatch):
    """app_mode=workflow 时, 仍然调 run_workflow (旧逻辑不变)。"""
    monkeypatch.setattr("app.services.dify.settings.dify.app_mode", "workflow")
    with patch("app.services.dify.DifyClient") as cls:
        instance = MagicMock(spec=DifyClient)
        instance.run_workflow = AsyncMock(
            return_value={
                "task_id": "t1",
                "data": {"status": "succeeded", "outputs": {"output": "工作流回答"}},
            }
        )
        instance.run_chatflow = AsyncMock()
        instance.file_ref = staticmethod(
            lambda fid, ft: {
                "type": ft,
                "transfer_method": "local_file",
                "upload_file_id": fid,
            }
        )
        instance.api_base = "https://api.dify.ai/v1"
        instance.api_key = "app-test"
        instance.end_user = "default-end-user"
        instance.upload_timeout = 60.0
        instance.workflow_timeout = 120.0
        instance.app_mode = "workflow"
        cls.return_value = instance
        svc = DifyService()
        await svc.run_workflow({"text": "x"}, user_id="u")
        instance.run_workflow.assert_awaited_once()
        instance.run_chatflow.assert_not_awaited()


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


def test_factory_unknown_backend_falls_back_to_dify():
    from app.core.config import settings
    from app.services import get_ai_service, DifyService

    original = settings.app.ai_backend
    settings.app.ai_backend = "bogus"
    try:
        svc = get_ai_service()
        assert isinstance(svc, DifyService)
    finally:
        settings.app.ai_backend = original


async def test_chatflow_mode_files_array_image_bytes(chatflow_service):
    """file_image_bytes -> client.upload_file -> files 数组 (local_file)。

    重构后图片统一走 bytes (上传延后到发送点按目标 app, Dify 文件库按 app 隔离,
    改投 A->B 时文件归属自动正确)。替代旧 file_image_id/file_image_url 契约测试。
    """
    svc, client = chatflow_service
    await svc.run_workflow(
        {"text": "看图", "file_image_bytes": b"\x89PNG fake", "file_image_name": "x.png"},
        user_id="wx-user-1",
    )
    client.upload_file.assert_awaited_once()
    call_kwargs = client.run_chatflow.await_args.kwargs
    assert call_kwargs["files"] == [
        {
            "type": "image",
            "transfer_method": "local_file",
            "upload_file_id": "dify-file-uuid-xxx",
        }
    ]
