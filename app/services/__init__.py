"""服务层包"""

import logging

from app.core.config import settings

from .wechat import WeChatService
from .dify import DifyService
from .media import MediaService

logger = logging.getLogger(__name__)

# SessionService已移除（单轮对话模式）

__all__ = [
    "WeChatService",
    "DifyService",
    "MediaService",
    "get_ai_service",
    "AIService",
]


# AI 后端统一类型 (当前仅 Dify; 保留别名供类型注解)
AIService = DifyService


def get_ai_service() -> AIService:
    """返回 AI 服务实例 (当前固定 Dify)。

    Coze 后端已于 2026-07 移除。``settings.app.ai_backend`` 保留向后兼容,
    但仅 ``"dify"`` 有效; 其他值回退 Dify。

    DifyService 实现:
        - ``upload_file(content: bytes, file_name: str) -> str``
        - ``run_workflow(input_data: dict, user_id: str, conversation_id: str | None, app: str) -> dict``
    """
    backend = (settings.app.ai_backend or "dify").lower()
    if backend != "dify":
        logger.warning("不支持的 AI 后端 %r, 回退 Dify", backend)
    return DifyService()
