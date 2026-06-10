#!/usr/bin/env python3
"""
企业微信客服回调链路诊断工具

逐层检测:
  L1  本地 FastAPI 服务是否在 8000 端口跑起来
  L2  隧道(ngrok/vicp.fun 等)是否能从公网访问你的本机
  L3  完整回调 URL (带微信签名参数) 是否能正确响应

用法:
  python diagnose_callback.py                # 使用 .env 里的 WECHAT_CALLBACK_BASE_URL
  python diagnose_callback.py https://xxx    # 使用命令行传入的 URL
"""

import hashlib
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import requests

ENV_FILE = Path(__file__).parent / ".env"
LOCAL_BASE = "http://localhost:8000"
TIMEOUT = 10

GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
CYAN = "\033[96m"
RESET = "\033[0m"


def cprint(color: str, text: str) -> None:
    print(f"{color}{text}{RESET}")


def read_env() -> dict[str, str]:
    """极简 .env 解析 — 不依赖 pydantic,即使项目其他依赖坏了也能跑"""
    env: dict[str, str] = {}
    if not ENV_FILE.exists():
        cprint(YELLOW, f"[WARN] .env 不存在: {ENV_FILE}")
        return env
    for raw in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        env[key.strip()] = value.strip()
    return env


def get_callback_url(env: dict[str, str], override: str | None) -> str:
    """从 .env / 命令行获取要测试的回调 URL"""
    if override:
        return override.rstrip("/")
    base = env.get("WECHAT_CALLBACK_BASE_URL", "").rstrip("/")
    if not base:
        cprint(RED, "[FATAL] 找不到 WECHAT_CALLBACK_BASE_URL,也未通过命令行传入")
        cprint(RED, "        请设置 .env 或执行: python diagnose_callback.py https://你的域名")
        sys.exit(1)
    return f"{base}/wechat/kf/callback"


def check_local_service() -> bool:
    """L1: 本地服务"""
    cprint(CYAN, "\n[L1] 检查本地 FastAPI 服务")
    cprint(CYAN, f"     目标: {LOCAL_BASE}")
    try:
        r = requests.get(f"{LOCAL_BASE}/", timeout=2)
        if r.status_code == 200:
            data = r.json()
            cprint(GREEN, f"     ✅ 通过 — service={data.get('service')} version={data.get('version')}")
            return True
        cprint(RED, f"     ❌ 状态码异常: {r.status_code}")
        return False
    except requests.exceptions.ConnectionError:
        cprint(RED, "     ❌ 无法连接 — 本地服务没跑")
        cprint(YELLOW, "     💡 启动命令: python run.py")
        return False
    except Exception as e:
        cprint(RED, f"     ❌ 异常: {e}")
        return False


def check_route_exists(callback_url: str) -> bool:
    """L2a: 先不带参数 GET 一下,确认隧道能穿透到 FastAPI 的 /wechat/kf/callback 路由"""
    cprint(CYAN, "\n[L2] 检查隧道是否打通到 /wechat/kf/callback")
    cprint(CYAN, f"     目标: {callback_url}")
    parsed = urlparse(callback_url)
    root = f"{parsed.scheme}://{parsed.netloc}/"
    try:
        r_root = requests.get(root, timeout=TIMEOUT)
        if r_root.status_code != 200:
            cprint(YELLOW, f"     ⚠️  根路径返回 {r_root.status_code} (前 80 字符): {r_root.text[:80]!r}")
            cprint(YELLOW, "     这通常意味着:")
            cprint(YELLOW, "       - ngrok 免费版警告页(需要浏览器先点 'Visit Site')")
            cprint(YELLOW, "       - 隧道服务已停止/URL 已失效")
        else:
            cprint(GREEN, f"     ✅ 隧道根路径可达 ({r_root.status_code})")
    except requests.exceptions.ConnectionError:
        cprint(RED, "     ❌ 隧道根路径都连不上 — 隧道死了/URL 写错")
        return False
    except Exception as e:
        cprint(RED, f"     ❌ 隧道探测异常: {e}")
        return False

    # 现在带参数 GET — 期望 200(成功) 或 403(签名错),绝不能 404
    ts = str(int(time.time()))
    nonce = f"diag_{ts}"
    echo = "diagnostic_echo"
    r = requests.get(
        callback_url,
        params={"msg_signature": "0" * 40, "timestamp": ts, "nonce": nonce, "echostr": echo},
        timeout=TIMEOUT,
    )
    if r.status_code == 404:
        cprint(RED, f"     ❌ 404 — 隧道没把请求转到 FastAPI,或路由不存在")
        cprint(YELLOW, f"     响应内容(前 200 字符): {r.text[:200]!r}")
        cprint(YELLOW, "     💡 常见原因:")
        cprint(YELLOW, "       - ngrok 域名已过期(重启 ngrok 会换域名)")
        cprint(YELLOW, "       - 隧道指向了别的端口(比如 8001)")
        cprint(YELLOW, "       - FastAPI 启动失败但没看到日志")
        return False
    if r.status_code in (200, 403):
        cprint(GREEN, f"     ✅ 路由可达 ({r.status_code}) — 隧道和 FastAPI 都活着")
        return True
    cprint(YELLOW, f"     ⚠️  意外状态码: {r.status_code}")
    cprint(YELLOW, f"     响应: {r.text[:200]!r}")
    return True


def check_signature_verify(callback_url: str, token: str) -> bool:
    """L3: 用 .env 里的真实 Token 跑一遍签名验证 — 模拟企业微信后台的 GET"""
    cprint(CYAN, "\n[L3] 模拟企业微信 URL 验证握手")
    if not token:
        cprint(RED, "     ❌ .env 缺少 WECHAT_KF_TOKEN,跳过")
        return False
    cprint(CYAN, f"     Token: {token[:4]}{'*' * (len(token) - 8)}{token[-4:]} (共 {len(token)} 字符)")

    ts = str(int(time.time()))
    nonce = f"diag_{ts}"
    echo = "diagnostic_echostring_123"
    # 企业微信 URL 验证签名: SHA1(sort([token, timestamp, nonce, echostr]))
    parts = sorted([token, ts, nonce, echo])
    signature = hashlib.sha1("".join(parts).encode("utf-8")).hexdigest()

    r = requests.get(
        callback_url,
        params={"msg_signature": signature, "timestamp": ts, "nonce": nonce, "echostr": echo},
        timeout=TIMEOUT,
    )
    cprint(CYAN, f"     状态: {r.status_code}")
    cprint(CYAN, f"     响应: {r.text[:120]!r}")

    if r.status_code == 200 and r.text.strip() == echo:
        cprint(GREEN, "     ✅ 签名验证 + 解密全通过 — 可以直接去企业微信后台保存配置")
        return True
    if r.status_code == 200:
        cprint(YELLOW, "     ⚠️  200 但内容不匹配 — 可能是 EncodingAESKey 配错,微信后台存的不是 .env 里的这个")
        return False
    if r.status_code == 403:
        cprint(YELLOW, "     ⚠️  403 — 签名/Token 不匹配")
        cprint(YELLOW, "     💡 检查企业微信后台填的 Token 跟 .env 里的 WECHAT_KF_TOKEN 是否一字不差")
        return False
    cprint(RED, f"     ❌ 失败: {r.status_code}")
    return False


def main() -> int:
    cprint(CYAN, "=" * 64)
    cprint(CYAN, "企业微信客服回调链路诊断")
    cprint(CYAN, "=" * 64)

    env = read_env()
    override = sys.argv[1] if len(sys.argv) > 1 else None
    callback_url = get_callback_url(env, override)
    cprint(CYAN, f"使用回调 URL: {callback_url}")
    cprint(CYAN, f"来源: {'命令行参数' if override else '.env (WECHAT_CALLBACK_BASE_URL)'}")

    l1 = check_local_service()
    l2 = check_route_exists(callback_url) if l1 else False
    l3 = check_signature_verify(callback_url, env.get("WECHAT_KF_TOKEN", "")) if l2 else False

    cprint(CYAN, "\n" + "=" * 64)
    cprint(CYAN, "诊断结果")
    cprint(CYAN, "=" * 64)
    cprint(GREEN if l1 else RED, f"  L1 本地服务:     {'✅ 正常' if l1 else '❌ 失败'}")
    cprint(GREEN if l2 else RED, f"  L2 隧道打通:     {'✅ 正常' if l2 else '❌ 失败'}")
    cprint(GREEN if l3 else RED, f"  L3 签名握手:     {'✅ 正常' if l3 else '❌ 失败'}")

    if l1 and l2 and l3:
        cprint(GREEN, "\n🎉 全部通过!现在可以去企业微信后台保存回调配置:")
        cprint(GREEN, f"   URL:    {callback_url}")
        cprint(GREEN, f"   Token:  {env.get('WECHAT_KF_TOKEN', '(未配置)')}")
        cprint(GREEN, f"   AESKey: {env.get('WECHAT_ENCODING_AES_KEY', '(未配置)')}")
        return 0

    cprint(YELLOW, "\n🔧 修复指引:")
    if not l1:
        cprint(YELLOW, "  - L1: 启动本地服务 → python run.py")
    if not l2:
        cprint(YELLOW, "  - L2: 检查隧道(ngrok / vicp.fun / frp)是否还在跑")
        cprint(YELLOW, "         ngrok 重启后域名会变,要同步更新 .env 的 WECHAT_CALLBACK_BASE_URL")
        cprint(YELLOW, "         免费 ngrok 域名有效期通常只有几小时")
    if not l3:
        cprint(YELLOW, "  - L3: 确认 .env 的 WECHAT_KF_TOKEN / WECHAT_ENCODING_AES_KEY")
        cprint(YELLOW, "         跟企业微信后台填的完全一致(注意空格、大小写)")
    return 1


if __name__ == "__main__":
    sys.exit(main())
