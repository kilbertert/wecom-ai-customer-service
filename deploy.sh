#!/bin/bash

# Docker 部署脚本 (docker-compose 栈: wecom 服务 + redis + celery worker)
# 用于快速部署微信 AI 客服服务 (Dify 后端)。

set -e  # 遇到错误立即退出

# 颜色输出
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

HOST_PORT="${HOST_PORT:-8501}"   # 主机端口 (compose 映射 8501:8000)

log_info()  { echo -e "${GREEN}[INFO]${NC} $1"; }
log_warn()  { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"; }

check_command() {
    if ! command -v "$1" &> /dev/null; then
        log_error "$1 命令未找到，请先安装"
        exit 1
    fi
}

# 健康检查 (主机端口)
health_check() {
    local url="http://localhost:${HOST_PORT}/monitoring/health"
    local code
    code=$(curl -sS -o /dev/null -w '%{http_code}' "$url" 2>/dev/null || echo "000")
    if [ "$code" = "200" ]; then
        log_info "服务健康: HTTP $code  ($url)"
        return 0
    else
        log_warn "服务异常: HTTP $code  ($url)"
        return 1
    fi
}

main() {
    log_info "开始 Docker 部署..."

    check_command docker
    check_command curl

    if [ ! -f ".env" ]; then
        log_error ".env 文件不存在"
        log_info "请复制 env.example 到 .env 并填入真实配置值:"
        log_info "  cp env.example .env && chmod 600 .env"
        exit 1
    fi

    if [ -w ".env" ] && [ "$(stat -c %a .env 2>/dev/null || stat -f %A .env)" != "600" ]; then
        log_warn ".env 文件权限不安全，建议设置为 600"
        log_info "  chmod 600 .env"
    fi

    mkdir -p temp_media logs

    log_info "停止现有服务..."
    $DC down || true

    log_info "构建镜像..."
    $DC build

    log_info "启动服务 (redis + wecom + celery)..."
    $DC up -d

    log_info "等待服务启动..."
    sleep 10

    log_info "验证部署结果..."
    if health_check; then
        log_info "🎉 部署成功！"
        log_info ""
        log_info "服务信息:"
        log_info "  - Web/API:   http://localhost:${HOST_PORT}"
        log_info "  - API 文档:   http://localhost:${HOST_PORT}/docs"
        log_info "  - 健康检查:   http://localhost:${HOST_PORT}/monitoring/health"
        log_info "  - 就绪检查:   http://localhost:${HOST_PORT}/monitoring/health/ready"
        log_info ""
        log_info "查看日志:   $DC logs -f wecom-ai-service"
        log_info "队列观察:   python3 scripts/queue_observe.py"
        log_info "停止服务:   $DC down"
    else
        log_error "部署验证失败"
        log_info "查看日志: $DC logs"
        exit 1
    fi
}

# docker compose v2 (plugin) 优先, 回退 v1 (docker-compose)
if command -v docker &>/dev/null && docker compose version &>/dev/null; then
    DC="docker compose"
elif command -v docker-compose &>/dev/null; then
    DC="docker-compose"
else
    DC="docker-compose"  # main() 会 check_command 报错
fi

case "${1:-}" in
    stop)     log_info "停止服务..."; $DC down; log_info "服务已停止" ;;
    restart)  log_info "重启服务..."; $DC restart; log_info "服务已重启" ;;
    logs)     $DC logs -f ;;
    status)   $DC ps ;;
    clean)    log_info "清理 Docker 资源..."; $DC down -v; docker system prune -f; docker volume prune -f; log_info "清理完成" ;;
    verify)   health_check ;;
    *)        main ;;
esac
