# 使用Python 3.11作为基础镜像
FROM python:3.11

# 设置工作目录
WORKDIR /app

# 安装系统依赖 (curl: healthcheck; ffmpeg/ffprobe: 语音转码; gcc: 编译)
RUN apt-get update && apt-get install -y \
    gcc \
    curl \
    wget \
    xz-utils \
    && rm -rf /var/lib/apt/lists/*
RUN wget https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz \
    && tar xf ffmpeg-release-amd64-static.tar.xz \
    && mv ffmpeg-*-amd64-static/ffmpeg /usr/local/bin/ \
    && mv ffmpeg-*-amd64-static/ffprobe /usr/local/bin/ \
    && chmod +x /usr/local/bin/ffmpeg /usr/local/bin/ffprobe \
    && rm -rf ffmpeg-release-amd64-static.tar.xz ffmpeg-*-amd64-static

# 复制依赖文件
COPY requirements.txt .

# 安装Python依赖
RUN pip install --no-cache-dir -r requirements.txt

# 复制应用代码
COPY . .

# 创建临时文件目录
RUN mkdir -p temp_media

# 设置环境变量
ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1
# 容器内固定 8000 (与 EXPOSE/healthcheck 一致); 主机经 compose 映射 8501:8000。
# .env 的 APP_PORT 若为 8501 会被此 ENV 覆盖 (OS env 优先于 env_file)。
ENV APP_PORT=8000

# 暴露端口
EXPOSE 8000

# 健康检查 (curl 由 apt 安装; ${APP_PORT:-8000} 跟随实际端口)
HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:${APP_PORT:-8000}/monitoring/health || exit 1

# 启动命令
CMD ["python", "run.py"]
