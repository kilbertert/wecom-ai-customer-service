#!/bin/bash
# 启动脚本

set -e

echo "🚀 启动微信Coze服务..."

# 检查环境变量
if [ ! -f ".env" ]; then
    echo "❌ 错误: .env 文件不存在，请复制 env.example 并配置"
    exit 1
fi

# 检查Python环境
if ! command -v python &> /dev/null; then
    echo "❌ 错误: Python 未安装"
    exit 1
fi

# 安装依赖（如果需要）
if [ ! -d "venv" ]; then
    echo "📦 创建虚拟环境..."
    python -m venv venv
fi

echo "🔧 激活虚拟环境..."
source venv/bin/activate

echo "📦 安装依赖..."
pip install -r requirements.txt

# 创建必要的目录
mkdir -p temp_media logs

# 启动服务
echo "🌟 启动服务..."
if [ "$1" = "dev" ]; then
    echo "开发模式启动..."
    python run.py
else
    echo "生产模式启动..."
    # 这里可以添加gunicorn或其他生产服务器
    python run.py
fi