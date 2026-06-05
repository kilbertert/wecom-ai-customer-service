"""FastAPI主应用"""
import logging
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware

from app.core.config import settings
from app.routes import wechat_router, monitoring_router
from app.core.exceptions import (
    WeChatAPIError,
    CozeAPIError,
    SessionError,
    handle_wechat_error,
    handle_coze_error,
    handle_session_error
)
from app.services import WeChatService, CozeService, MediaService

# 配置标准日志
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=getattr(logging, settings.app.log_level.upper()),
)

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """应用生命周期管理"""
    logger.info("Starting WeChat Coze Service (Single-round mode)")

    # 初始化全局服务 (单轮对话模式，无会话管理)
    app.state.wechat_service = WeChatService()
    app.state.coze_service = CozeService()
    app.state.media_service = MediaService(app.state.wechat_service)

    yield

    # 清理资源
    logger.info("Shutting down WeChat Coze Service")

    try:
        await app.state.wechat_service.close()
        await app.state.coze_service.close()
    except Exception as e:
        logger.error("Error during shutdown", error=str(e))


# 创建FastAPI应用
app = FastAPI(
    title=settings.app.app_name,
    version=settings.app.version,
    description="微信客服接入Coze智能体",
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
        "description": "微信客服接入Coze智能体",
        "features": [
            "微信回调处理",
            "消息标准化",
            "Coze工作流集成",
            "会话管理",
            "媒体文件处理",
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