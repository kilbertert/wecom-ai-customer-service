"""路由包"""

from .wechat import router as wechat_router
from .monitoring import router as monitoring_router

__all__ = [
    "wechat_router",
    "monitoring_router",
]