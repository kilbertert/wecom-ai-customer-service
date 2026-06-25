#!/bin/bash
# WeChat AI Service uvicorn 启动脚本（最终版）
# 用法：bash start_uvicorn.sh

set -e

PROJECT_DIR="/root/ai-customer/wecom-ai-customer-service-main"

# 杀掉旧进程（覆盖 inline `python3 -c` 和独立 run_uvicorn.py 两种启动方式）
pkill -9 -f "uvicorn app.main:app" 2>/dev/null || true
pkill -9 -f "run_uvicorn.py" 2>/dev/null || true
# 同时杀掉持有 8501 端口的进程（兜底）
fuser -k 8501/tcp 2>/dev/null || true
sleep 3

# 清缓存
find "$PROJECT_DIR" -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
find "$PROJECT_DIR" -name "*.pyc" -delete 2>/dev/null || true

# 清空日志
: > /tmp/uvicorn.log

# 用 setsid + nohup 启动 Python 启动脚本（不是 -c 字符串，避免引号问题）
setsid nohup "$PROJECT_DIR/venv/bin/python3" "$PROJECT_DIR/run_uvicorn.py" \
    </dev/null >>/tmp/uvicorn.log 2>&1 &

echo "started pid $!"
sleep 5

# 验证
if ss -tlnp 2>/dev/null | grep -qE ":8501\b"; then
    NEW_PID=$(ss -tlnp 2>/dev/null | grep ":8501" | grep -oE "pid=[0-9]+" | head -1 | cut -d= -f2)
    CWD=$(readlink /proc/$NEW_PID/cwd 2>/dev/null)
    echo "✅ 8501 listening, pid=$NEW_PID, cwd=$CWD"
    curl -sS -o - -w "\nHTTP=%{http_code}\n" --max-time 5 http://127.0.0.1:8501/monitoring/health
else
    echo "❌ 8501 not up; tail log:"
    tail -30 /tmp/uvicorn.log
fi