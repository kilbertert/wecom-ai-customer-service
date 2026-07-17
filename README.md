# WeCom AI 客服编排层

FastAPI 中间件, 桥接 **企业微信客服 (KF) + 企业微信智能机器人** 回调与 **Dify** AI 后端:
接收微信回调 -> 验签/解密 (协议适配器) -> 媒体归一化 -> 调 Dify chatflow -> 回复投递 (KF `send_kf` / bot `response_url`)。

> Coze 后端已于 2026-07 移除, 当前仅 Dify。人工侧由 Chatwoot 集成。

## 架构

```
WeChat KF / 智能机器人 ──POST /wechat/{kf|bot}/callback──▶ FastAPI (薄路由)
                                      │
                          ProtocolAdapter.receive()   ← KfAdapter / BotAdapter
                            (验签 + AES 解密 + 协议解析) -> List[InboundMessage]
                                      │
                      ┌───────────────┴───────────────┐
                      ▼ (memory 模式)                 ▼ (redis 持久队列模式, #15)
              BackgroundTasks / create_task      RedisMessageQueue (LPUSH/BLMOVE)
                      │                               + 每(user,scope)分布式锁 (#17B)
                      └───────────────┬───────────────┘
                                      ▼
                          MessageProcessor.process  (协议无关编排)
                            dedup -> 媒体 -> Chatwoot handoff -> ConversationStore(双app路由)
                            -> Dify chatflow -> compose -> adapter.send -> Chatwoot notify
```

核心抽象是 `ProtocolAdapter` (KF / Bot 各自独立凭证与适配器), 共享 `DedupStore` 与
`MessageProcessor`。新增协议只需实现 `ProtocolAdapter`, 无需改编排器。**双 app 路由**
(A=KB问答 / B=bug追踪) 经 Dify 回复末尾 SWITCH 标记驱动改投; 二阶段 bug 反馈经 TIMER
标记 + Celery 30 分钟超时。

**完整架构 / 代码约定 / 配置 / 常见陷阱见 [CLAUDE.md](CLAUDE.md)。**

## 快速开始

### Docker (完整栈: wecom + redis + celery)

```bash
cp env.example .env && vim .env   # 填真实凭据; chmod 600 .env
./deploy.sh                       # build + up -d (redis + wecom + celery)
# 或: docker-compose up -d
curl http://localhost:8501/monitoring/health
```

### 本地开发

```bash
pip install -r requirements.txt
python run.py                     # -> http://localhost:8501 (APP_PORT)
pytest                            # 单元 + 集成测试
```

> 语音转码需宿主机装 `ffmpeg`/`ffprobe` (Docker 镜像已装; 本地需自行装)。

## 配置

全部 env 驱动 (`pydantic-settings`), 详见 [env.example](env.example)。必需:

- `WECHAT_CORP_ID` / `WECHAT_CORP_SECRET` / `WECHAT_KF_TOKEN` / `WECHAT_ENCODING_AES_KEY` (43 字符) / `WECHAT_CALLBACK_BASE_URL`
- `DIFY_API_KEY` (双 app 模式另配 `DIFY_API_KEY_A` / `DIFY_API_KEY_B`)

多进程 / 抗重启 / 持久队列 (可选, 同一 Redis 实例不同 key 前缀):

- `APP_CONVERSATION_STORE=redis` / `APP_DEDUP_STORE=redis` / `APP_MESSAGE_QUEUE=redis`
- `APP_QUEUE_WORKERS=2` / `APP_QUEUE_LOCK_TTL=600` / `APP_QUEUE_MAX_ATTEMPTS=3`

## 主要功能

- 双协议接入: 微信客服 (XML + sync_msg 拉取) + 智能机器人 (JSON envelope + response_url 推送)
- 多模态: 图片 (AES 解密 + PIL 转码) / 语音 (AMR->WAV + DashScope ASR -> 文本入 query)
- Dify chatflow 多轮: 双 app 路由状态机 {active, conv_a, conv_b}, SWITCH 标记驱动改投
- 二阶段 bug 反馈: TIMER 标记 + Celery 30 分钟超时 + 飞书多维表格
- Chatwoot 人工接管 (handoff 跳过 AI) + 双向消息同步
- 持久队列 + 分布式锁 (可选 redis 模式): 进程重启不丢消息, 同用户消息串行化防竞态
- 监控: `/monitoring/health` (存活) / `/health/ready` (就绪) / `/metrics` / `/stats`

## 部署 / 灰度

见 [DEPLOYMENT.md](DEPLOYMENT.md) 与 `scripts/`:

- `scripts/deploy_local_changes.sh` - scp 推送改动到生产 + 重启 + 健康检查
- `scripts/queue_observe.py` - 灰度观察 redis 队列深度 / 死信 / 健康 (可作 canary gate)
