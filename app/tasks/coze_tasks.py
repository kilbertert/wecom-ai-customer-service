"""Coze相关异步任务"""
from typing import Dict, Any
from app.core.celery_app import celery_app
import asyncio

from app.services.coze import CozeService
from app.services.standardization import DataStandardizationService


@celery_app.task(bind=True, max_retries=3)
async def trigger_coze_workflow(self, standardized_data: Dict[str, Any]) -> Dict[str, Any]:
    """触发Coze工作流"""
    try:
        coze_service = CozeService()
        standardization_service = DataStandardizationService(None)

        # 转换数据格式
        from app.models.coze import StandardizedMessage
        message = StandardizedMessage(**standardized_data)

        # 触发工作流
        result = await coze_service.trigger_workflow(message)

        return {
            "status": "success",
            "workflow_output": result.dict(),
            "user_id": standardized_data.get("user_id")
        }

    except Exception as e:
        if self.request.retries < self.max_retries:
            raise self.retry(countdown=2 ** self.request.retries, exc=e)
        else:
            return {
                "status": "failed",
                "error": str(e),
                "user_id": standardized_data.get("user_id")
            }


@celery_app.task
def process_workflow_result(workflow_result: Dict[str, Any]) -> Dict[str, Any]:
    """处理工作流结果"""
    try:
        action = workflow_result.get("action")

        if action == "reply":
            # 安排发送回复任务
            from .wechat_tasks import send_wechat_reply
            reply_data = workflow_result.get("reply_content", {})
            send_wechat_reply.delay(reply_data)

        elif action == "transfer_human":
            # 处理转人工逻辑
            print(f"需要转人工: {workflow_result}")

        return {
            "status": "processed",
            "action": action
        }

    except Exception as e:
        return {
            "status": "failed",
            "error": str(e)
        }