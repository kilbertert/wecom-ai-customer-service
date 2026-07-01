# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project is

A FastAPI middleware that bridges **WeChat Customer Service (微信客服)** + **企业微信智能机器人** callbacks and a **Coze / Dify** AI backend. The service receives WeChat callbacks (KF XML or bot JSON), decrypts/verifies them via protocol adapters, downloads/standardizes media, calls the AI workflow, and posts the reply back (KF `send_kf` or bot `response_url`).

**Architecture**: `ProtocolAdapter` (KfAdapter / BotAdapter) 剥离协议, `MessageProcessor` 协议无关编排, `ConversationStore` 薄 conversation_id 映射让 Dify chatflow 续接多轮 (非历史会话存储)。AI 后端 Coze/Dify 可经 `APP_AI_BACKEND` 切换; 人工侧由 Chatwoot 集成。

**Current mode**: 本服务无状态, 不持有会话历史 —— 多轮记忆委托给 Dify chatflow (经 conversation_id 续接) 与 Chatwoot (人工侧)。`SessionService` 与 Redis-backed 历史会话存储是不同概念, 后者仍被禁用 (do not reintroduce unless explicitly asked)。

## Common commands

> All `git`, `pytest`, `ls`, etc. commands are auto-rewritten by the rtk hook to save tokens; run them as you normally would.

### Run

```bash
# Dev (auto-reload)
python run.py                                  # → http://localhost:8000

# Interactive test menu (callback-only server on :8001, or full on :8000)
python run_test.py

# Standalone callback test server (no business logic)
python test_wechat_callback.py
python test_wechat_simple.py
```

### Test

```bash
# Unit + integration tests
pytest

# With coverage
pytest --cov=app --cov-report=html
```

Tests live in `tests/`. `test_main.py` covers root/info/health. `test_flow.py` covers the full WeChat→Coze flow with mocked services.

### Lint / format

```bash
black .            # formatter
isort .            # import order
flake8 .           # lint
mypy .             # type checking (best-effort; not all modules are fully typed)
```

### Deploy

```bash
# One-shot Docker deploy (builds, validates .env, starts detached)
cp env.example .env && vim .env   # fill in real secrets; chmod 600 .env
./deploy.sh                       # build + up -d
./deploy.sh logs|status|restart|stop|clean|verify
```

Alternatively: `docker-compose up -d`. The image installs `ffmpeg`/`ffprobe` system-side for `pydub` voice work.

### Health / debug

```bash
curl http://localhost:8000/                          # service info
curl http://localhost:8000/monitoring/health
curl http://localhost:8000/monitoring/health/detailed
curl http://localhost:8000/monitoring/metrics
```

API docs auto-served at `http://localhost:8000/docs`.

## Architecture

```
WeChat KF / 智能机器人 ──POST /wechat/{kf|bot}/callback──▶ FastAPI (薄路由)
                                      │
                                      ▼
                          ProtocolAdapter.receive()   ← KfAdapter / BotAdapter
                            (验签 + AES 解密 + 协议解析)
                            → List[InboundMessage]    (协议无关)
                                      │
                          BackgroundTasks (KF) / asyncio.create_task (bot)
                                      ▼
                          MessageProcessor.process(inbound, adapter)
                            ├─ DedupStore.acquire        (共享去重)
                            ├─ adapter→media 编排        (download + upload)
                            ├─ ConversationStore.get     (薄 conversation_id 映射)
                            ├─ ai.run_workflow(conversation_id=…)
                            ├─ ConversationStore.save    (Dify chatflow 续接)
                            ├─ compose_multimodal_markdown
                            ├─ adapter.send(reply, trace)   (KF: send_kf / bot: POST response_url)
                            └─ ChatwootSyncService.notify_incoming  (仅 KF)
```

核心抽象是 `ProtocolAdapter` (借鉴对标仓库的 Channel Provider 模式): KF 与智能
机器人各自独立凭证、独立适配器, 共享 `DedupStore` 与 `MessageProcessor`。
新增协议只需实现 `ProtocolAdapter`, 无需改编排器。

### Request flow (POST /wechat/kf/callback)

1. `app/routes/wechat.py:wechat_callback_handler` 检查 `User-Agent` 允许 `WeChat`/`Mozilla/4.0`。
2. `KfAdapter.receive(request)` 预解析 XML 取 `Encrypt` → `WeChatService.verify_signature` (SHA1 of `token + timestamp + nonce + Encrypt`, 委托 `wecom_crypto`) → `decrypt_message_custom` (AES, receive_id=corp_id)。
3. 解密后 XML 的 `MsgType=='event' && Event=='kf_msg_or_event'` 触发 pull 式同步: `WeChatService.sync_latest_messages` 分页拉取最新客户消息 (text/image/voice; 其他类型记日志丢弃)。
4. 最新一条归一为 `InboundMessage`, 派发到 `BackgroundTasks(MessageProcessor.process)`; HTTP 立即回 `success` (防 5s 超时)。
5. `MessageProcessor` 编排: 去重 → 媒体下载/上传 → `ConversationStore.get` → `ai.run_workflow(conversation_id=…)` → `ConversationStore.save` → `compose_multimodal_markdown` → `adapter.send` → Chatwoot 同步。

智能机器人 (`POST /wechat/bot/callback`) 同构, 差异: body 是 `{"encrypt":…}` JSON、receive_id=`""`、`BotAdapter.receive` 提取 image/voice/mixed、`BotAdapter.send` POST `response_url` (markdown)、`BotAdapter.build_sync_ack` 返回加密 envelope、`asyncio.create_task` 替代 `BackgroundTasks`。

### Single-round mode — what that means in code

- `app/services/__init__.py` exports `WeChatService`, `CozeService`, `DifyService`, `MediaService` — no `SessionService`。
- **`ConversationStore` 不是 SessionService**: 它只存一个 `(user_id, scope) → dify_conversation_id` 字符串映射, 让 Dify chatflow (`/v1/chat-messages`) 续接多轮; **不存任何消息历史**。会话记忆由 Dify chatflow 侧持有, 人工侧记忆由 Chatwoot 持有。这与 CLAUDE.md 禁止的"Redis-backed 历史会话存储"是不同概念。
  - 默认 `InMemoryConversationStore` (单 worker); `APP_CONVERSATION_STORE=redis` 切 `RedisConversationStore` (多 worker 抗重启)。
  - KF scope = `open_kfid`; bot scope = `"bot"`。
  - `DifyService.run_workflow(input_data, user_id, conversation_id=None)` 透传 `conversation_id`; Coze 忽略该参数 (workflow 无会话概念)。
- `app/core/config.py:Settings` keeps `RedisSettings` and `CelerySettings` classes defined for config compatibility, but they are not wired into the runtime. `monitoring/health/detailed` reports `mode: "single_round_conversation"`.
- `Celery`/`flower`/`prometheus_client`/`sentry-sdk` are pinned in `requirements.txt` but not currently wired in — leave them unless the task is to enable them.

### Module layout (`app/`)

| Path | Responsibility |
|------|----------------|
| `main.py` | FastAPI app, lifespan (注入 wechat/ai/media/dedup_store/conversation_store/message_processor/kf_adapter/bot_adapter 到 app.state), CORS/trusted-host middleware, exception handlers, root + info endpoints. |
| `core/config.py` | `pydantic-settings` with grouped settings: `WeChatSettings`, `CozeSettings`, `DifySettings`, `ChatwootSettings`, `RedisSettings`, `DatabaseSettings`, `CelerySettings`, `AppSettings`. `load_settings()` falls back from `.env` → `env.example` → defaults. |
| `core/exceptions.py` | `WeChatAPIError`, `CozeAPIError`, `AIBackendError`, `SessionError` (kept for compat), `ValidationError`, `BusinessError`, and the matching `handle_*` → `HTTPException` converters. |
| `crypto/wecom_crypto.py` | 纯函数 AES-256-CBC 加解密 + SHA1 签名 (无 flask, 无 print)。`compute_signature`/`verify_signature`/`decrypt_message`/`encrypt_message`。 |
| `protocols/base.py` | `InboundMessage`/`OutboundReply` (frozen dataclass) + `ProtocolAdapter` ABC + `DedupStore` ABC + `InMemoryDedupStore`。 |
| `protocols/kf_adapter.py` | `KfAdapter`: receive (XML 解密+sync_msg) / send (send_kf) / build_sync_ack("success") / verify_url / dedup_ttl=300s。 |
| `protocols/bot_adapter.py` | `BotAdapter`: receive (JSON 解密+image/voice/mixed) / send (POST response_url + trace off/inline/separate) / build_sync_ack (加密 envelope) / verify_url (receive_id="") / dedup_ttl=600s。 |
| `models/wechat.py`, `models/coze.py` | Pydantic models: `WeChatMessage`, `MessageType` (text/image/voice/video/file/location/event), `StandardizedMessage`, `CozeWorkflowOutput`, `ActionType`。 |
| `routes/wechat.py` | 薄分发器 (~170 行): `GET/POST /wechat/kf/callback`, `GET/POST /wechat/bot/callback`, `GET /wechat/test`。无业务逻辑。 |
| `routes/monitoring.py` | `/monitoring/health`, `/health/detailed`, `/metrics`, `/stats`。 |
| `services/wechat.py` | `WeChatService` + `WeChatConfig`. Wraps `wechatpy.enterprise.WeChatClient`/`WeChatCrypto`. Owns access_token, signature verify (委托 wecom_crypto), AES decrypt, message sync, send_kf, download_media, is_event_processed。crypto 方法是 `wecom_crypto` 的薄委托。 |
| `services/message_processor.py` | `MessageProcessor`: 协议无关编排器 (KF+bot 合并)。dedup→媒体→Chatwoot handoff 检查→conversation_id→AI→compose→send→chatwoot notify。bot 9 阶段 trace 门控发射。`_upload_to_dify_file_store` 在此 (bot 媒体上传)。 |
| `services/conversation_store.py` | `ConversationStore` ABC + `InMemory`/`Redis` 实现。薄 conversation_id 映射 (非历史存储)。 |
| `services/trace_extract.py` | `extract_knowledge` / `extract_thinking` 纯函数 (从 Dify outputs 提取 trace 阶段数据)。 |
| `services/coze.py` | `CozeService`. Calls the Coze workflow via `httpx` (`/v1/workflow/stream_run` SSE); also holds a `cozepy.Coze` SDK client. **Note**: `run_workflow()` currently sends `parameters: {}` and ignores `input_data`/`user_id`/`conversation_id` (the deployed Coze workflow takes no inputs); the documented `{text, file_image_id, file_voice_id}` contract is stale for Coze. `DifyService.run_workflow` consumes its inputs faithfully. |
| `services/dify.py` | `DifyService` (workflow / chatflow 双模式, `settings.dify.app_mode` 切换)。chatflow 透传 `conversation_id` 续接多轮。 |
| `services/media.py` | `MediaService`. Downloads temporary media from WeChat (uses `pydub` + `ffmpeg` for voice). Auto-detects `ffmpeg` on Windows. |
| `services/bot_trace.py` | `BotTrace` + `render_trace()`。9 阶段决策日志 (接收/预过滤/去重/上下文/媒体/知识库/思考/AI/推送), 按 `APP_BOT_TRACE_MODE` 渲染。 |
| `tasks/*.py` | Celery task definitions for `wechat`/`coze`/`media`. Unused in current single-round mode but kept for future use. |

### Bot 决策日志 (可选, 智能机器人增强)

`MessageProcessor.process` (bot 路径) 在每个关键阶段调用 `trace.event(stage, status, detail)`, 最终由 `BotAdapter.send` 按 `APP_BOT_TRACE_MODE` 渲染:

- `off` (默认) — 完全不输出, 对现有行为零侵入
- `inline` — 把 trace 作为灰色 markdown 块拼到 AI 回复文本末尾, 单次 POST
- `separate` — 主回复发出后, 再单独 POST 一次 trace 消息; 失败仅 warning, 不影响主消息

环境变量: `APP_BOT_TRACE_MODE` (`off`|`inline`|`separate`), `APP_BOT_TRACE_MAX_LEN` (默认 1500 字符截断上限, 避免超 4KB markdown 限制)。

### Chatwoot handoff (人工接管, 仅 KF)

`MessageProcessor.process` 在调 AI 前调 `ChatwootSyncService.check_handoff(open_kfid, external_userid)`:

- 仅当 `CHATWOOT_ENABLED=true` 且 inbound 有 `open_kfid` (KF 路径) 时检查; bot 路径不检查。
- `handoff=True` (人工 assignee + online) → **跳过 Dify 调用** (不消耗 conversation 轮次), 也不发送 AI 回复 (人工经 Chatwoot→wecom 另一条路径回复)。
- 检查异常 fail-open (默认不接管, 继续调 AI), 不阻塞主流程。
- 这是被动查询 (人工已接管则让出), 不做自动阈值转人工 (用户明确不做 AI 自动 escalation)。

### Configuration

All config is env-driven via `pydantic-settings`. Required keys for a working deployment:

- `WECHAT_CORP_ID`, `WECHAT_CORP_SECRET`, `WECHAT_KF_TOKEN`, `WECHAT_ENCODING_AES_KEY` (43 chars), `WECHAT_CALLBACK_BASE_URL`
- `WECHAT_ALLOWED_OPEN_KFID` (optional) — if set, only that KF account's messages are processed.
- `APP_BOT_TRACE_MODE` (optional) — `off` (默认) | `inline` | `separate`, 智能机器人决策日志开关
- `APP_BOT_TRACE_MAX_LEN` (optional) — inline 模式 trace 块最大字符数 (默认 1500)
- `APP_CONVERSATION_STORE` (optional) — `memory` (默认, 单 worker) | `redis` (多 worker 抗重启), Dify chatflow conversation_id 映射存储
- `APP_AI_BACKEND` (optional) — `coze` (默认) | `dify`, AI 后端切换
- `CHATWOOT_ENABLED` (optional, 默认 false) — Chatwoot 同步开关
- `COZE_API_TOKEN`, `COZE_BOT_ID` (default in code: `7599886499640147968`)
- `APP_SECRET_KEY`, `APP_DEBUG`, `APP_HOST`, `APP_PORT`, `APP_LOG_LEVEL`

The `Settings` classes use `env_prefix` so group names map directly: `WECHAT_*` → `WeChatSettings`, `COZE_*` → `CozeSettings`, etc. The `Settings` aggregator also reads `.env` directly.

The Coze workflow contract is documented in `COZE_WORKFLOW_GUIDE.md` — the workflow was designed to accept `{user_id, text?, file_image_id?, file_voice_id?}` and return `{action, reply_content: {msgtype, text: {content}}, metadata}`. In practice `CozeService.run_workflow` sends `parameters: {}` (the deployed workflow takes no inputs); `DifyService.run_workflow` implements the documented input contract. The `file_image_id` / `file_voice_id` JSON-stringification as `{"file_id": ...}` applies to Coze; Dify uses `{type, transfer_method, upload_file_id}` file-object arrays.

## Code conventions

- Async-first: services use `httpx.AsyncClient`; never call `requests` or sync I/O from async routes.
- `routes/` are thin dispatchers (~170 lines total); protocol logic lives in `protocols/`, orchestration in `services/message_processor.py`. Don't put business logic in routes.
- KF uses `BackgroundTasks`; bot uses `asyncio.create_task` (both to beat WeChat's 5s callback timeout). The bot returns an encrypted envelope immediately, KF returns `"success"`.
- Crypto is centralized in `app/crypto/wecom_crypto.py` (pure functions); `WeChatService` crypto methods are thin delegates. Adapters/routes should not call `wechatpy` crypto directly — go through `WeComCrypto` or `WeChatService`.
- New protocol? Implement `ProtocolAdapter` (receive/send/build_sync_ack/verify_url/dedup). `MessageProcessor` is protocol-agnostic — don't branch it on protocol except for the acknowledged trace差异 (bot-only 9-stage BotTrace).
- Logging uses the stdlib `logging` module; `app.main` configures root level from `APP_LOG_LEVEL`. The format string is `%(asctime)s - %(name)s - %(levelname)s - %(message)s` — keep it compatible when adding log lines.
- Do not log secrets, access tokens, or raw `echostr` after decryption.

## Working with the WeChat callback

- WeChat requires HTTPS for production callbacks; the dev servers (`run_test.py`/`test_wechat_*.py`) are HTTP-only and intended for tunneling.
- Every KF callback handler must return `PlainTextResponse("success")` on any internal error so WeChat doesn't retry; log the underlying exception. Bot returns an encrypted envelope (or 4xx/5xx on parse/sig failure).
- `User-Agent` filtering is intentional (KF path) — it rejects traffic that isn't WeChat. Don't loosen it without confirming the new UA is legitimate.
- Adapters are wired in `app/main.py` lifespan onto `app.state` (`kf_adapter`, `bot_adapter`, `message_processor`, `dedup_store`, `conversation_store`). Routes read them from `request.app.state`.

## Common pitfalls

- **`ffmpeg`/`ffprobe`**: required on the host for `MediaService` voice conversion (KF voice AMR→WAV). The Dockerfile installs them; local dev needs them on `PATH` (or in the Windows paths `media.py` checks). Bot voice path does NOT transcode (uploads raw AMR).
- **`EncodingAESKey` length**: must be exactly 43 chars. `WeChatConfig._validate_config` logs a warning otherwise but doesn't block startup.
- **Single-round mode**: don't accidentally re-introduce `SessionService`/消息历史存储. `ConversationStore` 仅存一个 conversation_id 字符串 (非历史), 用于 Dify chatflow 续接 —— 这是允许的, 与禁用的历史会话存储是不同概念。
- **Coze input shape**: the workflow must receive `file_image_id` / `file_voice_id` as **JSON strings** containing `{"file_id": ...}`, not bare IDs. `CozeService.run_workflow` does the stringification. (Dify uses `{type, transfer_method, upload_file_id}` file-object arrays — no stringification.)
- **Bot media differences**: bot image `url` → Dify `remote_url` (no download/upload); bot image/voice `media_id` → download + `client.upload_file` (no transcode). KF image/voice `media_id` → download + `ai.upload_file` (voice transcodes AMR→WAV).
- **Test isolation**: tests mock at the adapter/processor boundary (`MagicMock(spec=WeChatService)` / `_FakeAdapter`), not at the `httpx` level, to keep tests stable. `tests/test_flow.py` is module-skipped due to starlette 0.27 + httpx 0.28 incompatibility (pre-existing infra issue, unrelated to this refactor).
