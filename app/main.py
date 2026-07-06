"""FastAPI主应用"""
import logging
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware

from app.core.config import settings
from app.routes import (
    wechat_router,
    monitoring_router,
    chatwoot_internal_router,
    bugtrack_internal_router,
)
from app.core.exceptions import (
    WeChatAPIError,
    CozeAPIError,
    SessionError,
    handle_wechat_error,
    handle_coze_error,
    handle_session_error
)
from app.protocols.base import InMemoryDedupStore
from app.protocols.kf_adapter import KfAdapter
from app.protocols.bot_adapter import BotAdapter
from app.services import WeChatService, MediaService, get_ai_service
from app.services.conversation_store import create_conversation_store
from app.services.pending_timer_store import create_pending_timer_store
from app.services.message_processor import MessageProcessor

# 配置标准日志
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=getattr(logging, settings.app.log_level.upper()),
)

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """应用生命周期管理"""
    backend = (settings.app.ai_backend or "coze").lower()
    logger.info("Starting WeChat AI Service (Single-round mode, backend=%s)", backend)

    # 初始化全局服务 (单轮对话模式，无会话管理)
    app.state.wechat_service = WeChatService()
    app.state.ai_service = get_ai_service()  # CozeService or DifyService
    app.state.media_service = MediaService(app.state.wechat_service)

    # 协议适配器 + 编排器 (Phase 3)
    # 共享去重存储 + 薄 conversation_id 映射 (默认 InMemory, 单 worker)
    app.state.dedup_store = InMemoryDedupStore()
    app.state.conversation_store = create_conversation_store()
    # 二阶段: 待办定时器元数据存储 (非会话历史, 类比 ConversationStore)
    app.state.pending_timer_store = create_pending_timer_store()
    app.state.message_processor = MessageProcessor(
        wechat_service=app.state.wechat_service,
        media_service=app.state.media_service,
        ai_service=app.state.ai_service,
        conversation_store=app.state.conversation_store,
        pending_timer_store=app.state.pending_timer_store,
    )
    app.state.kf_adapter = KfAdapter(
        app.state.wechat_service, app.state.dedup_store
    )
    app.state.bot_adapter = BotAdapter(
        app.state.wechat_service, app.state.dedup_store
    )

    yield

    # 清理资源
    logger.info("Shutting down WeChat AI Service (backend=%s)", backend)

    try:
        await app.state.wechat_service.close()
        await app.state.ai_service.close()
    except Exception as e:
        logger.error("Error during shutdown", error=str(e))


# 创建FastAPI应用
app = FastAPI(
    title=settings.app.app_name,
    version=settings.app.version,
    description="微信客服接入AI智能体 (Coze / Dify 可切换)",
    lifespan=lifespan,
    debug=settings.app.debug,
)

# 添加中间件
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.add_middleware(
    TrustedHostMiddleware,
    allowed_hosts=settings.app.allowed_hosts,
)


# 全局异常处理
@app.exception_handler(WeChatAPIError)
async def wechat_api_exception_handler(request: Request, exc: WeChatAPIError):
    return handle_wechat_error(exc)


@app.exception_handler(CozeAPIError)
async def coze_api_exception_handler(request: Request, exc: CozeAPIError):
    return handle_coze_error(exc)


@app.exception_handler(SessionError)
async def session_exception_handler(request: Request, exc: SessionError):
    return handle_session_error(exc)


# 注册路由
app.include_router(wechat_router)
app.include_router(monitoring_router)
app.include_router(chatwoot_internal_router)
app.include_router(bugtrack_internal_router)


@app.get("/")
async def root():
    """根路径"""
    return {
        "service": settings.app.app_name,
        "version": settings.app.version,
        "status": "running",
        "docs": "/docs",
        "health": "/monitoring/health"
    }


@app.get("/info")
async def service_info():
    """服务信息"""
    return {
        "service": settings.app.app_name,
        "version": settings.app.version,
        "description": "微信客服/智能机器人接入 AI 智能体 (Coze / Dify 可切换)",
        "ai_backend": (settings.app.ai_backend or "coze").lower(),
        "features": [
            "微信回调处理 (客服 KF + 智能机器人)",
            "媒体文件处理 (图片/语音)",
            "多模态回复 (markdown)",
            "Coze / Dify 双后端可切换",
            "Chatwoot 双向集成",
            "监控和健康检查"
        ],
        "endpoints": {
            "wechat_callback": "/wechat/kf/callback",
            "health_check": "/monitoring/health",
            "metrics": "/monitoring/metrics",
            "stats": "/monitoring/stats"
        }
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=settings.app.host,
        port=settings.app.port,
        workers=settings.app.workers,
        reload=settings.app.debug,
        log_level=settings.app.log_level.lower(),
    )