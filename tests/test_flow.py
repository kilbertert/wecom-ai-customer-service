"""完整流程测试"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi.testclient import TestClient

from app.main import app
from app.models.wechat import WeChatMessage, MessageType
from app.models.coze import StandardizedMessage, CozeWorkflowOutput, ActionType
from app.services.wechat import WeChatService
from app.services.coze import CozeService
# SessionService removed in single-round mode
from app.services.standardization import DataStandardizationService
from app.services.media import MediaService


@pytest.fixture
def client():
    """测试客户端"""
    return TestClient(app)


@pytest.fixture
def mock_wechat_service():
    """模拟微信服务"""
    service = MagicMock(spec=WeChatService)

    # 模拟签名验证
    service.verify_callback_signature.return_value = True

    # 模拟异步关闭
    service.close = AsyncMock()

    return service


# Removed mock_session_service fixture - single-round mode doesn't use session service


@pytest.fixture
def mock_coze_service():
    """模拟Coze服务"""
    service = MagicMock(spec=CozeService)

    # 模拟工作流触发
    mock_output = CozeWorkflowOutput(
        action=ActionType.REPLY,
        reply_content={
            "msgtype": "text",
            "text": {"content": "您好，我是智能客服，请问有什么可以帮您？"}
        },
        metadata={"intent_type": "闲聊问候"}
    )
    service.trigger_workflow = AsyncMock(return_value=mock_output)

    # 模拟异步关闭
    service.close = AsyncMock()

    return service


@pytest.fixture
def mock_media_service():
    """模拟媒体服务"""
    service = MagicMock(spec=MediaService)

    # 模拟媒体处理
    service.download_and_process_media = AsyncMock(return_value={
        "media_id": "test_media_id",
        "filename": "test.jpg",
        "content_type": "image/jpeg",
        "size": 1024
    })

    return service


@pytest.fixture
def sample_wechat_message():
    """示例微信消息"""
    return WeChatMessage(
        msgid="msg_123456",
        msgtype=MessageType.TEXT,
        send_time=1705254000,
        origin=1,
        external_userid="external_user_123",
        open_kfid="kf_123",
        text={"content": "你好"}
    )


def test_wechat_callback_verification_success(client, mock_wechat_service):
    """测试微信回调验证成功"""
    with patch('app.routes.wechat.WeChatService', return_value=mock_wechat_service):
        response = client.get(
            "/wechat/kf/callback",
            params={
                "signature": "test_signature",
                "timestamp": "1234567890",
                "nonce": "test_nonce",
                "echostr": "test_echostr"
            }
        )

        assert response.status_code == 200
        assert response.text == "test_echostr"


def test_wechat_callback_verification_failure(client):
    """测试微信回调验证失败"""
    mock_service = MagicMock()
    mock_service.verify_callback_signature.return_value = False

    with patch('app.routes.wechat.WeChatService', return_value=mock_service):
        response = client.get(
            "/wechat/kf/callback",
            params={
                "signature": "invalid_signature",
                "timestamp": "1234567890",
                "nonce": "test_nonce",
                "echostr": "test_echostr"
            }
        )

        assert response.status_code == 403


@pytest.mark.asyncio
async def test_data_standardization(
    sample_wechat_message,
    mock_session_service
):
    """测试数据标准化"""
    standardization_service = DataStandardizationService(mock_session_service)

    result = await standardization_service.standardize_wechat_message(sample_wechat_message)

    assert isinstance(result, StandardizedMessage)
    assert result.user_id == "external_user_123"
    assert result.message_type == "text"
    assert result.content["text"] == "你好"


@pytest.mark.asyncio
async def test_full_message_processing_flow(
    sample_wechat_message,
    mock_wechat_service,
    mock_coze_service,
    mock_media_service
):
    """测试完整消息处理流程"""
    from app.routes.wechat import process_single_message
    from app.services.standardization import DataStandardizationService

    # 初始化标准化服务（单轮模式，无会话服务）
    standardization_service = DataStandardizationService()

    # 模拟微信发送消息
    mock_wechat_service.send_message = AsyncMock(return_value={"errcode": 0})

    # 执行消息处理
    await process_single_message(
        sample_wechat_message,
        mock_wechat_service,
        mock_coze_service,
        standardization_service,
        mock_media_service
    )

    # 验证各服务被正确调用
    mock_coze_service.trigger_workflow.assert_called_once()
    mock_wechat_service.send_message.assert_called_once()

    # 单轮模式不使用会话服务


def test_service_initialization():
    """测试服务初始化"""
    # 测试微信服务
    wechat_service = WeChatService()
    assert hasattr(wechat_service, 'verify_callback_signature')
    assert hasattr(wechat_service, 'sync_messages')
    assert hasattr(wechat_service, 'send_message')

    # 测试Coze服务
    coze_service = CozeService()
    assert hasattr(coze_service, 'trigger_workflow')

    # 测试标准化服务（单轮模式，无会话服务）
    standardization_service = DataStandardizationService()
    assert hasattr(standardization_service, 'standardize_wechat_message')
    assert standardization_service.session_service is None  # 单轮模式

    # 测试媒体服务
    media_service = MediaService(wechat_service)
    assert hasattr(media_service, 'download_and_process_media')


def test_models_import():
    """测试数据模型导入"""
    # 验证微信模型
    from app.models.wechat import WeChatMessage, MessageType, WeChatSyncRequest
    assert WeChatMessage
    assert MessageType.TEXT == "text"

    # 验证Coze模型
    from app.models.coze import StandardizedMessage, CozeWorkflowOutput, ActionType
    assert StandardizedMessage
    assert ActionType.REPLY == "reply"

    # 单轮模式不再使用会话模型


def test_routes_registration(client):
    """测试路由注册"""
    # 测试根路径
    response = client.get("/")
    assert response.status_code == 200

    # 测试服务信息
    response = client.get("/info")
    assert response.status_code == 200

    # 测试健康检查
    response = client.get("/monitoring/health")
    assert response.status_code == 200

    # 测试微信测试端点
    response = client.get("/wechat/test")
    assert response.status_code == 200


if __name__ == "__main__":
    pytest.main([__file__, "-v"])