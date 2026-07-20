# Bugtrack PostgreSQL

该实例只承载 Bug 反馈草稿、会话绑定、消息、附件元数据、状态事件和飞书同步 outbox。
它与服务器上其他业务 PostgreSQL 隔离，并且只监听 `127.0.0.1:55432`。

部署顺序：

1. `cp env.example .env`，生成独立强密码并 `chmod 600 .env`。
2. `docker compose -f compose.yml up -d`。
3. 在应用 `.env` 配置 `DATABASE_URL=postgresql+asyncpg://...@127.0.0.1:55432/bugtrack`。
4. 在应用目录执行 `venv/bin/alembic upgrade head`。
5. 每日运行 `backup.sh`；恢复演练使用 `restore.sh`，应先停止写入服务。

附件二进制不保存在 PostgreSQL；生产目录由 `BUGTRACK_ATTACHMENT_ROOT` 指定，需要和数据库备份一起纳入主机备份。

