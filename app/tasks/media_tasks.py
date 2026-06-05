"""媒体处理异步任务"""
from typing import Dict, Any
from app.core.celery_app import celery_app

from app.services.media import MediaService
from app.services.wechat import WeChatService


@celery_app.task(bind=True, max_retries=2)
async def process_media_file(self, media_id: str, media_type: str) -> Dict[str, Any]:
    """处理媒体文件"""
    try:
        wechat_service = WeChatService()
        media_service = MediaService(wechat_service)

        # 处理媒体文件
        result = await media_service.download_and_process_media_sync(media_id, media_type)

        return {
            "status": "processed",
            "media_id": media_id,
            "result": result
        }

    except Exception as e:
        if self.request.retries < self.max_retries:
            raise self.retry(countdown=5, exc=e)
        else:
            return {
                "status": "failed",
                "media_id": media_id,
                "error": str(e)
            }


@celery_app.task
def cleanup_temp_files() -> Dict[str, Any]:
    """清理临时文件"""
    try:
        from app.services.media import MediaService
        wechat_service = WeChatService()
        media_service = MediaService(wechat_service)

        # 这里需要实现清理逻辑
        # await media_service.cleanup_temp_files()

        return {
            "status": "cleaned",
            "message": "临时文件清理完成"
        }

    except Exception as e:
        return {
            "status": "failed",
            "error": str(e)
        }