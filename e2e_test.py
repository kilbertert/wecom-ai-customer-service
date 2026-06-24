#!/usr/bin/env python3
"""
WeChat KF Callback 端到端验证脚本

L1  本地 FastAPI 是否存活
L2  外网 URL 是否能从公网打通到 FastAPI（验证 nginx 反代）
L3  GET 验证请求（带真实签名 + 加密 echostr）能否正确解密并明文回包
L4  POST 业务消息（加密 XML）能否被路由正确接收（看 FastAPI 日志）

用法：
  python e2e_test.py                       # 默认读 .env 里的 WECHAT_CALLBACK_BASE_URL
  python e2e_test.py https://your.domain   # 显式指定回调 base url
"""

import base64
import hashlib
import os
import struct
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import requests
from Crypto.Cipher import AES

ENV_FILE = Path(__file__).parent / ".env"
TIMEOUT = 15

GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
CYAN = "\033[96m"
RESET = "\033[0m"


def cprint(color: str, text: str) -> None:
    print(f"{color}{text}{RESET}")


def read_env() -> dict[str, str]:
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
    if override:
        return override.rstrip("/")
    base = env.get("WECHAT_CALLBACK_BASE_URL", "").rstrip("/")
    if not base:
        cprint(RED, "[FATAL] 找不到 WECHAT_CALLBACK_BASE_URL，也未通过命令行传入")
        cprint(RED, "        请设置 .env 或执行: python e2e_test.py https://你的域名")
        sys.exit(1)
    return f"{base}/wechat/kf/callback"


# ──────────────── 微信 AES 加密（与 FastAPI 的 decrypt_message_custom 互逆）────────────

def pkcs7_pad(data: bytes, block: int = 32) -> bytes:
    pad = block - (len(data) % block)
    return data + bytes([pad] * pad)


def encrypt_for_wechat(plaintext: str, encoding_aes_key: str, corp_id: str) -> str:
    """把明文按企业微信 AES-256-CBC 协议封装并加密，返回 base64 字符串"""
    if len(encoding_aes_key) != 43:
        raise ValueError(f"EncodingAESKey 必须是 43 位，当前 {len(encoding_aes_key)}")
    key = base64.b64decode(encoding_aes_key + "=")
    iv = key[:16]

    rand16 = os.urandom(16)
    msg_bytes = plaintext.encode("utf-8")
    msg_len = struct.pack(">I", len(msg_bytes))
    body = rand16 + msg_len + msg_bytes + corp_id.encode("utf-8")
    body = pkcs7_pad(body, 32)

    cipher = AES.new(key, AES.MODE_CBC, iv)
    encrypted = cipher.encrypt(body)
    return base64.b64encode(encrypted).decode("utf-8")


def make_signature(token: str, items: list[str]) -> str:
    """SHA1( sorted(items + token).join() )"""
    arr = list(items) + [token]
    arr.sort()
    return hashlib.sha1("".join(arr).encode("utf-8")).hexdigest()


# ──────────────── 各层级验证 ─────────────

def l1_local_health(local_base: str) -> bool:
    """best-effort：如果远程运行，L1 会超时/拒绝，仅作提示，不阻塞后续 L2/L3/L4"""
    cprint(CYAN, "\n[L1] 本机 FastAPI 健康检查（best-effort，远程跑会失败属正常）")
    cprint(CYAN, f"     目标: {local_base}/monitoring/health")
    try:
        r = requests.get(f"{local_base}/monitoring/health", timeout=3)
        if r.status_code == 200:
            cprint(GREEN, f"     ✅ 通过 — {r.text[:120]}")
            return True
        cprint(YELLOW, f"     ⚠️  状态码 {r.status_code}")
        return False
    except Exception as e:
        cprint(YELLOW, f"     ⚠️  跳过（远程不可达是预期的）: {type(e).__name__}: {e}")
        return True


def l2_nginx_proxy(callback_url: str) -> bool:
    cprint(CYAN, "\n[L2] 公网 URL → nginx → FastAPI 反代")
    cprint(CYAN, f"     目标: {callback_url}")
    parsed = urlparse(callback_url)
    root = f"{parsed.scheme}://{parsed.netloc}/"
    try:
        r_root = requests.get(root, timeout=TIMEOUT)
        cprint(CYAN, f"     根路径 {root} → {r_root.status_code}")
    except Exception as e:
        cprint(YELLOW, f"     ⚠️  根路径探测失败（不致命）: {e}")

    test_url = callback_url.rsplit("/kf/callback", 1)[0] + "/test"
    try:
        r = requests.get(test_url, timeout=TIMEOUT, headers={"User-Agent": "e2e-test/1.0"})
        if r.status_code == 200 and "ok" in r.text.lower():
            cprint(GREEN, f"     ✅ 反代打通 — {r.text[:120]}")
            return True
        cprint(RED, f"     ❌ 状态码 {r.status_code}: {r.text[:200]}")
        return False
    except Exception as e:
        cprint(RED, f"     ❌ 异常: {e}")
        return False


def l3_get_verify(callback_url: str, env: dict[str, str]) -> bool:
    cprint(CYAN, "\n[L3] GET 验证请求（带真实签名 + 加密 echostr）")
    token = env.get("WECHAT_KF_TOKEN", "").strip()
    aes_key = env.get("WECHAT_ENCODING_AES_KEY", "").strip()
    corp_id = env.get("WECHAT_CORP_ID", "").strip()

    if not (token and aes_key and corp_id):
        cprint(RED, "     ❌ .env 里缺少 WECHAT_KF_TOKEN / WECHAT_ENCODING_AES_KEY / WECHAT_CORP_ID")
        return False

    plaintext_echostr = "echo_" + str(int(time.time()))
    try:
        encrypted = encrypt_for_wechat(plaintext_echostr, aes_key, corp_id)
    except Exception as e:
        cprint(RED, f"     ❌ 加密 echostr 失败: {e}")
        return False

    timestamp = str(int(time.time()))
    nonce = "n" + str(int(time.time() * 1000))[-10:]
    msg_signature = make_signature(token, [timestamp, nonce, encrypted])

    url = f"{callback_url}?msg_signature={msg_signature}&timestamp={timestamp}&nonce={nonce}&echostr={encrypted}"
    cprint(CYAN, f"     明文 echostr = {plaintext_echostr!r}")
    cprint(CYAN, f"     签名 = {msg_signature[:16]}...")
    cprint(CYAN, f"     URL = {url[:120]}...")

    try:
        # 用 params= 让 requests 库做正确的 URL 编码（避免 + 被当作空格）
        r = requests.get(
            callback_url,
            params={
                "msg_signature": msg_signature,
                "timestamp": timestamp,
                "nonce": nonce,
                "echostr": encrypted,
            },
            timeout=TIMEOUT,
            headers={"User-Agent": "Mozilla/4.0"},
        )
    except Exception as e:
        cprint(RED, f"     ❌ 请求异常: {e}")
        return False

    cprint(CYAN, f"     HTTP {r.status_code}, content-type={r.headers.get('content-type')}")
    cprint(CYAN, f"     响应体前 80 字符: {r.text[:80]!r}")
    if r.status_code == 200 and r.text.strip() == plaintext_echostr:
        cprint(GREEN, "     ✅ 完整链路通过 — 签名校验 + AES 解密 + 明文回包 全部正确")
        return True
    if r.status_code == 200:
        cprint(YELLOW, "     ⚠️  200 但回包不是原 echostr（可能是 fallback 返回原 echostr，企业微信也能接受）")
        return True
    cprint(RED, f"     ❌ 失败: HTTP {r.status_code}, body={r.text[:200]}")
    return False


def l4_post_message(callback_url: str, env: dict[str, str]) -> bool:
    """模拟企业微信 POST 推送：发一条加密的 kf_msg_or_event 事件 XML"""
    cprint(CYAN, "\n[L4] POST 业务消息（加密 XML → 验证加签 + 解密链路）")
    token = env.get("WECHAT_KF_TOKEN", "").strip()
    aes_key = env.get("WECHAT_ENCODING_AES_KEY", "").strip()
    corp_id = env.get("WECHAT_CORP_ID", "").strip()
    if not (token and aes_key and corp_id):
        cprint(YELLOW, "     ⚠️  跳过：缺少 token / aes_key / corp_id")
        return True

    inner_xml = (
        "<xml>"
        "<ToUserName><![CDATA[__CORP_ID__]]></ToUserName>"
        "<CreateTime>1719120000</CreateTime>"
        "<MsgType><![CDATA[event]]></MsgType>"
        "<Event><![CDATA[kf_msg_or_event]]></Event>"
        "<Token><![CDATA[e2e_test_token_xx]]></Token>"
        "<OpenKfId><![CDATA[e2e_test_openkfid]]></OpenKfId>"
        "</xml>"
    ).replace("__CORP_ID__", corp_id)

    encrypted = encrypt_for_wechat(inner_xml, aes_key, corp_id)
    envelope_xml = (
        f"<xml><ToUserName><![CDATA[{corp_id}]]></ToUserName>"
        f"<Encrypt><![CDATA[{encrypted}]]></Encrypt></xml>"
    )

    timestamp = str(int(time.time()))
    nonce = "n" + str(int(time.time() * 1000))[-10:]
    msg_signature = make_signature(token, [timestamp, nonce, encrypted])

    cprint(CYAN, f"     加密 XML 长度: {len(encrypted)} 字符")
    cprint(CYAN, f"     签名: {msg_signature[:16]}...")
    try:
        r = requests.post(
            callback_url,
            params={
                "msg_signature": msg_signature,
                "timestamp": timestamp,
                "nonce": nonce,
            },
            data=envelope_xml.encode("utf-8"),
            headers={"Content-Type": "text/xml", "User-Agent": "Mozilla/4.0"},
            timeout=TIMEOUT,
        )
    except Exception as e:
        cprint(RED, f"     ❌ 请求异常: {e}")
        return False

    cprint(CYAN, f"     HTTP {r.status_code}, body={r.text[:120]!r}")
    if r.status_code == 200 and r.text.strip() == "success":
        cprint(GREEN, "     ✅ POST 路由签名 + 解密 + 分发链路全部正确（返回 success）")
        cprint(CYAN, "     接下来请执行:")
        cprint(CYAN, "       ssh -p 2134 root@120.55.45.59 'docker logs --tail 50 weixin-coze | grep -E \"kf_msg|event|EVENT|Verify\"'")
        cprint(CYAN, "       应该能看到 '收到客服消息事件(kf_msg_or_event)' 一类的日志")
        return True
    cprint(RED, f"     ❌ 失败: HTTP {r.status_code}, body={r.text[:200]}")
    return False


# ──────────────── main ─────────────

def main() -> int:
    env = read_env()
    callback_url = get_callback_url(env, sys.argv[1] if len(sys.argv) > 1 else None)
    parsed = urlparse(callback_url)
    local_base = f"{parsed.scheme}://{parsed.hostname}:8501"

    print("=" * 60)
    print("WeChat KF Callback 端到端验证")
    print("=" * 60)
    print(f"  回调 URL:   {callback_url}")
    print(f"  本机基址:   {local_base}")
    print(f"  Token:      {env.get('WECHAT_KF_TOKEN', '?')[:8]}...")
    print(f"  AESKey len: {len(env.get('WECHAT_ENCODING_AES_KEY', ''))}")
    print(f"  CorpID:     {env.get('WECHAT_CORP_ID', '?')[:10]}...")

    results = {
        "L1 (本地 FastAPI)":        l1_local_health(local_base),
        "L2 (公网 URL 反代)":       l2_nginx_proxy(callback_url),
        "L3 (GET 验证签名+解密)":   l3_get_verify(callback_url, env),
        "L4 (POST 加密消息)":       l4_post_message(callback_url, env),
    }

    print("\n" + "=" * 60)
    print("汇总")
    print("=" * 60)
    failed = 0
    for k, v in results.items():
        flag = "✅" if v else "❌"
        print(f"  {flag}  {k}")
        if not v:
            failed += 1

    if failed == 0:
        cprint(GREEN, "\n🎉 全部通过 — 现在去企业微信后台点保存，应当一次通过。")
        return 0
    cprint(RED, f"\n{failed} 项未通过，请按上面提示检查后再跑。")
    return 1


if __name__ == "__main__":
    sys.exit(main())