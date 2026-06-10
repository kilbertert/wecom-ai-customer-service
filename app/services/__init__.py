"""服务层包"""

from typing import Union

from app.core.config import settings

from .wechat import WeChatService
from .coze import CozeService
from .dify import DifyService
from .media import MediaService

# SessionService已移除（单轮对话模式）

__all__ = [
    "WeChatService",
    "CozeService",
    "DifyService",
    "MediaService",
    "get_ai_service",
    "AIService",
]


# 与 CozeService / DifyService 同形的统一类型(供类型注解用)
AIService = Union[CozeService, DifyService]


def get_ai_service() -> AIService:
    """根据 ``settings.app.ai_backend`` 返回对应的 AI 服务实例。

    - ``"coze"`` → :class:`CozeService` (默认,向后兼容)
    - ``"dify"`` → :class:`DifyService`

    两个服务实现同名同形接口:
        - ``upload_file(content: bytes, file_name: str) -> str``
        - ``run_workflow(input_data: dict, user_id: str) -> dict``
    因此 ``WeChatService.process_single_message`` 不需要关心后端。
    """
    backend = (settings.app.ai_backend or "coze").lower()
    if backend == "dify":
        return DifyService()
    if backend == "coze":
        return CozeService()
    raise ValueError(
        f"Unsupported AI backend: {settings.app.ai_backend!r} (expected 'coze' or 'dify')"
    )