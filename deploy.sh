#!/bin/bash

# Docker安全部署脚本
# 用于快速、安全地部署微信Coze服务

set -e  # 遇到错误立即退出

# 颜色输出
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# 日志函数
log_info() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# 检查命令是否存在
check_command() {
    if ! command -v $1 &> /dev/null; then
        log_error "$1 命令未找到，请先安装"
        exit 1
    fi
}

# 主函数
main() {
    log_info "开始Docker安全部署..."

    # 检查必需命令
    check_command docker
    check_command docker-compose

    # 检查环境变量文件
    if [ ! -f ".env" ]; then
        log_error ".env 文件不存在"
        log_info "请复制 env.template 到 .env 并填入正确的配置值"
        log_info "cp env.template .env"
        exit 1
    fi

    # 检查环境变量文件权限
    if [ -w ".env" ] && [ $(stat -c %a .env 2>/dev/null || stat -f %A .env) != "600" ]; then
        log_warn ".env 文件权限不安全，建议设置为600"
        log_info "运行: chmod 600 .env"
    fi

    # 验证环境变量
    log_info "验证环境变量配置..."
    if [ -f "verify_deployment.py" ]; then
        python3 verify_deployment.py --url http://localhost:8000 || {
            log_error "环境变量验证失败"
            exit 1
        }
    else
        log_warn "verify_deployment.py 不存在，跳过验证"
    fi

    # 创建必要的目录
    mkdir -p temp_media logs

    # 停止现有服务
    log_info "停止现有服务..."
    docker-compose down || true

    # 清理未使用的资源
    log_info "清理Docker资源..."
    docker system prune -f || true

    # 构建镜像
    log_info "构建Docker镜像..."
    docker-compose build --no-cache

    # 启动服务
    log_info "启动服务..."
    docker-compose up -d

    # 等待服务启动
    log_info "等待服务启动..."
    sleep 10

    # 验证部署
    log_info "验证部署结果..."
    if python3 verify_deployment.py --url http://localhost:8000; then
        log_info "🎉 部署成功！"
        log_info ""
        log_info "服务信息:"
        log_info "  - Web界面: http://localhost:8000"
        log_info "  - API文档: http://localhost:8000/docs"
        log_info "  - 健康检查: http://localhost:8000/monitoring/health"
        log_info ""
        log_info "查看日志: docker-compose logs -f"
        log_info "停止服务: docker-compose down"
    else
        log_error "部署验证失败"
        log_info "查看日志: docker-compose logs"
        exit 1
    fi
}

# 参数处理
case "${1:-}" in
    "stop")
        log_info "停止服务..."
        docker-compose down
        log_info "服务已停止"
        ;;
    "restart")
        log_info "重启服务..."
        docker-compose restart
        log_info "服务已重启"
        ;;
    "logs")
        log_info "查看日志..."
        docker-compose logs -f
        ;;
    "status")
        log_info "服务状态..."
        docker-compose ps
        ;;
    "clean")
        log_info "清理Docker资源..."
        docker-compose down -v
        docker system prune -f
        docker volume prune -f
        log_info "清理完成"
        ;;
    "verify")
        log_info "验证部署..."
        python3 verify_deployment.py --url http://localhost:8000
        ;;
    *)
        main
        ;;
esac