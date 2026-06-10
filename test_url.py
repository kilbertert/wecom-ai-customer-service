#!/usr/bin/env python3
"""
快速测试回调URL是否可访问
"""

import requests
import sys

def test_callback_url():
    """测试回调URL"""
    url = "https://lourie-avulsed-tobie.ngrok-free.dev/wechat/kf/callback"
    
    print("测试回调URL可访问性...")
    print(f"URL: {url}")
    print()
    
    try:
        # 测试基本访问（不带参数）
        response = requests.get(url, timeout=10)
        print(f"状态码: {response.status_code}")
        
        if response.status_code == 200:
            print("✅ URL可访问")
            return True
        elif response.status_code == 422:
            print("✅ URL可访问（服务正常运行）")
            print("   422状态码是正常的，因为缺少必需的查询参数")
            print("   微信的验证请求会包含signature、timestamp、nonce等参数")
            print("   这说明服务已正确运行，可以接收微信的验证请求")
            
            # 尝试使用正确的参数测试
            print("\n尝试使用正确参数测试...")
            return test_with_signature(url)
        elif response.status_code == 403:
            print("⚠️  URL可访问，但返回403（可能是签名验证失败）")
            print("   这是正常的，因为测试请求没有正确的签名")
            print("   微信的验证请求会包含正确的签名，应该可以通过")
            return True
        else:
            print(f"❌ URL返回错误状态码: {response.status_code}")
            print(f"   响应内容: {response.text[:200]}")
            return False
            
    except requests.exceptions.ConnectionError:
        print("❌ 无法连接到URL")
        print("   可能原因:")
        print("   - 服务未启动")
        print("   - ngrok未运行")
        print("   - URL已失效")
        return False
    except requests.exceptions.Timeout:
        print("❌ 连接超时")
        return False
    except Exception as e:
        print(f"❌ 连接异常: {str(e)}")
        return False

def test_with_signature(url):
    """使用正确的签名测试"""
    import hashlib
    import time
    
    # 使用env.example中的Token
    token = "OCmjYUSjJhpsKUDpneDdWhoI"
    timestamp = str(int(time.time()))
    nonce = "test_nonce_" + timestamp
    echostr = "test_echo_string"
    
    # 生成签名
    params = [token, timestamp, nonce]
    params.sort()
    tmp_str = ''.join(params)
    signature = hashlib.sha1(tmp_str.encode('utf-8')).hexdigest()
    
    # 构造验证URL
    verify_url = f"{url}?signature={signature}&timestamp={timestamp}&nonce={nonce}&echostr={echostr}"
    
    try:
        response = requests.get(verify_url, timeout=10)
        print(f"   验证测试状态码: {response.status_code}")
        print(f"   响应内容: {response.text[:100]}")
        
        if response.status_code == 200 and response.text.strip() == echostr:
            print("   ✅ 签名验证测试通过！")
            print("   ✅ 回调URL配置正确，可以在微信后台配置了")
            return True
        elif response.status_code == 403:
            print("   ⚠️  签名验证失败")
            print("   可能原因: Token配置不匹配")
            print("   请检查env.example中的WECHAT_KF_TOKEN是否与微信后台一致")
            return False
        else:
            print(f"   ⚠️  验证测试返回: {response.status_code}")
            return False
    except Exception as e:
        print(f"   ❌ 验证测试异常: {str(e)}")
        return False

def check_service():
    """检查本地服务"""
    print("\n检查本地服务...")
    
    try:
        response = requests.get("http://localhost:8000/", timeout=2)
        if response.status_code == 200:
            print("✅ 本地服务运行正常 (8000端口)")
            return True
        else:
            print(f"⚠️  本地服务响应异常: {response.status_code}")
            return False
    except requests.exceptions.ConnectionError:
        print("❌ 本地服务未运行 (8000端口)")
        print("   请运行: python test_wechat_callback.py")
        return False
    except Exception as e:
        print(f"❌ 检查失败: {str(e)}")
        return False

def main():
    print("=" * 60)
    print("回调URL诊断工具")
    print("=" * 60)
    print()
    
    # 检查本地服务
    local_ok = check_service()
    
    # 测试回调URL
    url_ok = test_callback_url()
    
    print("\n" + "=" * 60)
    print("诊断结果:")
    print("=" * 60)
    print(f"本地服务: {'✅ 正常' if local_ok else '❌ 未运行'}")
    print(f"回调URL: {'✅ 可访问' if url_ok else '❌ 不可访问'}")
    
    if not local_ok:
        print("\n🔧 修复建议:")
        print("1. 启动服务: python test_wechat_callback.py")
        print("2. 确保服务运行在8000端口")
        print("3. 等待服务完全启动后再测试")
    
    if not url_ok:
        print("\n🔧 修复建议:")
        print("1. 检查ngrok是否运行: ngrok http 8000")
        print("2. 确认ngrok URL是否已更新")
        print("3. 检查网络连接")
        print("4. 确认服务已启动")
    
    if local_ok and url_ok:
        print("\n✅ 基础连接正常！")
        print("\n📋 下一步操作:")
        print("1. 在微信后台配置回调URL:")
        print("   https://lourie-avulsed-tobie.ngrok-free.dev/wechat/kf/callback")
        print("2. Token配置: OCmjYUSjJhpsKUDpneDdWhoI")
        print("3. EncodingAESKey: AujPZkqIfghgcqNUQKoFlmmORPekMWhRovTAUBnKfIb")
        print("4. 保存配置后，观察test_wechat_callback.py的控制台输出")
        print("5. 如果看到'签名验证通过'，说明配置成功！")

if __name__ == "__main__":
    main()
