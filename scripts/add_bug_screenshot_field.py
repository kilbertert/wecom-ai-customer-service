#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一次性 (自包含): 在飞书生产 bug 表新增 "Bug截图" 附件字段 (type=17)。

自包含: 直接读 .env 的 BUGTRACK_FEISHU_* 凭据 + 原始 HTTP 调飞书,
不依赖 prod app 代码 (prod/dev 已分歧, 避免依赖 prod feishu_bitable 模块状态)。

用户 bug 截图需内联写入飞书记录, 文本字段 (截图1/2/3, type=1) 无法渲染图片,
故新增附件字段。附件字段值 [{"file_token": "..."}], file_token 由
feishu_bitable.upload_attachment 上传得到 (见 /internal/bugtrack/add)。

用法 (在生产 wecom 服务器, .env 已配 BUGTRACK_FEISHU_* 生产凭据):
    cd <wecom-repo-root>
    python3 add_bug_screenshot_field.py [.env路径]

幂等: 已存在 "Bug截图" 字段则跳过。
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request
import urllib.error

_BASE = "https://open.feishu.cn/open-apis"
FIELD_NAME = "Bug截图"
FIELD_TYPE = 17  # 飞书附件类型


def load_env(env_path: str) -> dict:
    env = {}
    if os.path.exists(env_path):
        for line in open(env_path, encoding="utf-8"):
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip("'\"")
    # 环境变量优先
    for k in ("BUGTRACK_FEISHU_APP_ID", "BUGTRACK_FEISHU_APP_SECRET",
              "BUGTRACK_FEISHU_APP_TOKEN", "BUGTRACK_FEISHU_TABLE_ID"):
        if os.environ.get(k):
            env[k] = os.environ[k]
    return env


def get_token(app_id: str, app_secret: str) -> str:
    body = json.dumps({"app_id": app_id, "app_secret": app_secret}).encode()
    r = urllib.request.Request(
        f"{_BASE}/auth/v3/tenant_access_token/internal",
        data=body, method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(r, timeout=20) as resp:
        data = json.loads(resp.read())
    if data.get("code") != 0:
        raise RuntimeError(f"获取 token 失败: {data}")
    return data["tenant_access_token"]


def api(method: str, url: str, token: str, body=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(
        url, data=data, method=method,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(r, timeout=20) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return json.loads(e.read().decode())


def main() -> int:
    env_path = sys.argv[1] if len(sys.argv) > 1 else ".env"
    env = load_env(env_path)
    app_id = env.get("BUGTRACK_FEISHU_APP_ID", "")
    app_secret = env.get("BUGTRACK_FEISHU_APP_SECRET", "")
    app_token = env.get("BUGTRACK_FEISHU_APP_TOKEN", "")
    table_id = env.get("BUGTRACK_FEISHU_TABLE_ID", "")
    if not (app_id and app_secret and app_token and table_id):
        print(f"[FAIL] 飞书配置不全 (from {env_path}): "
              f"APP_ID={'Y' if app_id else 'N'} SECRET={'Y' if app_secret else 'N'} "
              f"APP_TOKEN={'Y' if app_token else 'N'} TABLE_ID={'Y' if table_id else 'N'}")
        return 1

    print(f"[INFO] 目标表 app_token={app_token} table_id={table_id}")
    token = get_token(app_id, app_secret)
    print("[INFO] tenant_access_token 获取成功")

    # 1) 列现有字段 (幂等检查)
    fields_url = f"{_BASE}/bitable/v1/apps/{app_token}/tables/{table_id}/fields"
    resp = api("GET", fields_url + "?page_size=100", token)
    if resp.get("code") != 0:
        print(f"[FAIL] 列字段失败: {resp}")
        return 1
    fields = (resp.get("data") or {}).get("items") or []
    print(f"[INFO] 现有 {len(fields)} 字段:")
    for f in fields:
        print(f"  - {f.get('field_name')} | type={f.get('type')} | id={f.get('field_id')}")

    existing = next((f for f in fields if f.get("field_name") == FIELD_NAME), None)
    if existing:
        print(f"[OK] 字段 '{FIELD_NAME}' 已存在 (type={existing.get('type')}, "
              f"id={existing.get('field_id')}), 跳过")
        return 0

    # 2) 建附件字段
    resp = api("POST", fields_url, token,
               {"field_name": FIELD_NAME, "type": FIELD_TYPE})
    if resp.get("code") != 0:
        print(f"[FAIL] 建字段失败: {resp}")
        msg = str(resp)
        if "permission" in msg.lower() or "99991672" in msg or "1254003" in msg:
            print("[HINT] 可能权限不足, 飞书后台->应用->权限管理 开通 bitable:app / drive:drive:write")
        return 1
    field = (resp.get("data") or {}).get("field") or {}
    print(f"[OK] 字段 '{FIELD_NAME}' 建立成功 id={field.get('field_id')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
