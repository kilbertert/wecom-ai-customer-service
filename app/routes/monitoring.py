"""监控和健康检查路由"""
from typing import Dict, Any
from fastapi import APIRouter
import time
import psutil

from app.core.config import settings

router = APIRouter(prefix="/monitoring", tags=["monitoring"])


@router.get("/health")
async def health_check() -> Dict[str, Any]:
    """健康检查"""
    return {
        "status": "healthy",
        "timestamp": int(time.time()),
        "service": settings.app.app_name,
        "version": settings.app.version
    }


@router.get("/health/detailed")
async def detailed_health_check() -> Dict[str, Any]:
    """详细健康检查"""
    health_status = {
        "status": "healthy",
        "timestamp": int(time.time()),
        "checks": {}
    }

    # 检查系统资源
    health_status["checks"]["system"] = {
        "status": "healthy",
        "cpu_percent": psutil.cpu_percent(interval=1),
        "memory_percent": psutil.virtual_memory().percent,
        "disk_percent": psutil.disk_usage('/').percent
    }

    # 检查配置（单轮对话模式，无Redis依赖）
    health_status["checks"]["configuration"] = {
        "status": "healthy",
        "wechat_configured": bool(settings.wechat.corp_id),
        "coze_configured": bool(settings.coze.bot_id),
        "mode": "single_round_conversation"
    }

    return health_status


@router.get("/metrics")
async def get_metrics() -> Dict[str, Any]:
    """获取指标数据"""
    try:
        metrics = {
            "mode": "single_round_conversation",
            "system": {
                "cpu_percent": psutil.cpu_percent(),
                "memory_percent": psutil.virtual_memory().percent,
                "disk_percent": psutil.disk_usage('/').percent
            },
            "timestamp": int(time.time())
        }

        return metrics

    except Exception as e:
        return {
            "error": f"获取指标失败: {str(e)}",
            "timestamp": int(time.time())
        }


@router.get("/stats")
async def get_stats() -> Dict[str, Any]:
    """获取统计信息"""
    try:
        return {
            "mode": "single_round_conversation",
            "description": "单轮对话模式，无会话统计",
            "system_info": {
                "cpu_count": psutil.cpu_count(),
                "memory_total": psutil.virtual_memory().total,
                "disk_total": psutil.disk_usage('/').total
            },
            "timestamp": int(time.time())
        }

    except Exception as e:
        return {
            "error": f"获取统计失败: {str(e)}",
            "timestamp": int(time.time())
        }