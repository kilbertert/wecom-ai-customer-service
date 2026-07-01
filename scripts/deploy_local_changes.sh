#!/bin/bash
# deploy_local_changes.sh — 推送本地代码修改到生产服务器 + 重启 uvicorn
#
# 用法:
#   SSH_PASSWORD=xxx ./deploy_local_changes.sh                          # 推送 git status 检测到的所有改动文件
#   SSH_PASSWORD=xxx ./deploy_local_changes.sh app/services/x.py        # 推送指定文件
#   SSH_PASSWORD=xxx ./deploy_local_changes.sh --from-commit HEAD~3     # 推送自 HEAD~3 以来的改动
#
# 行为:
#   1) 自动检测改动文件 (git status 或 git diff)
#   2) 把每个文件 scp 到远端 (保留相对路径)
#   3) 对比本地/远端 md5, 不一致则报错
#   4) 调远端的 /root/start_uvicorn.sh 重启 uvicorn
#   5) /monitoring/health 健康检查
#   6) 自动清理 /tmp/.sshpw 密码文件 (如果由本脚本创建)
#
# 环境变量 (都有默认值):
#   REMOTE_HOST        默认 120.55.45.59
#   REMOTE_PORT        默认 2134
#   REMOTE_USER        默认 root
#   REMOTE_DIR         默认 /root/ai-customer/wecom-ai-customer-service-main
#   REMOTE_HEALTH_URL  默认 http://127.0.0.1:8501/monitoring/health
#   REMOTE_RESTART     默认 /root/start_uvicorn.sh
#   SSH_PASSWORD       必填 (除非 /tmp/.sshpw 已存在)

set -euo pipefail

# ----- 配置 -----
REMOTE_HOST="${REMOTE_HOST:-120.55.45.59}"
REMOTE_PORT="${REMOTE_PORT:-2134}"
REMOTE_USER="${REMOTE_USER:-root}"
REMOTE_DIR="${REMOTE_DIR:-/root/ai-customer/wecom-ai-customer-service-main}"
REMOTE_HEALTH_URL="${REMOTE_HEALTH_URL:-http://127.0.0.1:8501/monitoring/health}"
REMOTE_RESTART="${REMOTE_RESTART:-/root/start_uvicorn.sh}"
SSH_PW_FILE="${SSH_PW_FILE:-/tmp/.sshpw}"

# ----- 参数解析 -----
FROM_COMMIT=""
FILES=""
while [ $# -gt 0 ]; do
    case "$1" in
        --from-commit)
            FROM_COMMIT="$2"; shift 2 ;;
        --help|-h)
            sed -n '2,21p' "$0"; exit 0 ;;
        *)
            FILES="$FILES $1"; shift ;;
    esac
done

# ----- 自动检测改动文件 -----
if [ -z "$FILES" ]; then
    if [ -n "$FROM_COMMIT" ]; then
        FILES=$(git diff --name-only "$FROM_COMMIT"..HEAD 2>/dev/null || true)
    else
        # 默认: 未跟踪 + 已修改 + 已暂存的所有文件 (排除目录)
        FILES=$(git status --porcelain 2>/dev/null | awk '{print $2}' | grep -v '^$' || true)
        FILES=$(echo "$FILES" | grep -v '/$' || true)
    fi
    FILES=$(echo "$FILES" | xargs)
fi

if [ -z "$FILES" ]; then
    echo "[ERR] 没有检测到任何改动文件"
    echo "      用法: $0 file1 [file2 ...]"
    echo "            $0 --from-commit HEAD~3"
    echo "            (无参数时自动用 git status 检测)"
    exit 1
fi

echo "==> 待部署文件:"
for f in $FILES; do echo "    - $f"; done
echo ""

# ----- 写 SSH 密码文件 -----
PW_FILE_CREATED_BY_SCRIPT=0
if [ ! -f "$SSH_PW_FILE" ]; then
    if [ -z "${SSH_PASSWORD:-}" ]; then
        echo "[ERR] SSH_PASSWORD 未设置, $SSH_PW_FILE 也不存在"
        echo "      任选其一:"
        echo "        SSH_PASSWORD=xxx $0 ..."
        echo "        或者: cat > $SSH_PW_FILE <<EOF"
        echo "                 #!/bin/bash"
        echo "                 echo '你的密码'"
        echo "                 EOF"
        exit 1
    fi
    cat > "$SSH_PW_FILE" <<EOF
#!/bin/bash
echo '${SSH_PASSWORD}'
EOF
    chmod 700 "$SSH_PW_FILE"
    PW_FILE_CREATED_BY_SCRIPT=1
else
    echo "[KEY] 使用已存在的 $SSH_PW_FILE (不会覆盖)"
fi

cleanup() {
    if [ "$PW_FILE_CREATED_BY_SCRIPT" = "1" ] && [ -f "$SSH_PW_FILE" ]; then
        rm -f "$SSH_PW_FILE"
        echo ""
        echo "[CLEAN] 已清理 $SSH_PW_FILE"
    fi
}
trap cleanup EXIT

# ----- SSH 辅助函数 -----
ssh_run() {
    SSH_ASKPASS="$SSH_PW_FILE" SSH_ASKPASS_REQUIRE=force DISPLAY=:0 \
        setsid -w ssh -p "$REMOTE_PORT" -o StrictHostKeyChecking=no \
        -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR \
        "$REMOTE_USER@$REMOTE_HOST" "$1"
}

scp_put() {
    SSH_ASKPASS="$SSH_PW_FILE" SSH_ASKPASS_REQUIRE=force DISPLAY=:0 \
        setsid -w scp -P "$REMOTE_PORT" -o StrictHostKeyChecking=no \
        -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR \
        "$1" "$2"
}

# ----- 1) scp 每个文件 -----
echo ""
echo "==> [1/4] scp 推送..."
PUSHED=0
for f in $FILES; do
    if [ ! -f "$f" ]; then
        echo "    [SKIP] 本地不存在: $f"
        continue
    fi
    echo "    [PUSH] $f"
    scp_put "$f" "$REMOTE_USER@$REMOTE_HOST:$REMOTE_DIR/$f"
    PUSHED=$((PUSHED+1))
done
if [ "$PUSHED" -eq 0 ]; then
    echo "[ERR] 没有任何文件实际推送, 中止"
    exit 1
fi
echo "    共推送 $PUSHED 个文件"

# ----- 2) 验证 md5 -----
echo ""
echo "==> [2/4] 验证 md5..."
EXISTING_LOCAL=""
for f in $FILES; do
    if [ -f "$f" ]; then EXISTING_LOCAL="$EXISTING_LOCAL $f"; fi
done
LOCAL_MD5=$(md5sum $EXISTING_LOCAL 2>/dev/null | sort)
echo "$LOCAL_MD5" | sed 's/^/    本地: /'
echo ""
REMOTE_MD5=$(ssh_run "cd $REMOTE_DIR && md5sum $EXISTING_LOCAL" | sort)
echo "$REMOTE_MD5" | sed 's/^/    远端: /'
echo ""
LOCAL_HASH=$(echo "$LOCAL_MD5" | awk '{print $1}' | sort | md5sum | awk '{print $1}')
REMOTE_HASH=$(echo "$REMOTE_MD5" | awk '{print $1}' | sort | md5sum | awk '{print $1}')
if [ "$LOCAL_HASH" != "$REMOTE_HASH" ]; then
    echo "[ERR] md5 不一致, 部署中止 (检查远端磁盘或网络)"
    exit 2
fi
echo "    [OK] md5 一致"

# ----- 3) 重启 -----
echo ""
echo "==> [3/4] 重启 uvicorn (调 $REMOTE_RESTART)..."
ssh_run "bash $REMOTE_RESTART" || true

# ----- 4) 健康检查 -----
echo ""
echo "==> [4/4] 健康检查..."
sleep 5
HEALTH_HTTP=$(ssh_run "curl -sS -o /dev/null -w '%{http_code}' $REMOTE_HEALTH_URL" 2>/dev/null || echo "000")
HEALTH_JSON=$(ssh_run "curl -sS $REMOTE_HEALTH_URL" 2>/dev/null || true)
if [ "$HEALTH_HTTP" = "200" ]; then
    echo "    [OK] 服务健康: HTTP $HEALTH_HTTP"
    echo "          $HEALTH_JSON"
else
    echo "    [WARN] 服务异常: HTTP $HEALTH_HTTP"
    echo "          $HEALTH_JSON"
    echo "          看日志: ssh ... 'tail -30 /tmp/uvicorn.log'"
    exit 3
fi

echo ""
echo "==> 部署完成!"
