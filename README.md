# 微信客服接入 AI 智能体服务（Coze / Dify 可切换）

把企业微信客服收到的消息转发给 AI 智能体（**Coze** 工作流或 **Dify** 工作流，二选一），再把 AI 的回复通过企业微信客服消息接口回给用户。

> 单一对话轮次模式：每条用户消息独立调用一次 AI 工作流，不维护会话状态（不依赖 Redis），适合客服问答、单轮任务型场景。

---

## 目录

- [快速开始](#快速开始)
- [运行](#运行)
- [测试](#测试)
- [AI 后端切换（Coze / Dify 并存）](#ai-后端切换coze--dify-并存)
- [环境变量参考](#环境变量参考)
- [部署](#部署)
- [API 端点](#api-端点)
- [项目结构](#项目结构)
- [常见问题](#常见问题)
- [架构概览](#架构概览)

---

## 快速开始

### 环境要求

- Python 3.11+（推荐 3.13）
- Windows / macOS / Linux
- 联网（安装依赖 + 调用企业微信 / AI 平台 API）

### 1. 克隆代码 & 创建虚拟环境

```bash
git clone <your-repo-url> wecom-ai-customer-service
cd wecom-ai-customer-service

# 创建虚拟环境
python -m venv .venv

# 激活（Windows Git Bash）
source .venv/Scripts/activate

# 激活（Windows PowerShell）
.venv\Scripts\Activate.ps1

# 激活（macOS / Linux）
source .venv/bin/activate
```

### 2. 安装依赖

```bash
pip install --upgrade pip
pip install -r requirements.txt
```

> 国内网络可临时加镜像：
> `pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple`

### 3. 准备 `.env`

```bash
# Linux / macOS / Git Bash
cp env.example .env

# Windows PowerShell
Copy-Item env.example .env
```

按需修改 `.env`：

- **必需**：填 `WECHAT_*`（企业微信凭据）和 `COZE_*` 或 `DIFY_*`（AI 凭据，二选一）
- **切换后端**：`APP_AI_BACKEND=coze` 或 `APP_AI_BACKEND=dify`
- 详细变量含义见 [环境变量参考](#环境变量参考)

### 4. 启动服务

```bash
python run.py
# 或
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

打开浏览器访问：

- 服务根：`http://localhost:8000/`
- 健康检查：`http://localhost:8000/health`
- API 文档（Swagger）：`http://localhost:8000/docs`

---

## 运行

### 本地开发

```bash
# 默认端口 8000
python run.py
```

或显式指定：

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

### 生产部署

详见 [部署](#部署) 一节。推荐使用 `deploy.sh`（Linux）或 Docker。

### Docker

```bash
docker build -t wecom-ai-service .
docker run -d --name wecom-ai -p 8000:8000 --env-file .env wecom-ai-service
```

---

## 测试

### 一键运行全部测试

```bash
pytest
```

### 单文件 + 详细输出

```bash
pytest tests/test_dify_service.py -v
```

### 带覆盖率

```bash
pytest --cov=app --cov-report=term-missing
```

### 标记筛选

```bash
# 只跑 Dify 相关
pytest -k dify

# 跳过大模型调用相关的慢测试
pytest -m "not slow"
```

测试框架使用 `pytest` + `pytest-asyncio`（`asyncio_mode = auto`，无需手动加 `@pytest.mark.asyncio`）。

---

## AI 后端切换（Coze / Dify 并存）

服务内置**两套 AI 客户端**——`CozeService` 和 `DifyService`，对外接口完全一致（`upload_file()` + `run_workflow()`），所以**业务层 `WeChatService` 不需要改一行代码**。通过环境变量 `APP_AI_BACKEND` 切换：

| 后端 | 适用场景 | 速度 | 多模态 |
| --- | --- | --- | --- |
| **Coze** | 字节豆包生态、工作流可视化编排 | 较快 | 文本 + 图片 + 语音 |
| **Dify** | 自托管 / 私有化、灵活 LLM 编排、Coze 不够用时 | 较慢 | 文本 + 图片 + 语音 |

### 切换步骤

1. **修改 `.env`**：把 `APP_AI_BACKEND=dify`（或保留 `coze`）
2. **填对应凭据**：
   - 切 Dify：填 `DIFY_API_BASE` / `DIFY_API_KEY` / `DIFY_INPUT_*` / `DIFY_OUTPUT_TEXT`
   - 切 Coze：填 `COZE_API_TOKEN` / `COZE_WORKFLOW_ID`
3. **重启服务**。

### 字段差异

| 维度 | Coze | Dify |
| --- | --- | --- |
| 凭据 | `COZE_API_TOKEN` (PAT) | `DIFY_API_KEY` (app-xxx) |
| 工作流 ID | `COZE_WORKFLOW_ID` | Dify 后台 URL 末尾的 `workflow_id`（已在 DIFY 内部按 workflow 配置） |
| 输入变量 | 隐式（按 `input_data` 字段名） | 显式：`DIFY_INPUT_TEXT` / `DIFY_INPUT_IMAGE` / `DIFY_INPUT_AUDIO` |
| 输出变量 | SDK 自动取 | 显式：`DIFY_OUTPUT_TEXT`（默认 `output`） |
| 文件格式 | SDK 封装 | `[{"type": "image", "transfer_method": "local_file", "upload_file_id": "uuid"}]` |
| 用户标识 | 入参 `user_id` | 强制 `end_user` 字段（自动用 `external_userid` 注入） |
| 失败语义 | HTTP 4xx 抛异常 | HTTP 200 + `data.status="failed"` 也算失败（已处理） |

### 接口同形性保证

`WeChatService.process_single_message` 的签名是：

```python
async def process_single_message(
    self,
    message: WeChatMessage,
    ai_service: "AIService",   # CozeService | DifyService
) -> None:
    ...
```

工厂函数 `get_ai_service()` 根据 `APP_AI_BACKEND` 返回对应实例，路由层 `app/routes/wechat.py` 在每个请求里调用一次工厂——**两种后端走完全相同的消息处理路径**。

---

## 环境变量参考

完整列表见 `env.example`。按分组说明：

### 微信客服（必需）

| 变量 | 必填 | 说明 |
| --- | :-: | --- |
| `WECHAT_CORP_ID` | ✅ | 企业 ID，企微管理后台 → 「我的企业」 |
| `WECHAT_CORP_SECRET` | ✅ | 应用 Secret，企微管理后台 → 「应用管理 → 自建」 |
| `WECHAT_KF_TOKEN` | ✅ | 回调 Token，企业微信客服「开发配置」里自定义 |
| `WECHAT_ENCODING_AES_KEY` | ✅ | 回调 EncodingAESKey（43 字符 Base64），同上手动生成 |
| `WECHAT_CALLBACK_BASE_URL` | ✅ | 公网可访问的回调根 URL，如 `https://abc.example.com` |
| `WECHAT_ALLOWED_OPEN_KFID` | ❌ | 只处理指定客服 ID 的消息，留空 = 全部 |

### Coze 后端（`APP_AI_BACKEND=coze` 时必需）

| 变量 | 必填 | 说明 |
| --- | :-: | --- |
| `COZE_API_TOKEN` | ✅ | Coze 个人访问令牌（PAT），[coze.com](https://www.coze.com) → 头像 → API Tokens |
| `COZE_WORKFLOW_ID` | ✅ | 工作流 ID，发布后从 URL 复制 |
| `COZE_API_BASE_URL` | ❌ | 默认 `https://api.coze.com`（国内版需改为 `https://api.coze.cn`） |

### Dify 后端（`APP_AI_BACKEND=dify` 时必需）

| 变量 | 必填 | 默认 | 说明 |
| --- | :-: | --- | --- |
| `DIFY_API_BASE` | ✅ | — | `https://api.dify.ai/v1`（海外） / 自托管 URL |
| `DIFY_API_KEY` | ✅ | — | Dify 工作流「API 访问」页签里的 `app-xxx` |
| `DIFY_INPUT_TEXT` | ❌ | `input_text` | 工作流开始节点的文本变量名 |
| `DIFY_INPUT_IMAGE` | ❌ | `input_img_id` | 图片输入变量名（必须是文件数组） |
| `DIFY_INPUT_AUDIO` | ❌ | `input_audio_id` | 语音输入变量名（必须是文件数组） |
| `DIFY_OUTPUT_TEXT` | ❌ | `output` | 工作流结束节点的文本输出变量名 |
| `DIFY_END_USER_DEFAULT` | ❌ | `wechat-default-user` | 兜底 `end-user`，WeChat 场景会用 `external_userid` 覆盖 |
| `DIFY_WORKFLOW_TIMEOUT` | ❌ | `120` | workflow / upload 超时秒数 |

### 应用配置

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `APP_SECRET_KEY` | `your_secret_key_here` | 应用内部签名密钥（生产请改为随机字符串） |
| `APP_DEBUG` | `false` | 调试模式 |
| `APP_HOST` | `0.0.0.0` | 监听地址 |
| `APP_PORT` | `8000` | 监听端口 |
| `APP_AI_BACKEND` | `coze` | AI 后端选择：`coze` / `dify` |

---

## 部署

### Linux 一键脚本

```bash
chmod +x deploy.sh
./deploy.sh
```

`deploy.sh` 默认使用 `nohup` + `uvicorn` 后台启动，日志输出到 `logs/`。

### Systemd 服务（推荐生产）

```ini
# /etc/systemd/system/wecom-ai.service
[Unit]
Description=Wecom AI Customer Service
After=network.target

[Service]
Type=simple
User=www-data
WorkingDirectory=/opt/wecom-ai-customer-service
EnvironmentFile=/opt/wecom-ai-customer-service/.env
ExecStart=/opt/wecom-ai-customer-service/.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now wecom-ai
sudo systemctl status wecom-ai
```

### Docker

```bash
docker build -t wecom-ai-service .
docker run -d \
  --name wecom-ai \
  --restart unless-stopped \
  -p 8000:8000 \
  --env-file .env \
  wecom-ai-service
```

### 回调地址要求

企业微信要求回调 URL 必须是 **公网 HTTPS**。本地调试推荐：

- [frp](https://github.com/fatedier/frp) / [ngrok](https://ngrok.com) / [cpolar](https://www.cpolar.com) 做内网穿透
- 把穿透得到的 HTTPS 域名填到 `WECHAT_CALLBACK_BASE_URL`，回调路径是 `/wechat/kf/callback`

---

## API 端点

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/` | 服务元信息（name / version / AI backend） |
| `GET` | `/health` | 健康检查 |
| `GET` | `/wechat/kf/callback` | 企业微信客服回调 URL 验证（echostr 校验） |
| `POST` | `/wechat/kf/callback` | 接收加密消息，丢到 BackgroundTasks 后立即 ACK |
| `GET` | `/docs` | Swagger UI |
| `GET` | `/redoc` | ReDoc |

详细请求/响应模型见 `/docs`。

---

## 项目结构

```
wecom-ai-customer-service/
├── app/
│   ├── main.py                 # FastAPI 应用工厂 + lifespan
│   ├── core/
│   │   ├── config.py           # pydantic-settings 配置 (Coze / Dify / 微信 / 应用)
│   │   └── exceptions.py       # AIBackendError (原 CozeAPIError,保留别名)
│   ├── routes/
│   │   └── wechat.py           # /wechat/kf/callback 路由
│   ├── services/
│   │   ├── __init__.py         # get_ai_service() 工厂 + AIService 类型别名
│   │   ├── wechat.py           # WeChatService 业务编排
│   │   ├── coze.py             # CozeService (原有)
│   │   ├── dify.py             # DifyService (新增,与 CozeService 同形)
│   │   ├── dify_client.py      # Dify HTTP 客户端 (upload_file / run_workflow)
│   │   ├── response_parser.py  # 通用回复文本提取 (含 <think> 块剥离)
│   │   └── media.py            # 媒体处理 (ffmpeg 转码 amr→wav 等)
│   ├── models/                 # Pydantic 数据模型
│   └── utils/                  # 工具函数
├── tests/
│   ├── test_dify_service.py    # DifyService 单测 (mock DifyClient, 27 用例)
│   └── ...
├── docs/                       # 设计/开发文档
├── logs/                       # 运行日志
├── env.example                 # 环境变量模板
├── requirements.txt
├── pytest.ini                  # asyncio_mode = auto
├── run.py                      # 本地启动入口
├── deploy.sh                   # Linux 部署脚本
├── Dockerfile
├── CLAUDE.md                   # Claude Code 项目说明
└── README.md                   # 本文件
```

---

## 常见问题

### 1. `WECHAT_ENCODING_AES_KEY` 报错 / 解密失败

企业微信要求 EncodingAESKey 是 **43 字符的 Base64 串**。在企业微信客服「开发配置」页点「随机生成」即可，不要自己拼。

### 2. 回调 401 / 加密签名错误

检查 `WECHAT_KF_TOKEN` 和「回调配置」页里的 Token 完全一致（区分大小写、不带空格）。

### 3. 语音消息（`.amr`）处理失败

需要系统装 ffmpeg：

```bash
# macOS
brew install ffmpeg

# Ubuntu / Debian
sudo apt install ffmpeg

# Windows
# https://www.gyan.dev/ffmpeg/builds/ 下载并加 PATH
```

代码里通过 `pydub` 调用 ffmpeg。

### 4. 切到 Dify 后 401 Unauthorized

Dify API Key 形如 `app-xxxxxxxx`，**不是** `app-` 开头的会被网关拒；在 Dify 工作流「API 访问」页直接复制完整 Key。

### 5. 切到 Dify 后 workflow 一直 504

默认超时 120s。如 Dify 工作流确实更慢，调整 `DIFY_WORKFLOW_TIMEOUT=300`（Dify 走 `blocking` 模式同步等待，可能要 1-3 分钟）。

### 6. `ModuleNotFoundError: No module named 'pydantic_core'`

Python 3.13 上 pydantic < 2.6 没有预编译 wheel。请确保用 `requirements.txt` 里的版本（`pydantic>=2.7,<3`）。

### 7. pytest 跑起来 25+ 个测试全 skipped

需要 `pytest.ini` 里有 `asyncio_mode = auto`（已默认提供），或每个 async 测试加 `@pytest.mark.asyncio`。

### 8. 日志里出现 `AIBackendError`

异常已经从旧名 `CozeAPIError` 重命名为 `AIBackendError`（更通用）。`CozeAPIError` 仍作为别名保留以兼容旧 import，不会破坏你的自定义 try/except。

---

## 架构概览

```
┌──────────────┐         ┌──────────────────────────────────────┐
│  微信用户     │  ─────► │  POST /wechat/kf/callback            │
│  (WeChat KF) │         │  ├─ AES 解密 + 签名校验               │
└──────────────┘         │  ├─ ACK (200 OK, 空串)                │
                         │  └─ BackgroundTasks                  │
                         │      └─ WeChatService.process_…       │
                         │           ├─ 文本: 直接调用 workflow  │
                         │           ├─ 图片: download→upload    │
                         │           └─ 语音: download→amr→wav   │
                         │                  │                   │
                         │                  ▼                   │
                         │  ┌──────────────────────────────┐    │
                         │  │ get_ai_service() 工厂         │    │
                         │  │   APP_AI_BACKEND=coze →     │    │
                         │  │     CozeService.run_workflow │    │
                         │  │   APP_AI_BACKEND=dify →     │    │
                         │  │     DifyService.run_workflow │    │
                         │  └──────────────────────────────┘    │
                         │           │                          │
                         │           ▼                          │
                         │  ┌──────────────────────────────┐    │
                         │  │  提取回复文本 + <think> 剥离   │    │
                         │  │  ─► 调企业微信客服"发送消息"   │    │
                         │  └──────────────────────────────┘    │
                         └──────────────────────────────────────┘
```

消息处理是**单轮**的：每条用户消息 → 一次 AI 调用 → 一次回复，无会话上下文累积。

---

## License

内部项目，按公司规定执行。
