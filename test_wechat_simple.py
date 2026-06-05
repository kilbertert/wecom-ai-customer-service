#!/usr/bin/env python3
"""
简化的微信客服回调测试脚本

直接运行主服务，并添加额外的日志输出专门用于测试回调接收。
"""

import sys
import os
import time
from datetime import datetime

def test_callback_connection():
    """测试微信回调连接"""

    print("=" * 60)
    print("🧪 微信客服回调连接测试")
    print("=" * 60)

    # 检查环境配置
    if not os.path.exists('.env'):
        print("❌ 错误: 未找到 .env 配置文件")
        print("请先复制 env.example 到 .env 并配置相关参数")
        return False

    print("✅ 配置文件存在")

    # 检查端口占用
    try:
        import socket
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        result = sock.connect_ex(('127.0.0.1', 8000))
        sock.close()

        if result == 0:
            print("⚠️  警告: 端口8000已被占用，可能已有服务运行")
        else:
            print("✅ 端口8000可用")

    except Exception as e:
        print(f"⚠️  端口检查失败: {e}")

    print("\n📋 测试步骤:")
    print("1. 确保微信企业后台已配置回调URL为: http://your-domain.com/wechat/kf/callback")
    print("2. 启动此测试脚本")
    print("3. 让用户通过微信客服发送消息")
    print("4. 观察控制台输出的回调信息")

    print("\n🚀 启动测试服务...")
    print("按 Ctrl+C 停止测试")
    print("=" * 60)

    return True

def run_with_callback_logging():
    """运行服务并添加回调日志"""

    # 修改环境变量，启用详细日志
    os.environ['APP_DEBUG'] = 'true'

    # 导入并运行主应用
    try:
        from app.main import app
        import uvicorn

        print(f"\n[{datetime.now().strftime('%H:%M:%S')}] 🔄 启动微信回调测试服务...")
        print(f"[{datetime.now().strftime('%H:%M:%S')}] 📡 服务地址: http://localhost:8000")
        print(f"[{datetime.now().strftime('%H:%M:%S')}] 🎯 回调地址: http://localhost:8000/wechat/kf/callback")
        print(f"[{datetime.now().strftime('%H:%M:%S')}] 📊 监控地址: http://localhost:8000/monitoring/health")
        print()

        # 添加自定义日志中间件
        @app.middleware("http")
        async def callback_logging_middleware(request, call_next):
            if request.url.path == "/wechat/kf/callback":
                timestamp = datetime.now().strftime("%H:%M:%S.%f")[:-3]
                method = request.method

                print(f"\n[{timestamp}] 📨 微信回调请求: {method} {request.url.path}")

                if method == "POST":
                    try:
                        body = await request.body()
                        xml_content = body.decode('utf-8', errors='ignore')

                        # 简单解析XML内容
                        import xml.etree.ElementTree as ET
                        try:
                            root = ET.fromstring(xml_content)
                            msg_type = root.find('MsgType')
                            event = root.find('Event')

                            if msg_type is not None:
                                print(f"[{timestamp}] 📝 消息类型: {msg_type.text}")
                            if event is not None:
                                print(f"[{timestamp}] 🎯 事件类型: {event.text}")

                            # 如果是消息事件，显示更多信息
                            if event is not None and event.text == 'kf_msg_or_event':
                                token = root.find('Token')
                                if token is not None:
                                    print(f"[{timestamp}] 🔑 同步Token: {token.text}")

                        except ET.ParseError:
                            print(f"[{timestamp}] ⚠️  XML解析失败，可能为加密消息")

                        # 显示XML长度
                        print(f"[{timestamp}] 📏 数据长度: {len(xml_content)} 字符")

                    except Exception as e:
                        print(f"[{timestamp}] ❌ 解析失败: {str(e)}")

                elif method == "GET":
                    # 回调验证请求
                    query_params = dict(request.query_params)
                    signature = query_params.get('signature', 'N/A')
                    timestamp_param = query_params.get('timestamp', 'N/A')
                    nonce = query_params.get('nonce', 'N/A')
                    echostr = query_params.get('echostr', 'N/A')

                    print(f"[{timestamp}] 🔍 回调验证:")
                    print(f"[{timestamp}]    签名: {signature[:16]}...")
                    print(f"[{timestamp}]    时间戳: {timestamp_param}")
                    print(f"[{timestamp}]    随机数: {nonce}")
                    print(f"[{timestamp}]    验证串: {echostr[:16]}...")

            response = await call_next(request)
            return response

        # 启动服务
        uvicorn.run(
            app,
            host="0.0.0.0",
            port=8000,
            log_level="warning"  # 降低uvicorn日志级别，避免干扰
        )

    except KeyboardInterrupt:
        print(f"\n[{datetime.now().strftime('%H:%M:%S')}] 🛑 测试服务已停止")
    except Exception as e:
        print(f"\n[{datetime.now().strftime('%H:%M:%S')}] ❌ 服务启动失败: {str(e)}")
        import traceback
        traceback.print_exc()

def main():
    """主函数"""
    if not test_callback_connection():
        return 1

    try:
        run_with_callback_logging()
    except KeyboardInterrupt:
        print("\n测试结束")
        return 0
    except Exception as e:
        print(f"\n❌ 测试过程中出错: {str(e)}")
        return 1

if __name__ == "__main__":
    sys.exit(main())