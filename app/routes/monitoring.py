"""监控和健康检查路由。

三类端点 (审查 P1 修复: 旧版 /health 无条件 healthy, PLACEHOLDER 也算已配置):

- ``/monitoring/health`` (liveness): 进程活着即 200。Docker/systemd 健康检查目标
  -> 不因 Dify/Redis 抖动而重启 (避免重启风暴)。
- ``/monitoring/health/ready`` (readiness): 关键配置非占位符 + Dify 可达才 200,
  否则 503。供负载均衡/部署门禁用 -> 配置缺失或 Dify 宕机能被发现。
- ``/monitoring/health/detailed``: 真实配置状态 (PLACEHOLDER 视为未配置) + 系统
  资源 + 依赖状态 (信息性)。

注: Redis/Celery 的主动探测较重且易抖动, 此处仅报配置状态; Dify 探测用 2s 超时
短连接, 不阻塞。
"""
from typing import Dict, Any

import httpx
from fastapi import APIRouter
from fastapi.responses import JSONResponse
import psutil
import time

from app.core.config import settings

router = APIRouter(prefix="/monitoring", tags=["monitoring"])


def _configured(value: Any) -> bool:
    """值是否为"真正已配置": 非空且非 PLACEHOLDER 占位符。"""
    if value is None:
        return False
    s = str(value)
    return bool(s) and not s.startswith("PLACEHOLDER")


def _critical_config_ok() -> Dict[str, bool]:
    """关键配置项是否真配置 (非占位符)。"""
    wechat_ok = (
        _configured(settings.wechat.corp_id)
        and _configured(settings.wechat.corp_secret.get_secret_value())
        and _configured(settings.wechat.kf_token.get_secret_value())
        and _configured(settings.wechat.encoding_aes_key.get_secret_value())
    )
    dify_key = (
        settings.dify.api_key_a.get_secret_value()
        or settings.dify.api_key.get_secret_value()
    )
    return {"wechat": wechat_ok, "dify": _configured(dify_key)}


async def _probe_dify(timeout: float = 2.0) -> Dict[str, Any]:
    """轻量探测 Dify 可达性 (短超时, 不阻塞主流程)。"""
    base = getattr(settings.dify, "api_base", "") or ""
    if not base:
        return {"reachable": False, "reason": "api_base 未配置"}
    try:
        async with httpx.AsyncClient(timeout=timeout) as ac:
            # 探 api_base 根 (任何 HTTP 响应即说明 Dify 进程在)
            r = await ac.get(base.rstrip("/").rsplit("/", 1)[0] or base)
        return {"reachable": True, "status_code": r.status_code}
    except Exception as e:
        return {"reachable": False, "reason": f"{type(e).__name__}: {str(e)[:80]}"}


@router.get("/health")
async def health_check() -> Dict[str, Any]:
    """liveness: 进程活着即 200 (Docker 健康检查目标)。"""
    return {
        "status": "healthy",
        "timestamp": int(time.time()),
        "service": settings.app.app_name,
        "version": settings.app.version,
    }


@router.get("/health/ready")
async def readiness_check() -> JSONResponse:
    """readiness: 关键配置非占位符 + Dify 可达才 200, 否则 503。

    供负载均衡/部署门禁; 不同于 liveness, 这里真实反映"能否服务"。
    """
    cfg = _critical_config_ok()
    dify = await _probe_dify()
    ready = cfg["wechat"] and cfg["dify"] and dify["reachable"]
    body = {
        "status": "ready" if ready else "not_ready",
        "timestamp": int(time.time()),
        "config": cfg,
        "dify": dify,
        "mode": "single_round_conversation",
    }
    return JSONResponse(content=body, status_code=200 if ready else 503)


@router.get("/health/detailed")
async def detailed_health_check() -> Dict[str, Any]:
    """详细健康检查: 真实配置状态 + 系统资源 + 依赖状态 (信息性)。"""
    cfg = _critical_config_ok()
    dify = await _probe_dify()
    overall = "healthy" if (cfg["wechat"] and cfg["dify"] and dify["reachable"]) else "degraded"

    return {
        "status": overall,
        "timestamp": int(time.time()),
        "checks": {
            "system": {
                "status": "healthy",
                "cpu_percent": psutil.cpu_percent(interval=1),
                "memory_percent": psutil.virtual_memory().percent,
                "disk_percent": psutil.disk_usage('/').percent,
            },
            "configuration": {
                "status": "healthy" if all(cfg.values()) else "misconfigured",
                "wechat_configured": cfg["wechat"],
                "dify_configured": cfg["dify"],
                # PLACEHOLDER 不再算已配置 (旧 bug)
            },
            "dependencies": {
                "dify": dify,
                # Redis/Celery 仅报配置态 (主动探测易抖动, 留作后续)
                "redis_mode": getattr(settings.app, "conversation_store", "memory"),
                "celery_broker_set": _configured(getattr(settings.celery, "broker_url", "")),
                "feishu_configured": bool(
                    getattr(settings.bugtrack, "feishu_app_id", "")
                    and getattr(settings.bugtrack, "feishu_app_secret", "")
                ),
                "bugtrack_enabled": getattr(settings.bugtrack, "enabled", False),
            },
            "mode": "single_round_conversation",
        },
    }


@router.get("/metrics")
async def get_metrics() -> Dict[str, Any]:
    """获取指标数据"""
    try:
        return {
            "mode": "single_round_conversation",
            "system": {
                "cpu_percent": psutil.cpu_percent(),
                "memory_percent": psutil.virtual_memory().percent,
                "disk_percent": psutil.disk_usage('/').percent
            },
            "timestamp": int(time.time())
        }
    except Exception as e:
        return {"error": f"获取指标失败: {str(e)}", "timestamp": int(time.time())}


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
        return {"error": f"获取统计失败: {str(e)}", "timestamp": int(time.time())}
