#!/usr/bin/env python3
"""配置检查脚本"""
import os
import sys
from pathlib import Path

def check_env_file():
    """检查环境变量文件"""
    env_file = Path(".env")
    if not env_file.exists():
        print("ERROR: .env 文件不存在")
        print("请复制 env.example 到 .env 并配置相应参数")
        return False

    print("OK: .env 文件存在")
    return True

def check_required_env_vars():
    """检查必需的环境变量"""
    required_vars = [
        "WECHAT_CORP_ID",
        "WECHAT_CORP_SECRET",
        "WECHAT_KF_TOKEN",
        "WECHAT_ENCODING_AES_KEY",
        "COZE_API_TOKEN",
        "COZE_WORKFLOW_ID",
        "APP_SECRET_KEY"
        # 单轮对话模式，不需要REDIS_HOST
    ]

    missing_vars = []
    for var in required_vars:
        if not os.getenv(var):
            missing_vars.append(var)

    if missing_vars:
        print("ERROR: 缺少必需的环境变量:")
        for var in missing_vars:
            print(f"   - {var}")
        return False

    print("OK: 所有必需的环境变量都已设置")
    return True

def check_dependencies():
    """检查Python依赖"""
    try:
        import fastapi
        import redis
        import httpx
        import pydantic
        print("OK: Python依赖已安装")
        return True
    except ImportError as e:
        print(f"ERROR: 缺少Python依赖: {e}")
        print("请运行: pip install -r requirements.txt")
        return False

def check_redis_connection():
    """检查Redis连接"""
    try:
        import redis
        r = redis.Redis(
            host=os.getenv("REDIS_HOST", "localhost"),
            port=int(os.getenv("REDIS_PORT", 6379)),
            db=int(os.getenv("REDIS_DB", 0)),
            password=os.getenv("REDIS_PASSWORD")
        )
        r.ping()
        print("OK: Redis连接正常")
        return True
    except Exception as e:
        print(f"WARN: Redis连接失败: {e}")
        print("   确保Redis服务正在运行")
        return False

def check_services():
    """检查服务初始化"""
    try:
        from app.services.wechat import WeChatService
        from app.services.coze import CozeService
        from app.services.standardization import DataStandardizationService
        from app.services.media import MediaService

        # 测试服务初始化（单轮对话模式，无Redis）
        wechat_service = WeChatService()
        coze_service = CozeService()
        standardization_service = DataStandardizationService()  # 无会话服务
        media_service = MediaService(wechat_service)

        print("OK: 基础服务可以正常初始化（单轮对话模式）")
        return True
    except Exception as e:
        print(f"ERROR: 服务初始化失败: {e}")
        return False

def main():
    """主检查函数"""
    print("检查微信Coze服务配置...\n")

    checks = [
        check_env_file,
        check_required_env_vars,
        check_dependencies,
        check_services
        # 单轮对话模式，不检查Redis连接
    ]

    passed = 0
    total = len(checks)

    for check in checks:
        if check():
            passed += 1
        print()

    print(f"检查结果: {passed}/{total} 项通过")

    if passed == total:
        print("SUCCESS: 所有检查通过！单轮对话服务可以正常启动。")
        print("\n启动命令:")
        print("  python run.py  # 无需Redis即可运行")
        print("  ./start.sh     # 开发环境启动脚本")
        return 0
    else:
        print("FAILED: 部分检查失败，请修复上述问题后重试。")
        return 1

if __name__ == "__main__":
    sys.exit(main())