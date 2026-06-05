"""配置管理模块"""
from typing import Optional, List
from pydantic import Field
from pydantic_settings import BaseSettings
from pydantic.types import SecretStr


class WeChatSettings(BaseSettings):
    """微信相关配置"""

    # 企业微信基础配置
    corp_id: str = Field("PLACEHOLDER_CORP_ID", description="企业微信CorpID")
    corp_secret: SecretStr = Field(SecretStr("PLACEHOLDER_CORP_SECRET"), description="企业微信CorpSecret")

    # 微信客服配置
    kf_token: SecretStr = Field(SecretStr("PLACEHOLDER_KEFUTOCKEN"), description="微信客服Token")
    encoding_aes_key: SecretStr = Field(SecretStr("PLACEHOLDER_ENCODING_AES_KEY"), description="微信客服EncodingAESKey")

    # 回调URL配置
    callback_base_url: str = Field("https://weixinkf.h5.qumall.qushiyun.com", description="回调基础URL")

    # 指定客服配置（可选）
    allowed_open_kfid: Optional[str] = Field(None, description="只处理指定客服的消息，为空则处理所有客服")

    class Config:
        env_prefix = "WECHAT_"
        env_file = ".env"       # 指定.env文件
        env_file_encoding = 'utf-8'
        extra = "ignore"


class CozeSettings(BaseSettings):
    """Coze相关配置"""

    api_token: SecretStr = Field(SecretStr("PLACEHOLDER_COZE_API_TOKEN"), description="Coze API Token")
    bot_id: str = Field("7599886499640147968", description="Coze Bot ID")

    # 工作流配置
    workflow_timeout: int = Field(30, description="工作流超时时间(秒)")
    max_retries: int = Field(3, description="工作流重试次数")

    class Config:
        env_prefix = "COZE_"
        env_file = ".env"       # 指定.env文件
        env_file_encoding = 'utf-8'
        extra = "ignore"

class RedisSettings(BaseSettings):
    """Redis配置"""

    host: str = Field("localhost", description="Redis主机")
    port: int = Field(6379, description="Redis端口")
    db: int = Field(0, description="Redis数据库")
    password: Optional[SecretStr] = Field(None, description="Redis密码")

    # 会话配置
    session_ttl: int = Field(3600, description="会话过期时间(秒)")
    session_prefix: str = Field("session:", description="会话键前缀")

    # 缓存配置
    cache_ttl: int = Field(7200, description="缓存过期时间(秒)")
    token_cache_key: str = Field("wechat:access_token", description="Access Token缓存键")

    class Config:
        env_prefix = "REDIS_"


class DatabaseSettings(BaseSettings):
    """数据库配置"""

    url: str = Field("sqlite:///./weixin_coze.db", description="数据库URL")
    pool_size: int = Field(10, description="连接池大小")
    max_overflow: int = Field(20, description="最大溢出连接数")

    class Config:
        env_prefix = "DATABASE_"


class CelerySettings(BaseSettings):
    """Celery异步任务配置"""

    broker_url: str = Field("redis://localhost:6379/1", description="消息代理URL")
    result_backend: str = Field("redis://localhost:6379/2", description="结果后端URL")

    # 任务配置
    task_default_queue: str = Field("weixin_coze", description="默认队列")
    task_default_exchange: str = Field("weixin_coze", description="默认交换机")
    task_default_routing_key: str = Field("weixin_coze", description="默认路由键")

    # 任务执行配置
    worker_prefetch_multiplier: int = Field(1, description="工作进程预取倍数")
    worker_max_tasks_per_child: int = Field(1000, description="子进程最大任务数")

    class Config:
        env_prefix = "CELERY_"


class AppSettings(BaseSettings):
    """应用配置"""

    # 基础配置
    app_name: str = Field("WeChat Coze Service", description="应用名称")
    version: str = Field("1.0.0", description="应用版本")
    debug: bool = Field(False, description="调试模式")

    # 服务器配置
    host: str = Field("0.0.0.0", description="服务器主机")
    port: int = Field(8000, description="服务器端口")
    workers: int = Field(1, description="工作进程数")

    # 日志配置
    log_level: str = Field("INFO", description="日志级别")
    log_format: str = Field("json", description="日志格式")

    # 安全配置
    secret_key: SecretStr = Field(SecretStr("PLACEHOLDER_APP_SECRET_KEY"), description="应用密钥")
    allowed_hosts: List[str] = Field(["*"], description="允许的主机")

    # 性能配置
    max_concurrent_requests: int = Field(100, description="最大并发请求数")
    request_timeout: int = Field(30, description="请求超时时间(秒)")

    # 监控配置
    enable_metrics: bool = Field(True, description="启用指标收集")
    metrics_port: int = Field(9090, description="指标端口")

    class Config:
        env_prefix = "APP_"


class Settings(BaseSettings):
    """全局配置"""

    # 子配置
    wechat: WeChatSettings = WeChatSettings()
    coze: CozeSettings = CozeSettings()
    redis: RedisSettings = RedisSettings()
    database: DatabaseSettings = DatabaseSettings()
    celery: CelerySettings = CelerySettings()
    app: AppSettings = AppSettings()

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        case_sensitive = False
        extra = "ignore"


# 创建全局配置实例
def load_settings():
    """加载配置，支持多级fallback"""
    global settings

    # 1. 首先尝试从.env文件加载
    try:
        settings = Settings()
        print("[INFO] 配置从.env文件加载成功")

        # 检查是否使用了占位符值
        if (settings.wechat.corp_id.startswith("PLACEHOLDER") or
            str(settings.wechat.corp_secret).startswith("PLACEHOLDER") or
            str(settings.coze.api_token).startswith("PLACEHOLDER")):
            print("[WARNING] 检测到占位符配置值，请确保已正确配置生产环境变量")

        return
    except Exception as e:
        print(f"[WARNING] 从.env文件加载配置失败: {e}")

    # 2. 尝试从env.example加载
    print("[INFO] 尝试从env.example文件加载配置...")
    try:
        # 临时修改配置以从env.example加载
        original_env_file = Settings.Config.env_file
        Settings.Config.env_file = "env.example"

        settings = Settings()
        print("[INFO] 配置从env.example文件加载成功")
        print("[WARNING] ⚠️  您正在使用示例配置!")
        print("[WARNING] 生产环境请创建.env文件并填入真实配置值")

        # 恢复原始配置
        Settings.Config.env_file = original_env_file
        return
    except Exception as e2:
        print(f"[ERROR] 从env.example加载配置也失败: {e2}")
        Settings.Config.env_file = original_env_file

    # 3. 使用默认配置（含占位符）
    print("[INFO] 使用默认配置（含占位符值）...")
    try:
        settings = Settings()
    except Exception as e3:
        print(f"[ERROR] 即使使用默认配置也失败: {e3}")
    raise SystemExit(1)

# 加载配置
load_settings()
