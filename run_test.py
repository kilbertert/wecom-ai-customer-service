#!/usr/bin/env python3
"""
微信客服测试运行脚本

选择并运行合适的测试脚本
"""

import sys
import os

def show_menu():
    """显示菜单"""
    print("=" * 60)
    print("🧪 微信客服连接测试工具")
    print("=" * 60)
    print("请选择测试模式:")
    print("")
    print("1. 📡 独立测试服务器 (推荐用于简单验证)")
    print("   - 轻量级测试服务器")
    print("   - 只监听回调，不处理业务逻辑")
    print("   - 端口: 8001")
    print("")
    print("2. 🔧 集成业务测试 (推荐用于完整功能测试)")
    print("   - 基于完整的主服务")
    print("   - 包含Coze调用和回复发送")
    print("   - 端口: 8000")
    print("")
    print("0. 退出")
    print("=" * 60)

def main():
    """主函数"""
    while True:
        show_menu()

        try:
            choice = input("请选择 (0-2): ").strip()

            if choice == "0":
                print("👋 再见!")
                break

            elif choice == "1":
                print("\n🚀 启动独立测试服务器...")
                print("📝 提示: 记得将回调URL配置为 http://your-domain.com:8001/wechat/kf/callback")
                print("按 Ctrl+C 停止服务器\n")
                os.system("python test_wechat_callback.py")

            elif choice == "2":
                print("\n🚀 启动集成业务测试...")
                print("📝 提示: 记得将回调URL配置为 http://your-domain.com:8000/wechat/kf/callback")
                print("确保 .env 文件已正确配置")
                print("按 Ctrl+C 停止服务器\n")

                # 检查.env文件
                if not os.path.exists('.env'):
                    print("❌ 错误: 未找到 .env 配置文件")
                    print("请先运行: cp env.example .env")
                    print("然后编辑 .env 文件配置相关参数\n")
                    continue

                os.system("python test_wechat_simple.py")

            else:
                print("❌ 无效选择，请重新输入\n")

        except KeyboardInterrupt:
            print("\n👋 测试结束")
            break
        except Exception as e:
            print(f"\n❌ 运行出错: {str(e)}")
            continue

if __name__ == "__main__":
    main()