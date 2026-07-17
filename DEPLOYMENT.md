# 部署指南

WeCom AI 客服编排层 (Dify 后端) 的部署方式。配置见 [env.example](env.example), 架构见 [CLAUDE.md](CLAUDE.md)。

## 部署前检查

- Python 3.11+ (本地) 或 Docker
- Redis 7+ (启用 redis 模式时: 持久队列 / 去重 / 会话 / Celery broker)
- `ffmpeg`/`ffprobe` (语音转码; Docker 镜像已装)
- `.env` 已填真实凭据且 `chmod 600 .env`

## 方式一: Docker Compose (完整栈, 推荐)

```bash
cp env.example .env && vim .env   # 填凭据; chmod 600 .env
./deploy.sh                       # build + up -d
# 等价: docker-compose up -d
```

起三个服务 (见 [docker-compose.yml](docker-compose.yml)):

| 服务 | 作用 |
|---|---|
| `redis` | 持久队列 / 锁 / 去重 / 会话 / Celery broker (db0 队列/锁/去重/会话/定时器, db1 broker, db2 result) |
| `wecom-ai-service` | FastAPI 主服务 (主机 8501 -> 容器 8000, 默认启用 redis 队列模式) |
| `celery-worker` | 二阶段 bug 超时 (`-Q wecom_timers --concurrency=1`) |

```bash
./deploy.sh logs|status|restart|stop|clean|verify
docker-compose logs -f wecom-ai-service
```

> 仅 memory 模式 (无 redis): 注释掉 `wecom-ai-service.environment` 里三个 `APP_*_STORE`/`APP_MESSAGE_QUEUE`, 并移除 redis/celery 依赖。

## 方式二: 生产 (systemd + scp 热更新)

生产用 systemd 保活 uvicorn + celery worker, 经 `scripts/deploy_local_changes.sh` 增量推送:

```bash
# 推送 git status 检测到的改动到生产 + 重启 + 健康检查
SSH_PASSWORD=xxx ./scripts/deploy_local_changes.sh
# 或指定文件 / 自某 commit
SSH_PASSWORD=xxx ./scripts/deploy_local_changes.sh app/services/x.py
SSH_PASSWORD=xxx ./scripts/deploy_local_changes.sh --from-commit HEAD~3

# 部署后灰度观察 (队列深度 / 死信 / 健康)
SSH_PASSWORD=xxx ./scripts/deploy_local_changes.sh --observe 300   # 观察 300 秒
```

环境变量 (均有默认值, 见脚本头): `REMOTE_HOST` / `REMOTE_PORT` / `REMOTE_USER` / `REMOTE_DIR` / `REMOTE_HEALTH_URL` / `REMOTE_RESTART` / `SSH_PASSWORD`。

生产 systemd 单元 (示例):

```ini
# /etc/systemd/system/wecom-ai.service
[Unit]
Description=WeCom AI Customer Service
After=network.target redis-server.service

[Service]
WorkingDirectory=/opt/wecom-ai-customer-service
EnvironmentFile=/opt/wecom-ai-customer-service/.env
ExecStart=/opt/wecom-ai-customer-service/venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8501
Restart=always

[Install]
WantedBy=multi-user.target
```

```ini
# /etc/systemd/system/wecom-celery-worker.service
[Unit]
Description=WeCom Celery Worker (bugtrack timeout)
After=network.target redis-server.service

[Service]
WorkingDirectory=/opt/wecom-ai-customer-service
EnvironmentFile=/opt/wecom-ai-customer-service/.env
ExecStart=/opt/wecom-ai-customer-service/venv/bin/celery -A app.core.celery_app worker -Q wecom_timers --concurrency=1 --loglevel=info
Restart=always

[Install]
WantedBy=multi-user.target
```

## 健康检查

```bash
curl http://localhost:8501/monitoring/health        # 存活 (liveness, 恒 200)
curl http://localhost:8501/monitoring/health/ready   # 就绪 (readiness, 配置占位/Dify 不可达 -> 503)
curl http://localhost:8501/monitoring/health/detailed # 详细配置状态
curl http://localhost:8501/monitoring/metrics
```

## 灰度观察

`scripts/queue_observe.py` 轮询 redis 队列深度 + 健康端点, 死信 > 0 / 处理中堆积增长 / 未就绪时告警并非零退出 (可作 canary gate):

```bash
python3 scripts/queue_observe.py                  # 持续观察 (Ctrl-C 停)
python3 scripts/queue_observe.py --duration 300   # 观察 5 分钟后退出 (非零=告警)
```

## 安全清单 (部署前)

- [ ] `.env` 权限 600, 未提交 git
- [ ] 无硬编码密钥 (凭据全走 env)
- [ ] `WECHAT_ENCODING_AES_KEY` 恰好 43 字符
- [ ] `APP_CONVERSATION_TTL` == `BUGTRACK_TIMEOUT_SECONDS` (load_settings 不等会告警)
- [ ] 启用 redis 队列时同步启用 redis 去重 (`APP_DEDUP_STORE=redis`, 否则告警)
- [ ] 凭据若曾泄露: 轮换 + `git filter-repo` 清史 + 重建镜像
