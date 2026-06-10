# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project is

A FastAPI middleware that bridges **WeChat Customer Service (微信客服)** callbacks and a **Coze AI agent workflow**. The service receives WeChat KF messages, decrypts/signs them, downloads/standardizes media, calls a Coze workflow, and posts the agent's reply back to the user.

**Current mode**: single-round conversation (单轮对话). No Redis-backed session store, no multi-turn memory — every inbound message triggers an independent Coze workflow call and the reply is sent back immediately. Code paths for `SessionService` and Redis are intentionally absent (do not reintroduce them unless explicitly asked).

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
WeChat KF server ──POST /wechat/kf/callback──▶ FastAPI
                          │                         │
                          │   decrypt (AES)         │
                          │   verify signature      │  BackgroundTasks
                          ▼                         ▼
                WeChatService.sync_latest_messages  ──▶ process_message_background()
                          │                                    │
                          │  text / image / voice              │  standardize
                          ▼                                    ▼
                MediaService (download + base64)   CozeService.run_workflow()
                                                              │
                                                              ▼
                                            WeChatService.send_kf_message()
                                                              │
                                                              ▼
                                                       WeChat KF client
```

### Request flow (POST /wechat/kf/callback)

1. `app/routes/wechat.py:wechat_callback_handler` checks the User-Agent allows `WeChat`/`Mozilla/4.0` and reads the encrypted XML body.
2. Signature is verified with `WeChatService.verify_signature` (SHA1 of `token + timestamp + nonce + Encrypt`).
3. The encrypted payload is decrypted via `WeChatService.decrypt_message_custom` (custom AES wrapper around `wechatpy`'s `WeChatCrypto`).
4. The decrypted XML's `MsgType == 'event' && Event == 'kf_msg_or_event'` triggers a pull-style sync: `WeChatService.sync_latest_messages` paginates the WeChat sync API to grab the latest customer message. Plain messages (text/image/voice) are supported; other types are logged and dropped.
5. Actual processing is dispatched into `BackgroundTasks` (`process_message_background`) so the HTTP response returns `success` immediately to WeChat.
6. The background coroutine standardizes the message (`DataStandardizationService`), invokes `CozeService.run_workflow` with `{text | file_image_id | file_voice_id, user_id}`, and posts the result back via `WeChatService.send_kf_message`.

### Single-round mode — what that means in code

- `app/services/__init__.py` exports only `WeChatService`, `CozeService`, `MediaService` — no `SessionService`.
- `app/core/config.py:Settings` keeps `RedisSettings` and `CelerySettings` classes defined for config compatibility, but they are not wired into the runtime. `monitoring/health/detailed` reports `mode: "single_round_conversation"`.
- `Celery`/`flower`/`prometheus_client`/`sentry-sdk` are pinned in `requirements.txt` but not currently wired in — leave them unless the task is to enable them.

### Module layout (`app/`)

| Path | Responsibility |
|------|----------------|
| `main.py` | FastAPI app, lifespan, CORS/trusted-host middleware, exception handlers, root + info endpoints. |
| `core/config.py` | `pydantic-settings` with grouped settings: `WeChatSettings`, `CozeSettings`, `RedisSettings`, `DatabaseSettings`, `CelerySettings`, `AppSettings`. `load_settings()` falls back from `.env` → `env.example` → defaults. |
| `core/exceptions.py` | `WeChatAPIError`, `CozeAPIError`, `SessionError` (kept for compat), `ValidationError`, `BusinessError`, and the matching `handle_*` → `HTTPException` converters. |
| `models/wechat.py`, `models/coze.py` | Pydantic models: `WeChatMessage`, `MessageType` (text/image/voice/video/file/location/event), `StandardizedMessage`, `CozeWorkflowOutput`, `ActionType`, `IntentType`. |
| `routes/wechat.py` | `GET /wechat/kf/callback` (URL verify), `POST /wechat/kf/callback` (message), `GET /wechat/test`. |
| `routes/monitoring.py` | `/monitoring/health`, `/health/detailed`, `/metrics`, `/stats`. |
| `services/wechat.py` | `WeChatService` + `WeChatConfig`. Wraps `wechatpy.enterprise.WeChatClient` and `wechatpy.enterprise.crypto.WeChatCrypto`. Owns signature verify, AES decrypt, message sync, single-message processing, KF message send. |
| `services/coze.py` | `CozeService`. Calls the Coze workflow via `httpx`; also holds a `cozepy.Coze` SDK client. `run_workflow()` accepts the simplified `{text, file_image_id, file_voice_id}` input and converts to Coze `parameters`. |
| `services/media.py` | `MediaService`. Downloads temporary media from WeChat (uses `pydub` + `ffmpeg` for voice). Auto-detects `ffmpeg` on Windows. |
| `services/standardization.py` | `DataStandardizationService`. WeChat message → `StandardizedMessage` (single-turn, no history). |
| `tasks/*.py` | Celery task definitions for `wechat`/`coze`/`media`. Unused in current single-round mode but kept for future use. |

### Configuration

All config is env-driven via `pydantic-settings`. Required keys for a working deployment:

- `WECHAT_CORP_ID`, `WECHAT_CORP_SECRET`, `WECHAT_KF_TOKEN`, `WECHAT_ENCODING_AES_KEY` (43 chars), `WECHAT_CALLBACK_BASE_URL`
- `WECHAT_ALLOWED_OPEN_KFID` (optional) — if set, only that KF account's messages are processed.
- `COZE_API_TOKEN`, `COZE_BOT_ID` (default in code: `7599886499640147968`)
- `APP_SECRET_KEY`, `APP_DEBUG`, `APP_HOST`, `APP_PORT`, `APP_LOG_LEVEL`

The `Settings` classes use `env_prefix` so group names map directly: `WECHAT_*` → `WeChatSettings`, `COZE_*` → `CozeSettings`, etc. The `Settings` aggregator also reads `.env` directly.

The Coze workflow contract is documented in `COZE_WORKFLOW_GUIDE.md` — the workflow must accept `{user_id, text?, file_image_id?, file_voice_id?}` and return `{action, reply_content: {msgtype, text: {content}}, metadata}`. `run_workflow` JSON-stringifies `file_image_id` / `file_voice_id` values as `{"file_id": ...}` before posting.

## Code conventions

- Async-first: services use `httpx.AsyncClient`; never call `requests` or sync I/O from async routes.
- `routes/` are thin; all business logic lives in `services/`.
- `BackgroundTasks` is used for any work that could exceed WeChat's 5s callback timeout (currently all message processing).
- Crypto is centralized in `WeChatService`; route handlers should not call `wechatpy` directly.
- Logging uses the stdlib `logging` module; `app.main` configures root level from `APP_LOG_LEVEL`. The format string is `%(asctime)s - %(name)s - %(levelname)s - %(message)s` — keep it compatible when adding log lines.
- Do not log secrets, access tokens, or raw `echostr` after decryption.

## Working with the WeChat callback

- WeChat requires HTTPS for production callbacks; the dev servers (`run_test.py`/`test_wechat_*.py`) are HTTP-only and intended for tunneling.
- Every callback handler must return `PlainTextResponse("success")` on any internal error so WeChat doesn't retry; log the underlying exception. See the existing `try/except` blocks in `routes/wechat.py`.
- `User-Agent` filtering is intentional — it rejects traffic that isn't WeChat. Don't loosen it without confirming the new UA is legitimate.
- The `decode` of `Encrypt` in the route handler pulls the field once for signature verification, then re-parses the XML after decryption; the local `decrypted_xml` variable must exist by the time control reaches the dispatch section.

## Common pitfalls

- **`ffmpeg`/`ffprobe`**: required on the host for `MediaService` voice conversion. The Dockerfile installs them; local dev needs them on `PATH` (or in the Windows paths `media.py` checks).
- **`EncodingAESKey` length**: must be exactly 43 chars. `WeChatConfig._validate_config` logs a warning otherwise but doesn't block startup.
- **Single-round mode**: don't accidentally re-introduce `SessionService`/Redis. The deployment notes in `README.md` say "Redis not required in current version" — keep it that way unless re-introduction is part of an explicit task.
- **Coze input shape**: the workflow must receive `file_image_id` / `file_voice_id` as **JSON strings** containing `{"file_id": ...}`, not bare IDs. `CozeService.run_workflow` does the stringification.
- **Test isolation**: `tests/test_flow.py` uses `MagicMock(spec=...)` for `WeChatService`/`CozeService` — keep mocks at the service layer, not at the `httpx` level, to keep tests stable.
