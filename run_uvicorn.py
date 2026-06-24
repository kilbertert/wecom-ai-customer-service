"""uvicorn 启动脚本 - 确保 cwd 正确 + 启动时打印诊断信息"""
import os
import sys

# 强制设置 cwd 到本文件所在目录
HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE)
sys.path.insert(0, HERE)

# 打印关键诊断
from app.core.config import settings
print(f">>> uvicorn cwd = {os.getcwd()}", flush=True)
print(f">>> api_token prefix = {settings.coze.api_token.get_secret_value()[:8]} (len={len(settings.coze.api_token.get_secret_value())})", flush=True)
print(f">>> bot_id = {settings.coze.bot_id}", flush=True)
print(f">>> api_base_url = {settings.coze.api_base_url}", flush=True)

import uvicorn
uvicorn.run("app.main:app", host="0.0.0.0", port=8501, log_level="info")