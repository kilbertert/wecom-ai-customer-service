#!/usr/bin/env python3
"""灰度观察脚本: 轮询 redis 消息队列深度 + 健康端点, 异常告警。

部署 (#15 持久队列) 后用于灰度观察:
  - wecom:msgq      主队列深度 (积压)
  - wecom:msgq:proc 处理中深度 (持续 > 0 且增长 = 处理跟不上 / 锁卡死)
  - wecom:msgq:dead 死信 (> 0 = 有消息反复失败, 需排查)
  - /monitoring/health       存活
  - /monitoring/health/ready 就绪 (配置占位 / Dify 不可达 -> 503)

任一告警 (dead>0 / proc 持续堆积 / 非 200) -> 退出码非零, 可作 canary gate:
  python3 scripts/queue_observe.py --duration 300   # 观察 5 分钟, 有告警则 exit 1

默认读 app settings (prod .env) 取 redis 与端口; 也可 --redis-url / --health-url 覆盖。
仅 memory 队列模式 (APP_MESSAGE_QUEUE=memory) 无 redis 队列, 本脚本退化为只看健康。
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime

# 让脚本可从任意 cwd 运行 (把仓库根加入 sys.path, 以 import app.*)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 导入队列 key (单一真相源) + settings
from app.core.config import settings  # noqa: E402
from app.services.message_queue import RedisMessageQueue  # noqa: E402


def _ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def main() -> int:
    ap = argparse.ArgumentParser(description="灰度观察 redis 队列 + 健康")
    ap.add_argument("--duration", type=int, default=0,
                    help="观察总秒数 (0=持续到 Ctrl-C, 默认 0)")
    ap.add_argument("--interval", type=int, default=5,
                    help="轮询间隔秒 (默认 5)")
    ap.add_argument("--redis-url", default="",
                    help="redis URL (默认从 settings.redis 拼)")
    ap.add_argument("--health-url", default="",
                    help="健康基址 (默认 http://localhost:{APP_PORT})")
    ap.add_argument("--proc-stuck-polls", type=int, default=6,
                    help="proc 连续非零多少轮判为堆积卡死 (默认 6 ≈ 30s)")
    args = ap.parse_args()

    # ---- redis 客户端 ----
    r = None
    queue_enabled = (getattr(settings.app, "message_queue", "memory") or "memory").lower() == "redis"
    if queue_enabled:
        try:
            import redis as _redis

            if args.redis_url:
                r = _redis.Redis.from_url(args.redis_url, socket_timeout=3, socket_connect_timeout=3)
            else:
                pw = (settings.redis.password.get_secret_value()
                      if settings.redis.password else None)
                r = _redis.Redis(
                    host=settings.redis.host, port=settings.redis.port,
                    db=settings.redis.db, password=pw,
                    socket_timeout=3, socket_connect_timeout=3,
                )
            r.ping()
        except Exception as e:
            print(f"[{_ts()}] [WARN] redis 不可达, 仅观察健康: {e}")
            r = None
    else:
        print(f"[{_ts()}] [INFO] APP_MESSAGE_QUEUE != redis, 仅观察健康 (无队列深度)")

    # ---- httpx 客户端 ----
    import httpx

    base = args.health_url or f"http://localhost:{settings.app.port}"
    client = httpx.Client(timeout=3.0)

    def http_code(path: str) -> int:
        try:
            return client.get(f"{base}{path}").status_code
        except Exception:
            return 0

    proc_nonzero = 0          # proc 连续非零轮数
    ever_alert = False
    start = time.time()
    QM, QP, QD = RedisMessageQueue.Q_MAIN, RedisMessageQueue.Q_PROC, RedisMessageQueue.Q_DEAD

    try:
        while True:
            now = time.time()
            if args.duration and (now - start) >= args.duration:
                break

            main_len = proc_len = dead_len = -1
            if r is not None:
                try:
                    main_len = r.llen(QM)
                    proc_len = r.llen(QP)
                    dead_len = r.llen(QD)
                except Exception as e:
                    print(f"[{_ts()}] [WARN] redis 查询失败: {e}")

            health = http_code("/monitoring/health")
            ready = http_code("/monitoring/health/ready")

            alerts = []
            if dead_len > 0:
                alerts.append(f"DEAD={dead_len}")
            if proc_len > 0:
                proc_nonzero += 1
                if proc_nonzero >= args.proc_stuck_polls:
                    alerts.append(f"PROC 卡死({proc_len}@{proc_nonzero}轮)")
            else:
                proc_nonzero = 0
            if health != 200:
                alerts.append(f"health={health}")
            if ready != 200:
                alerts.append(f"ready={ready}")

            qpart = (f"main={main_len} proc={proc_len} dead={dead_len}"
                     if r is not None else "queue=n/a")
            hpart = f"health={health} ready={ready}"
            tag = "⚠ " + " ".join(alerts) if alerts else "OK"
            if alerts:
                ever_alert = True
            print(f"[{_ts()}] {qpart} | {hpart} | {tag}")

            # 死信 peek (前 3 条, 看是哪类失败)
            if dead_len > 0 and r is not None:
                try:
                    for it in r.lrange(QD, 0, 2):
                        try:
                            import json as _json
                            env = _json.loads(it)
                            print(f"           dead: msgid={env.get('payload', {}).get('msgid', '?')} "
                                  f"adapter={env.get('adapter')} attempts={env.get('attempts')}")
                        except Exception:
                            print(f"           dead: <unparseable> {str(it)[:80]}")
                except Exception:
                    pass

            time.sleep(args.interval)
    except KeyboardInterrupt:
        print(f"\n[{_ts()}] 中断, 退出")
    finally:
        client.close()

    print(f"[{_ts()}] 观察结束, 告警={'是' if ever_alert else '否'}")
    return 1 if ever_alert else 0


if __name__ == "__main__":
    sys.exit(main())
