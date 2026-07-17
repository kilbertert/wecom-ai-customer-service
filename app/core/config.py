"""配置管理模块"""

import logging
from typing import List, Optional

from pydantic import Field
from pydantic.types import SecretStr
from pydantic_settings import BaseSettings

logger = logging.getLogger(__name__)


class WeChatSettings(BaseSettings):
    """微信相关配置"""

    # 企业微信基础配置
    corp_id: str = Field("PLACEHOLDER_CORP_ID", description="企业微信CorpID")
    corp_secret: SecretStr = Field(
        SecretStr("PLACEHOLDER_CORP_SECRET"), description="企业微信CorpSecret"
    )

    # 微信客服配置
    kf_token: SecretStr = Field(
        SecretStr("PLACEHOLDER_KEFUTOCKEN"), description="微信客服Token"
    )
    encoding_aes_key: SecretStr = Field(
        SecretStr("PLACEHOLDER_ENCODING_AES_KEY"), description="微信客服EncodingAESKey"
    )

    # 回调URL配置
    callback_base_url: str = Field(
        "https://weixinkf.h5.qumall.qushiyun.com", description="回调基础URL"
    )

    # 指定客服配置（可选）
    allowed_open_kfid: Optional[str] = Field(
        None, description="只处理指定客服的消息，为空则处理所有客服"
    )

    class Config:
        env_prefix = "WECHAT_"
        env_file = ".env"  # 指定.env文件
        env_file_encoding = "utf-8"
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
    token_cache_key: str = Field(
        "wechat:access_token", description="Access Token缓存键"
    )

    class Config:
        env_prefix = "REDIS_"
        env_file = ".env"
        env_file_encoding = "utf-8"
        extra = "ignore"


class DatabaseSettings(BaseSettings):
    """数据库配置"""

    url: str = Field("sqlite:///./wecom.db", description="数据库URL")
    pool_size: int = Field(10, description="连接池大小")
    max_overflow: int = Field(20, description="最大溢出连接数")

    class Config:
        env_prefix = "DATABASE_"


class CelerySettings(BaseSettings):
    """Celery异步任务配置"""

    broker_url: str = Field("redis://localhost:6379/1", description="消息代理URL")
    result_backend: str = Field("redis://localhost:6379/2", description="结果后端URL")

    # 任务配置
    task_default_queue: str = Field("wecom", description="默认队列")
    task_default_exchange: str = Field("wecom", description="默认交换机")
    task_default_routing_key: str = Field("wecom", description="默认路由键")

    # 任务执行配置
    worker_prefetch_multiplier: int = Field(1, description="工作进程预取倍数")
    worker_max_tasks_per_child: int = Field(1000, description="子进程最大任务数")

    class Config:
        env_prefix = "CELERY_"
        env_file = ".env"
        env_file_encoding = "utf-8"
        extra = "ignore"


class BugtrackSettings(BaseSettings):
    """二阶段 bug 反馈表配置 (智能表格 webhook key + 查表 corp 凭据)。

    - ``main_webhook_key``: 主表 (bug 反馈数据表) webhook key, N16 新增 / N14 修改
    - ``cache_webhook_key``: 缓存表 (第二张表) webhook key, N19 超时暂存
    - 查表 (N2/N9) 需 access_token, 复用企微 corp_id/corp_secret 换取
      (与 WeChatSettings 同源), 见 SmartSheetQueryService。
    - ``main_doc_id`` / ``main_sheet_id``: 主表文档id/子表id (查询记录 API 需要)
    """

    enabled: bool = Field(False, description="是否启用二阶段 bug 反馈流程")
    main_webhook_key: str = Field("", description="主表 (bug反馈表) webhook key")
    cache_webhook_key: str = Field("", description="缓存表 webhook key")

    # 查表 API (101158 查询记录) 所需定位参数
    main_doc_id: str = Field("", description="主表 doc_id (查询记录用)")
    main_sheet_id: str = Field("", description="主表 sheet_id (查询记录用)")

    # 内部接口鉴权 (来源 IP 白名单, 替代 Bearer token; token 已从 Dify 侧移除)
    internal_token: str = Field("", description="内部接口 Bearer token (已弃用, 保留兼容)")
    allowed_ips: str = Field(
        "127.0.0.1,::1",
        description="允许调用 /internal/bugtrack/* 的来源IP(逗号分隔);生产配 127.0.0.1,::1,<dify_server_ip>",
    )

    # MCP 通道 (智能机器人文档能力, 查表/写表走此通道, 绕开 wedoc REST 48002)
    mcp_apikey: str = Field("", description="企微 MCP robot-doc apikey")
    mcp_url: str = Field(
        "https://qyapi.weixin.qq.com/mcp/robot-doc",
        description="MCP StreamableHttp 端点 (不含 apikey)",
    )

    # 飞书多维表格 (二阶段 bug 表, 替代企微智能表格 — 企微查表是死路)
    feishu_app_id: str = Field("", description="飞书自建应用 App ID (cli_xxx)")
    feishu_app_secret: str = Field("", description="飞书自建应用 App Secret")
    feishu_app_token: str = Field("", description="飞书多维表格 app_token")
    feishu_table_id: str = Field("", description="飞书多维表格 table_id")

    # 超时窗口 (秒)
    timeout_seconds: int = Field(1800, description="待确认超时窗口 (秒, 默认30分钟)")

    class Config:
        env_prefix = "BUGTRACK_"
        env_file = ".env"
        env_file_encoding = "utf-8"
        extra = "ignore"


class ChatwootSettings(BaseSettings):
    """Chatwoot 集成配置 (Phase 1)"""

    base_url: str = Field("http://chatwoot:3000", description="Chatwoot base URL")
    hmac_secret: SecretStr = Field(
        SecretStr("PLACEHOLDER_CHATWOOT_HMAC_SECRET"),
        description="与 Channel::Wecom.wecom_ai_secret 一致的 HMAC 密钥",
    )
    enabled: bool = Field(False, description="是否启用 Chatwoot 同步")

    # 超时
    request_timeout: int = Field(10, description="调用 Chatwoot 的 HTTP 超时 (秒)")

    class Config:
        env_prefix = "CHATWOOT_"
        env_file = ".env"
        env_file_encoding = "utf-8"
        extra = "ignore"


class DifySettings(BaseSettings):
    """Dify相关配置"""

    api_base: str = Field("https://api.dify.ai/v1", description="Dify API base URL")
    api_key: SecretStr = Field(
        SecretStr("PLACEHOLDER_DIFY_API_KEY"), description="Dify API Key (app-xxx)"
    )
    # 双 app 拆分 (KB问答 + bug追踪): api_key_b 非空 → 双 app 路由模式;
    # 为空 → 单 app 兼容 (只走 api_key/api_key_a, 忽略 SWITCH 标记)。
    api_key_a: SecretStr = Field(
        SecretStr(""), description="App A (KB问答) token; 空则回退 api_key"
    )
    api_key_b: SecretStr = Field(
        SecretStr(""), description="App B (bug追踪) token; 空则单 app 模式"
    )

    # Workflow 输入变量名 — 与 Dify 工作流"开始"节点保持一致
    input_text: str = Field("input_text", description="Workflow 文本输入变量名")
    input_image: str = Field("input_img_id", description="Workflow 图片输入变量名")
    input_audio: str = Field("input_audio_id", description="Workflow 语音输入变量名")

    # end-user 标识 (Dify 强制要求)。WeChat 场景下用 external_userid 覆盖
    end_user_default: str = Field(
        "wechat-default-user", description="Dify end-user 默认标识"
    )

    # 工作流配置
    workflow_timeout: int = Field(
        120, description="工作流超时时间(秒) — Dify chatflow 较慢,默认 120s"
    )
    upload_timeout: int = Field(60, description="文件上传超时(秒)")

    # 输出变量名 — workflow 结束节点里设置的变量
    output_text: str = Field("output", description="Workflow 文本输出变量名")

    # App 类型 — 决定走哪个 Dify API 端点
    # "workflow" : /v1/workflows/run (传统工作流, outputs 在 data.outputs 里)
    # "chatflow" : /v1/chat-messages   (advanced-chat / Chatflow, answer 在顶层,
    #                                   知识库在 metadata.retriever_resources)
    app_mode: str = Field(
        "chatflow",
        description='Dify app 类型: "workflow" | "chatflow"',
    )
    # Chatflow 专用超时 (跟 workflow_timeout 同源, 但语义上是两种调用)
    chatflow_timeout: int = Field(120, description="Chatflow 调用超时(秒)")

    # Chatflow user_input_form select 字段 — 决定 chatflow 内 L1 板块路由。
    # 部署级常量 (这个 wecom-ai 实例服务哪种端类型/地域/语言); 默认空=不传, Dify 用
    # 字段 default=""。线上 charge_charging_v16 定义了这三个 select (见 /parameters)。
    # 逐消息可在 input_data 里用 language/hint_endpoint/hint_region 覆盖 (见 _run_chatflow)。
    chatflow_input_language: str = Field(
        "", description='Chatflow select: input_language (zh|en|vi)'
    )
    chatflow_input_hint_endpoint: str = Field(
        "", description='Chatflow select: input_hint_endpoint (user|butler|pc)'
    )
    chatflow_input_hint_region: str = Field(
        "", description='Chatflow select: input_hint_region (cn|overseas)'
    )

    class Config:
        env_prefix = "DIFY_"
        env_file = ".env"
        env_file_encoding = "utf-8"
        extra = "ignore"


class AppSettings(BaseSettings):
    """应用配置"""

    # 基础配置
    app_name: str = Field("WeChat AI Service", description="应用名称")
    version: str = Field("1.1.0", description="应用版本")
    debug: bool = Field(False, description="调试模式")

    # AI 后端 (Coze 已于 2026-07 移除; 仅保留 "dify", 字段留作向后兼容)
    ai_backend: str = Field("dify", description="AI 后端: dify (coze 已移除)")

    # 服务器配置
    host: str = Field("0.0.0.0", description="服务器主机")
    port: int = Field(8000, description="服务器端口")
    workers: int = Field(1, description="工作进程数")

    # 日志配置
    log_level: str = Field("INFO", description="日志级别")
    log_format: str = Field("json", description="日志格式")

    # 安全配置
    secret_key: SecretStr = Field(
        SecretStr("PLACEHOLDER_APP_SECRET_KEY"), description="应用密钥"
    )
    allowed_hosts: List[str] = Field(["*"], description="允许的主机")

    # 性能配置
    max_concurrent_requests: int = Field(100, description="最大并发请求数")
    request_timeout: int = Field(30, description="请求超时时间(秒)")

    # 监控配置
    enable_metrics: bool = Field(True, description="启用指标收集")
    metrics_port: int = Field(9090, description="指标端口")

    # Bot 决策日志 — 智能机器人每次回复后追加一条"决策日志"
    # "off"      : 不输出 (默认, 零侵入)
    # "inline"   : 把 trace 拼到 AI 回复文本末尾, 单次 POST
    # "separate" : 主回复发出后, 再单独 POST 一次 trace 消息
    bot_trace_mode: str = Field(
        "off",
        description='Bot 决策日志模式: "off" | "inline" | "separate"',
    )
    bot_trace_max_len: int = Field(
        1500,
        description="inline 模式下 trace 块最大字符数;超出截断",
    )

    # conversation_id 映射存储 (非 SessionService, 仅存一个 id 字符串让 Dify chatflow 续接)
    # "memory" (默认, 单 worker) | "redis" (多 worker 抗重启)
    conversation_store: str = Field(
        "memory",
        description='conversation_id 映射存储: "memory" | "redis"',
    )
    # 会话状态 TTL(秒): 与 bugtrack 定时器(1800)对齐, 超时未活动 -> conv 过期 -> 下条消息起新会话。
    # 修根因5: conv_id 永不过期致跨话题串话/状态残留(实测 YeBiWei 消息 A↔B 弹跳)。
    # save_state 每条消息调一次, TTL 滑动刷新(活跃会话不过期, 30min 不活动才过期)。
    conversation_ttl: int = Field(
        1800,
        description="会话状态TTL秒(滑动,每条消息刷新);与bugtrack定时器1800对齐",
    )

    # 持久消息队列 + 分布式锁 (Phase: #15+#17B 耦合对)。
    # "memory" (默认): 路由层 BackgroundTasks/asyncio.create_task, 无持久化无锁 (单进程 dev)。
    # "redis"  : 入站消息入 Redis list 持久队列, worker 循环消费, 每 (user,scope) 分布式锁
    #            串行化 process() 防同用户并发竞态 (read->Dify->save)。进程重启不丢消息
    #            (proc 列表 orphan sweep 重入队, 至少一次投递)。生产多进程部署用此模式。
    message_queue: str = Field(
        "memory",
        description='消息队列+锁: "memory"(默认) | "redis"(持久队列+分布式锁)',
    )
    # 去重存储 (与 message_queue 解耦, 但 redis 队列模式下建议 redis 去重, 否则进程崩溃
    # 重投递会因 InMemory 状态丢失导致 Dify 重复轮次)。
    dedup_store: str = Field(
        "memory",
        description='去重存储: "memory"(默认) | "redis"(多进程/崩溃安全幂等)',
    )
    queue_workers: int = Field(
        2,
        description="Redis 队列 worker 协程数 (锁按 user 隔离, 多 worker 服务不同用户)",
    )
    queue_lock_ttl: int = Field(
        600,
        description="分布式锁 TTL 秒; 须 > 最坏 4 轮 Dify (MAX_ROUTES=3, ×chatflow_timeout120=480); "
                    "太短->锁过期中段被他人抢占致状态竞态; 太长->崩溃后该用户消息恢复延迟",
    )
    queue_max_attempts: int = Field(
        3,
        description="process 真异常最大重试次数; 超出入死信 (CancelledError 不计数)",
    )

    class Config:
        env_prefix = "APP_"
        env_file = ".env"  # 必需: 否则 APP_* 字段不会从 .env 读取
        env_file_encoding = "utf-8"
        extra = "ignore"


class ASRSettings(BaseSettings):
    """语音识别 (ASR) 配置 - wecom 侧 paraformer 转写语音为文本入 query。

    Dify chatflow 无 ASR 节点且 speech_to_text feature 只管前端 UI,
    语音必须在 wecom 侧转文本后作为 query 发送 (见 multimodal-vision-findings 记忆)。
    """

    enabled: bool = Field(True, description="是否启用 wecom 侧 ASR (语音转文本)")
    dashscope_api_key: SecretStr = Field(
        SecretStr(""), description="通义 DashScope API key (paraformer ASR)"
    )
    model: str = Field(
        "paraformer-realtime-v2",
        description="ASR 模型 (paraformer-realtime-v2 支持本地 wav 流式识别)",
    )
    sample_rate: int = Field(16000, description="音频采样率 (wecom AMR->WAV 转 16kHz)")
    timeout: int = Field(30, description="ASR 单次转写超时秒")

    class Config:
        env_prefix = "ASR_"
        env_file = ".env"
        env_file_encoding = "utf-8"
        extra = "ignore"


class Settings(BaseSettings):
    """全局配置"""

    # 子配置
    wechat: WeChatSettings = WeChatSettings()
    dify: DifySettings = DifySettings()
    chatwoot: ChatwootSettings = ChatwootSettings()
    bugtrack: BugtrackSettings = BugtrackSettings()
    redis: RedisSettings = RedisSettings()
    database: DatabaseSettings = DatabaseSettings()
    celery: CelerySettings = CelerySettings()
    app: AppSettings = AppSettings()
    asr: ASRSettings = ASRSettings()

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
        logger.info("配置从.env文件加载成功")

        # 检查是否使用了占位符值
        if (
            settings.wechat.corp_id.startswith("PLACEHOLDER")
            or str(settings.wechat.corp_secret).startswith("PLACEHOLDER")
        ):
            logger.warning("检测到占位符配置值，请确保已正确配置生产环境变量")

        # 配置对齐约束 (Path C 防护): APP_CONVERSATION_TTL 必须与
        # BUGTRACK_TIMEOUT_SECONDS 一等。conv TTL > bug timeout -> 超时后 conv 仍在,
        # 用户晚归命中 stale conv_b (await_confirm_* 残留); conv TTL < bug timeout ->
        # 超时前 conv 提前过期。两者都破坏"超时后新会话 cv=IDLE"不变量。不等则告警
        # (若需更强保证, 可在此处 settings.app.conversation_ttl = min(...) 强制取小)。
        conv_ttl = getattr(settings.app, "conversation_ttl", 1800)
        bug_timeout = getattr(settings.bugtrack, "timeout_seconds", 1800)
        if conv_ttl != bug_timeout:
            logger.warning(
                "[CONFIG] APP_CONVERSATION_TTL(%s) != BUGTRACK_TIMEOUT_SECONDS(%s) "
                "-> 超时路径 cv_flow_state 一致性可能破裂 (Path C 复现风险), 请对齐",
                conv_ttl, bug_timeout,
            )

        # 队列/去重对齐约束 (#15+#17B): redis 持久队列下, 进程崩溃重投递靠 DedupStore
        # 幂等去重。若 dedup 仍为 memory, 崩溃后 InMemory 状态丢失 -> 重投递的 msgid
        # 重新 acquire 成功 -> Dify chatflow 重复一轮 (污染上下文)。仅告警, 不强制。
        mq = (getattr(settings.app, "message_queue", "memory") or "memory").lower()
        dd = (getattr(settings.app, "dedup_store", "memory") or "memory").lower()
        if mq == "redis" and dd != "redis":
            logger.warning(
                "[CONFIG] APP_MESSAGE_QUEUE=redis 但 APP_DEDUP_STORE=%s -> 崩溃重投递"
                " 可能产生 Dify 重复轮次 (InMemory 去重不跨进程/不抗重启), 建议设 "
                "APP_DEDUP_STORE=redis",
                dd,
            )

        return
    except Exception as e:
        logger.warning("从.env文件加载配置失败: %s", e)

    # 2. 尝试从env.example加载
    logger.info("尝试从env.example文件加载配置...")
    try:
        # 临时修改配置以从env.example加载
        original_env_file = Settings.Config.env_file
        Settings.Config.env_file = "env.example"

        settings = Settings()
        logger.info("配置从env.example文件加载成功")
        logger.warning("您正在使用示例配置!")
        logger.warning("生产环境请创建.env文件并填入真实配置值")

        # 恢复原始配置
        Settings.Config.env_file = original_env_file
        return
    except Exception as e2:
        logger.error("从env.example加载配置也失败: %s", e2)
        Settings.Config.env_file = original_env_file

    # 3. 使用默认配置（含占位符）
    logger.info("使用默认配置（含占位符值）...")
    try:
        settings = Settings()
    except Exception as e3:
        logger.error("即使使用默认配置也失败: %s", e3)
    raise SystemExit(1)


# 加载配置
load_settings()
