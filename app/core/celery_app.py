"""Celery异步任务配置"""
from celery import Celery
from app.core.config import settings

# 创建Celery应用
celery_app = Celery(
    "weixin_coze_service",
    broker=settings.celery.broker_url,
    backend=settings.celery.result_backend,
    include=["app.tasks"]
)

# Celery配置
celery_app.conf.update(
    # 任务序列化
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,

    # 工作进程配置
    worker_prefetch_multiplier=settings.celery.worker_prefetch_multiplier,
    worker_max_tasks_per_child=settings.celery.worker_max_tasks_per_child,

    # 队列配置
    task_default_queue=settings.celery.task_default_queue,
    task_default_exchange=settings.celery.task_default_exchange,
    task_default_routing_key=settings.celery.task_default_routing_key,

    # 结果过期时间
    result_expires=3600,

    # 任务路由
    task_routes={
        "app.tasks.process_wechat_message": {"queue": "wechat_messages"},
        "app.tasks.send_wechat_reply": {"queue": "wechat_replies"},
        "app.tasks.process_media_file": {"queue": "media_processing"},
    }
)

# 自动发现任务
celery_app.autodiscover_tasks()


if __name__ == "__main__":
    celery_app.start()