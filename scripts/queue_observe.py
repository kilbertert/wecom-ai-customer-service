#!/usr/bin/env python3
"""灰度观察脚本: 轮询 redis 消息队列深度 + 健康端点, 异常告警。

部署 (#15 持久队列) 后用于灰度观察:
  - wecom:msgq      主队列深度 (积压)
  - wecom:msgq:proc:* 各实例处理中深度 + 单条 delivery 在途年龄
  - wecom:msgq:dead 死信 (> 0 = 有消息反复失败, 需排查)
  - /monitoring/health       存活
  - /monitoring/health/ready 就绪 (配置占位 / Dify 不可达 -> 503)

任一告警 (dead>0 / proc 超容量 / 单条 delivery 超时 / 非 200) -> 退出码非零,
可作 canary gate:
  python3 scripts/queue_observe.py --duration 300   # 观察 5 分钟, 有告警则 exit 1

默认读 app settings (prod .env) 取 redis 与端口; 也可 --redis-url / --health-url 覆盖。
仅 memory 队列模式 (APP_MESSAGE_QUEUE=memory) 无 redis 队列, 本脚本退化为只看健康。
"""
from __future__ import annotations

import argparse
import hashlib
import json
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


def _processing_items(redis_client) -> list:
    """读取旧版全局 proc + 所有实例 ``proc:{consumer_id}`` 的 in-flight。"""
    keys = [RedisMessageQueue.Q_PROC]
    keys.extend(redis_client.scan_iter(match=f"{RedisMessageQueue.Q_PROC_PREFIX}*"))
    seen = set()
    items = []
    for key in keys:
        if isinstance(key, bytes):
            key = key.decode("utf-8", errors="ignore")
        if key in seen:
            continue
        seen.add(key)
        items.extend(redis_client.lrange(key, 0, -1))
    return items


def _delivery_id(raw) -> str:
    """从队列 envelope 取稳定 delivery id；坏数据用内容摘要兜底。"""
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    try:
        env = json.loads(raw)
        delivery_id = env.get("id") or (env.get("payload") or {}).get("msgid")
        if delivery_id:
            return str(delivery_id)
    except Exception:
        pass
    return "raw-" + hashlib.sha1(str(raw).encode("utf-8")).hexdigest()[:12]


def _processing_started_at(raw) -> float | None:
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    try:
        value = json.loads(raw).get("processing_started_at")
        return float(value) if value is not None else None
    except Exception:
        return None


def _stuck_deliveries(
    items: list, first_seen: dict[str, float], now: float, max_age: float
) -> list[tuple[str, int]]:
    """跟踪同一 delivery 连续停留在任一 proc list 的时长。"""
    current = set()
    for raw in items:
        delivery_id = _delivery_id(raw)
        current.add(delivery_id)
        started_at = _processing_started_at(raw)
        initial = min(now, started_at) if started_at is not None else now
        if delivery_id not in first_seen:
            first_seen[delivery_id] = initial
        else:
            first_seen[delivery_id] = min(first_seen[delivery_id], initial)
    for delivery_id in set(first_seen) - current:
        first_seen.pop(delivery_id, None)
    return sorted(
        (
            (delivery_id, int(now - first_seen[delivery_id]))
            for delivery_id in current
            if now - first_seen[delivery_id] >= max_age
        ),
        key=lambda item: item[1],
        reverse=True,
    )


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
                    help="proc 连续超容量多少轮判为卡死 (默认 6 ≈ 30s)")
    ap.add_argument("--workers", type=int,
                    default=int(getattr(settings.app, "queue_workers", 2) or 2),
                    help="所有服务实例的 worker 总数; proc>workers 持续判超容量 "
                         "(单实例默认读 APP_QUEUE_WORKERS, 多实例请显式传总数)")
    ap.add_argument(
        "--proc-stuck-seconds", type=int,
        default=int(getattr(settings.app, "queue_lock_ttl", 600) or 600) + 60,
        help="同一 delivery 连续停留在 proc 的告警秒数 "
             "(默认 APP_QUEUE_LOCK_TTL+60, 避免正常长 Dify 误报)",
    )
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

    proc_over = 0             # proc 连续超容量 (>workers) 轮数
    proc_first_seen: dict[str, float] = {}
    ever_alert = False
    start = time.time()
    QM, QD = RedisMessageQueue.Q_MAIN, RedisMessageQueue.Q_DEAD

    try:
        while True:
            now = time.time()
            if args.duration and (now - start) >= args.duration:
                break

            main_len = proc_len = dead_len = -1
            proc_items = []
            if r is not None:
                try:
                    main_len = r.llen(QM)
                    proc_items = _processing_items(r)
                    proc_len = len(proc_items)
                    dead_len = r.llen(QD)
                except Exception as e:
                    print(f"[{_ts()}] [WARN] redis 查询失败: {e}")

            health = http_code("/monitoring/health")
            ready = http_code("/monitoring/health/ready")

            alerts = []
            if dead_len > 0:
                alerts.append(f"DEAD={dead_len}")
            # proc>workers 持续 = 超容量积压 (审查 P1 #8): 正常在途 proc<=workers
            # 不告警; 单条 Dify 可跑最长 ~lock_ttl=600s, 仅 proc>0 会大量误报。
            if proc_len > args.workers:
                proc_over += 1
                if proc_over >= args.proc_stuck_polls:
                    alerts.append(
                        f"PROC 超容量卡死({proc_len}>w{args.workers}@{proc_over}轮)"
                    )
            else:
                proc_over = 0
            # 数量无法识别“全部 worker 都卡死且 proc==workers”。按 delivery id 连续在途
            # 年龄补齐这一盲区；消息正常完成/回队后会从 first_seen 移除。
            stuck = _stuck_deliveries(
                proc_items, proc_first_seen, now, args.proc_stuck_seconds
            )
            if stuck:
                sample = ",".join(f"{did[:8]}:{age}s" for did, age in stuck[:3])
                alerts.append(f"PROC_STUCK={len(stuck)}[{sample}]")
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
