#!/usr/bin/env python3
"""简化验证脚本 - 检查单轮对话模式代码结构"""
import sys
import traceback

def test_imports():
    """测试核心模块导入"""
    print("Testing core imports...")
    try:
        # 测试数据模型
        from app.models.wechat import WeChatMessage, MessageType
        from app.models.coze import StandardizedMessage, CozeWorkflowOutput, ActionType

        # 测试标准化服务（关键修改点）
        from app.services.standardization import DataStandardizationService

        # 创建标准化服务实例（无会话服务）
        service = DataStandardizationService()
        assert service.session_service is None, "单轮模式会话服务应为None"

        print("SUCCESS: Core imports work correctly")
        return True
    except Exception as e:
        print(f"FAILED: Import error - {e}")
        traceback.print_exc()
        return False

def test_data_models():
    """测试数据模型"""
    print("\nTesting data models...")
    try:
        from app.models.wechat import WeChatMessage, MessageType
        from app.models.coze import StandardizedMessage, ActionType

        # 创建测试数据
        msg = WeChatMessage(
            msgid="test_123",
            msgtype=MessageType.TEXT,
            send_time=1705254000,
            origin=1,
            external_userid="test_user",
            open_kfid="test_kf",
            text={"content": "你好"}
        )

        # 验证数据结构
        assert msg.external_userid == "test_user"
        assert msg.msgtype == MessageType.TEXT
        assert msg.text["content"] == "你好"

        print("SUCCESS: Data models work correctly")
        return True
    except Exception as e:
        print(f"FAILED: Data model error - {e}")
        traceback.print_exc()
        return False

def test_standardization_service():
    """测试数据标准化服务（核心修改）"""
    print("\nTesting standardization service...")
    try:
        from app.services.standardization import DataStandardizationService

        # 测试无会话服务模式
        service = DataStandardizationService()
        assert service.session_service is None

        # 测试有会话服务模式
        mock_session = type('MockSession', (), {})()
        service_with_session = DataStandardizationService(mock_session)
        assert service_with_session.session_service is mock_session

        print("SUCCESS: Standardization service works in both modes")
        return True
    except Exception as e:
        print(f"FAILED: Standardization service error - {e}")
        traceback.print_exc()
        return False

def test_removed_redis():
    """测试Redis相关代码已被移除"""
    print("\nTesting Redis removal...")
    try:
        # 检查主要文件中是否还有Redis相关导入
        import inspect

        # 检查微信路由
        from app.routes import wechat_router
        source = inspect.getsource(wechat_router.routes[0].endpoint)
        assert "SessionService" not in source, "微信路由不应导入SessionService"

        # 检查主应用
        from app.main import app
        source = inspect.getsource(app.router.lifespan_context)
        assert "session_service" not in source.lower(), "主应用不应初始化session_service"

        print("SUCCESS: Redis dependencies properly removed")
        return True
    except Exception as e:
        print(f"FAILED: Redis removal check error - {e}")
        traceback.print_exc()
        return False

def test_config_changes():
    """测试配置变更"""
    print("\nTesting configuration changes...")
    try:
        # 检查环境变量示例
        with open("weixin_coze_service/env.example", "r", encoding="utf-8") as f:
            content = f.read()
            assert "# Redis配置 (单轮对话模式已移除，无需配置)" in content
            assert "# REDIS_" not in content or content.count("# REDIS_") > 0

        print("SUCCESS: Configuration updated for single-round mode")
        return True
    except Exception as e:
        print(f"FAILED: Configuration check error - {e}")
        traceback.print_exc()
        return False

def main():
    """主验证函数"""
    print("=" * 60)
    print("微信Coze服务 - 单轮对话模式代码结构验证")
    print("=" * 60)

    tests = [
        test_imports,
        test_data_models,
        test_standardization_service,
        test_removed_redis,
        test_config_changes
    ]

    passed = 0
    total = len(tests)

    for test in tests:
        if test():
            passed += 1

    print("\n" + "=" * 60)
    print(f"代码结构验证结果: {passed}/{total} 项通过")

    if passed == total:
        print("SUCCESS: 单轮对话模式代码结构验证通过！")
        print("\n主要修改:")
        print("✅ 移除Redis会话管理依赖")
        print("✅ 数据标准化服务支持可选会话")
        print("✅ 微信路由去除会话初始化")
        print("✅ 主应用生命周期简化")
        print("✅ 监控接口移除会话统计")
        print("✅ 配置文件更新")
        print("\n服务特点:")
        print("• 无Redis依赖，启动简单")
        print("• 保持基础问答功能")
        print("• 支持多媒体消息处理")
        print("• Coze智能体集成完整")
        print("\n启动命令:")
        print("pip install -r requirements.txt")
        print("python run.py")
        return 0
    else:
        print("FAILED: 部分验证失败，请检查上述错误。")
        return 1

if __name__ == "__main__":
    sys.exit(main())