"""路由包"""

from .wechat import router as wechat_router
from .monitoring import router as monitoring_router
from .chatwoot_internal import router as chatwoot_internal_router

__all__ = [
    "wechat_router",
    "monitoring_router",
    "chatwoot_internal_router",
]
