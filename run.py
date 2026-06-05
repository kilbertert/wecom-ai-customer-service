#!/usr/bin/env python3
"""应用启动脚本"""
import uvicorn
from app.core.config import settings


def main():
    """主启动函数"""
    uvicorn.run(
        "app.main:app",
        host=settings.app.host,
        port=settings.app.port,
        workers=settings.app.workers,
        reload=settings.app.debug,
        log_level=settings.app.log_level.lower(),
        access_log=True,
    )
    


if __name__ == "__main__":
    main()