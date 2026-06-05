"""微信相关异步任务"""
from typing import Dict, Any
from app.core.celery_app import celery_app

from app.services.wechat import WeChatService
from app.services.standardization import DataStandardizationService


@celery_app.task(bind=True, max_retries=3)
def process_wechat_message(self, message_data: Dict[str, Any]) -> Dict[str, Any]:
    """处理微信消息"""
    try:
        # 初始化服务（单轮对话模式）
        wechat_service = WeChatService()
        standardization_service = DataStandardizationService()  # 无会话服务

        # 这里实现消息处理逻辑
        # 由于需要异步上下文，这里简化处理

        return {
            "status": "processed",
            "message_id": message_data.get("msgid"),
            "user_id": message_data.get("external_userid")
        }

    except Exception as e:
        # 重试逻辑
        if self.request.retries < self.max_retries:
            raise self.retry(countdown=2 ** self.request.retries, exc=e)
        else:
            return {
                "status": "failed",
                "error": str(e),
                "message_id": message_data.get("msgid")
            }


@celery_app.task(bind=True, max_retries=3)
async def send_wechat_reply(self, reply_data: Dict[str, Any]) -> Dict[str, Any]:
    """发送微信回复"""
    try:
        wechat_service = WeChatService()

        # 发送消息逻辑
        result = await wechat_service.send_message(reply_data)

        return {
            "status": "sent",
            "result": result
        }

    except Exception as e:
        if self.request.retries < self.max_retries:
            raise self.retry(countdown=2 ** self.request.retries, exc=e)
        else:
            return {
                "status": "failed",
                "error": str(e)
            }


@celery_app.task
def sync_wechat_messages(token: str, cursor: str = None) -> Dict[str, Any]:
    """同步微信消息"""
    try:
        wechat_service = WeChatService()

        # 同步消息逻辑
        # 这里需要实现完整的同步逻辑

        return {
            "status": "synced",
            "token": token,
            "messages_count": 0
        }

    except Exception as e:
        return {
            "status": "failed",
            "error": str(e),
            "token": token
        }