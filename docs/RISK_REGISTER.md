# 风险登记表 (Risk Register)

> 基于 2026-07-02 代码审计 + 远端 .env + uvicorn 日志。现网基线:`APP_AI_BACKEND=dify`、`CHATWOOT_ENABLED=true`、`APP_BOT_TRACE_MODE=inline`、单 worker、InMemory 存储。
>
> 用法:逐项修复,完成后把状态改为 `✅ 已修复` 并填 commit/日期。`🔴/🟠/🟡` = 高/中/低优先级。`活/潜在` = 现网是否触发。

---

## A. 消息丢失类

### A1 🔴 `mark_done` 在发回复之前调用 — 崩溃=丢消息且重试被吞 (活) ✅ 已修复
- **位置**: `app/services/message_processor.py`(原 :113) + `app/protocols/base.py`(DedupStore)
- **机制**: 旧版 `:113` 在 AI 调用/handoff/send **之前**就把 msgid 标记完成。进程在此区间崩溃/取消 → 回复没发 → 微信重试被 `acquire` 挡住 → 消息永久丢失。
- **修复**: 重设计 dedup 状态机 —— `_processing`(处理中,可重试) → send 成功后才 `mark_done` 进 `_processed`(防重发)。`mark_done` 移到 `adapter.send` 成功后;失败/异常路径 `release_processing` 清 `_processing` 允许重试。`finally` 兜底释放未完成的 msgid。
- **状态**: ✅ 已修复 (2026-07-02, 部署 pid 862356)

### A2 🟠 异常处理器不清 `_processed` — 崩溃后该 msgid TTL 内不可重试 (活) ✅ 已修复
- **位置**: `message_processor.py`(原 :201-205)
- **机制**: 旧版 `except Exception` 只清 `_processing`,异常发生在 mark_done 之后则 no-op。
- **修复**: 随 A1 —— mark_done 仅在 send 成功后调用,异常时 msgid 仍在 `_processing`(非 `_processed`),`release_processing` 生效;`finally` 兜底。
- **状态**: ✅ 已修复 (随 A1)

### A3 🟠 `CancelledError` 不被 `except Exception` 捕获 (活) ✅ 已修复
- **位置**: `message_processor.py` + `routes/wechat.py:141`
- **机制**: `CancelledError` 是 `BaseException`,穿透 `except Exception` → release 不执行 → msgid 卡 `_processing`。
- **修复**: 加 `except BaseException`(记日志后 re-raise)+ `finally` 兜底 release。
- **状态**: ✅ 已修复 (随 A1)

### A4 🟠 `_processing` 集合无 TTL 清理 — 永久泄漏 (活) ✅ 已修复
- **位置**: `base.py`(InMemoryDedupStore.acquire)
- **机制**: 旧版 acquire 只清过期 `_processed`/`_sent`,从不清 `_processing` → 取消/硬崩的 msgid 永久卡死。
- **修复**: `_processing` 改为 `Dict[msgid, 进入时间戳]`,acquire 时顺带清过期项。
- **状态**: ✅ 已修复 (随 A1)

### A5 🟠 KF 批量消息只处理最新一条 (活) ✅ 已修复
- **位置**: `kf_adapter.py`(receive 原 `latest = messages[0]`) + `wechat.py`(`clear_cursor=True`)
- **机制**: `sync_latest_messages` 可返回多条客户消息(降序),只取最新,其余静默丢弃;`clear_cursor` 清游标,一次回调多条客户消息只活一条。
- **修复**: `receive` 改为派发本次同步的全部客户消息。`sync_latest_messages` 返回降序(最新在前)→ 反转为升序(最旧在前),让 route 层 BackgroundTasks 按时间顺序串行处理,Dify chatflow 多轮 conversation_id 正确续接。无 msgid 的脏数据(无法 dedup)跳过。每条独立 dedup,已处理过的由 MessageProcessor 跳过。route 层已遍历 `inbound_list` 全派发,无需改。
- **状态**: ✅ 已修复 (2026-07-02)

---

## B. 静默失败类

### B1 🟠 KF 空回复:用户什么都收不到 (活) ✅ 已修复
- **位置**: `message_processor.py`(空回复兜底块)
- **机制**: AI 返回空时 KF 分支只 `logger.warning`+`return`,用户收不到任何回复也无错误。Dify `data.status=failed` 经 `compose_multimodal_markdown` 变空串 → 触发。
- **修复**: KF 空回复也给兜底文案("抱歉，我暂时无法处理该消息，请稍后重试。"),与 bot 路径("（AI 未返回内容）")对齐;不再 early return,正常走 step 7 投递 + mark_done。
- **状态**: ✅ 已修复 (2026-07-02)

### B2 🔴 `_is_handoff` fail-open — Chatwoot 抖动时 AI 抢答 (活, Chatwoot 已启) ✅ 已修复 (轻量)
- **位置**: `message_processor.py` `_is_handoff`
- **机制**: Chatwoot 异常时返回 False(不接管)→ AI 照答,可能与人工抢答。
- **修复**: 按用户决定保持 fail-open(避免 Chatwoot 抖动时 AI 全面停摆),仅升级告警 —— 日志加 `HANDOFF_FAIL_OPEN` 标记 + open_kfid 上下文,便于告警/统计。
- **状态**: ✅ 已修复 (2026-07-02, 仅加告警, 行为不变)

### B3 🔴 AI 异常原文直接发给用户 — 数据泄漏 (活, Dify) ✅ 已修复
- **位置**: `message_processor.py`(原 :143-151)
- **机制**: 旧版 `reply_text = f"AI 处理失败: {e}"` 把 `AIBackendError`/`DifyError`(含内部 outputs)发给客户。
- **修复**: AI 失败时对用户只回固定脱敏文案("AI 服务暂时不可用,请稍后重试"/"AI 服务暂时不可用，请稍后重试");详细错误只进日志(exc_info=True)。
- **状态**: ✅ 已修复 (2026-07-02)

### B4 🟡 bot `asyncio.create_task` 不保留引用 + BaseException 不可见 (活)
- **位置**: `routes/wechat.py:141-144`
- **机制**: task 不存引用可能被 GC;`CancelledError`/`BaseException` 不被 `process` 捕获,只产 "Task exception was never retrieved"。KF 用 `BackgroundTasks`,两协议语义不一致。
- **修复思路**: 用 `asyncio.create_task` 时存引用到集合,done 回调里移除并记日志;或统一用 BackgroundTasks。
- **状态**: 🔧 待修复

---

## C. 配置陷阱类

### C1 🟡 `WeChatConfig._validate_config` fail-open — 配置缺失不阻断启动 (潜在)
- **位置**: `wechat.py:44-74`
- **机制**: 缺 corp_id/secret/token/aes_key 时填默认值(`'default_corp_id'`、`'A'*43`)并继续启动,然后每个微信 API 调用静默 401。
- **修复思路**: 关键配置缺失时启动期 fail-fast(raise),而非填默认继续。
- **状态**: 🔧 待修复

### C2 🟡 `load_settings` 第三分支无条件 `raise SystemExit(1)` (潜在)
- **位置**: `config.py:308-314`
- **机制**: `SystemExit(1)` 缩进在 `try` 外,即使 `Settings()` 成功也杀进程。只在 `.env`+`env.example` 都加载失败时触发,但控制流写错。
- **修复思路**: 把 `raise SystemExit(1)` 移进 `except` 块。
- **状态**: 🔧 待修复

### C3 🟡 三个 `WeChatService` 生命周期并存 (活)
- **位置**: `app.state.wechat_service`(单例)、`chatwoot_internal.py:64`(每请求新建)、`coze.py:51`(CozeService 内部建且从不被 MessageProcessor 使用/关闭)
- **机制**: 资源泄漏 + 维护隐患。
- **修复思路**: 统一用 `app.state.wechat_service`;删 `coze.py:51` 的冗余实例。
- **状态**: 🔧 待修复

### C4 🟡 `access_token` 经 wechatpy 同步属性在 async 里取 (活)
- **位置**: `wechat.py:230-240`
- **机制**: `get_access_token` 是 async 但返回 wechatpy 同步属性(token 过期时阻塞 HTTP 刷新);单例无 async 锁,并发撞过期 token 会双重刷新 + 阻塞事件循环。
- **修复思路**: 加 async lock 串行化 token 刷新;或 token 预刷新。
- **状态**: 🔧 待修复

---

## D. 数据泄漏类

### D1 🟠 DIAG `logger.warning` 打全量 inputs/raw (潜在, 切 workflow 触发) ✅ 已修复
- **位置**: `dify.py`(原 :405-407 inputs 500 字、原 :426-444 raw 头尾各 300 字 + 每 key 120 字)
- **机制**: WARNING 级临时调试码,含用户消息/文件 URL/Dify 内部输出。仅 workflow 路径有,chatflow 无 → 现网不触发,切 workflow 即泄漏。
- **修复**: 删除两处 DIAG 调试码 + 随之未用的 `import json`。保留 `inputs keys=%s`(仅 key 名,无值)的 info 日志。
- **状态**: ✅ 已修复 (2026-07-02)

### D2 🟡 启动日志泄露 token 前缀 (活)
- **位置**: `run_uvicorn.py:13`(api_token[:8])、`wechat.py:54/103`(corp_id、kf_token 前 10 位)
- **机制**: 进 `/tmp/uvicorn.log`(root 可读)。
- **修复思路**: 只打"已配置/未配置"布尔,不打前缀。
- **状态**: 🔧 待修复

### D3 🟡 `separate` trace 模式复用一次性 response_url (潜在, 切 separate 触发)
- **位置**: `bot_adapter.py:184` + docstring `:45`
- **机制**: 对同一 `response_url` 第二次 POST(trace),与"一次性"docstring 矛盾。复用要么被拒(trace 静默丢,`:380` 吞异常),要么给用户发游离 trace。现网 `inline` 不触发。
- **修复思路**: separate 模式改为不二次 POST(trace 只进日志),或确认微信是否允许多次后更新 docstring。
- **状态**: 🔧 待修复

---

## E. Chatwoot Rails 侧 (`/home/ranlei/chatwoot`, 不改代码无法修)

### E1 🔴 handoff 公开端点完全无鉴权 + 信息泄露 (活) ✅ 已修复
- **位置**: `app/controllers/public/api/v1/wecom/handoff_statuses_controller.rb`
- **机制**: 注释声称"由 X-WecomAI-Signature 签名保护",但代码从不读 header/不验签。任何人凭 `open_kfid+external_userid` 可查 `assignee_id/assignee_name/conversation_id/is_online`,无频率限制。
- **修复**: 控制器 `show` 前置 `valid_signature?`(HMAC-SHA256 over `request.query_string`,secret 用 per-channel `channel.wecom_ai_secret`,与 webhook 路径一致);失败 401。用原始 query_string 验签(与 Python `urllib.parse.urlencode` 输出对齐,不重组 params)。spec 已补。
- **状态**: ✅ 已修复 (Chatwoot commit 351ffb11a, 已 live 返回 401)

### E2 🟠 webhook 控制器不验签就入队 (活) ✅ 已修复
- **位置**: `app/controllers/webhooks/wecom_controller.rb`
- **机制**: 校验全推给 job,控制器无条件 `head :ok`。可向 `low` 队列灌垃圾(DoS 放大);wecom-ai 无法区分接受/拒绝(都 200)。
- **修复**: 控制器 `process_payload` 前置验签(用 `request.raw_post` + per-channel secret),失败 401 不入队;malformed body(非 Hash)400 不入队;仅验签通过才 `perform_later`。job 内保留二次验签。spec 已补。
- **状态**: ✅ 已修复 (Chatwoot commit 351ffb11a)

### E3 🟠 HMAC 无 replay 防护 (活)
- **位置**: `app/jobs/webhooks/wecom_events_job.rb:41`
- **机制**: 签 `HMAC(secret, post_body)` 无 nonce/timestamp,捕获对永久有效。`reason:ai_handoff` 类完全无去重,可无限重放触发 `bot_handoff!`。
- **修复思路**: payload 加 timestamp + 服务端校验时效;handoff 事件加幂等键。
- **状态**: 🔧 待修复 (需改 Rails)

### E4 🟠 `bot_handoff!` 不分配客服 (活)
- **位置**: `app/models/conversation.rb:166-170` + `wecom_events_job.rb:24-33`
- **机制**: 只 `update(waiting_since)`+`open!`+派发事件,不 assign agent。是否有人接取决于外部监听器。无人接则 `HandoffQueryService` 持续 `unassigned` → 主动转人工后下条消息被动 handoff 不生效。
- **修复思路**: Chatwoot 配 `CONVERSATION_BOT_HANDOFF` 自动分配;或 Rails 侧 `bot_handoff!` 后自动 assign 在线 agent。
- **状态**: 🔧 待修复 (需改 Rails + 运维配置)

### E5 🟠 已解决(resolved)会话被静默复用 (活) ✅ 已修复
- **位置**: `incoming_message_service.rb:48`、`wecom_events_job.rb:29`、`handoff_query_service.rb:23-24`
- **机制**: 三处 `conversations.last` 无 status 过滤,因 `lock_to_single_conversation?` 恒 true。已解决会话被追加消息/查 handoff。
- **修复**: 三处 `conversations.last` → `conversations.where.not(status: :resolved).last`(方案 A:resolved 后开新会话,用户确认)。resolved 后:incoming 新建会话 / handoff 返回 no_conversation→AI 答 / handle_bot_handoff! 不触发。incoming_message_service 简化掉 lock 的 if/else 死分支。spec 已补。
- **状态**: ✅ 已修复 (Chatwoot commit dc02a8b92, 22 examples 0 failures; 另补 handoff_status suspended account 拒绝逻辑 + handle_bot_handoff! resolved spec)

### E6 🟡 `assignee` 为 nil 时 500 (活) ✅ 已修复
- **位置**: `handoff_query_service.rb`(原 :29)
- **机制**: `assignee.id`,assignee 被硬删而 `assignee_id` 还在 → `NoMethodError` → 公开端点 500。
- **修复**: `assignee.nil?` 时返回 `{handoff:false, reason:'unassigned'}`(纯防御,不改正常行为)。
- **状态**: ✅ 已修复 (Chatwoot commit 351ffb11a)

### E7 🟠 多处并发竞态无锁无唯一约束 (活) 🟡 部分修复 (E7-2 done, E7-3 仍为 known gap)
- **位置**: `incoming_message_service.rb:32`(`messages.source_id` 非唯一 → TOCTOU 重复消息)、`:54-59`(首条 `Conversation.create!` 无锁 → 重复会话)、`:100-104`(`social_wecom_user_id` 无唯一索引 → 群聊 per-user Contact 重复)
- **现状(已查)**: `contact_inboxes` 已有 `(inbox_id, source_id)` 唯一索引;生产 DB 三项均无重复数据(2026-07-02 复查)。
- **修复 (Chatwoot commit 94be8b050)**: E7-2 用 Redis mutex(`MutexApplicationJob` + `(inbox_id, external_userid)` key, 5s TTL)串行化同一客户消息处理;锁耗尽走 `process_without_lock` 但重新校验 channel/account/HMAC。E7-1(wecom-ai dedup 已够,未做 DB 兜底)。
- **未覆盖 (E7-3, known gap)**: per-user Contact 按 `userid`(群成员)创建,mutex key 是 `external_userid`,群聊场景下不同 `external_userid` 同 `userid` 的并发消息走不同 mutex → 仍可能重复创建 Contact。`set_sender_contact` 仍无 rescue,无 DB 唯一索引。当前未做 JSONB 唯一索引是因为 `social_wecom_user_id` 在主 Contact 和 per-user Contact 上双写,直接 account 级唯一索引会误伤合法多群/多 contact 场景 → 需要先重构 Contact attribute 语义(主/group 用 group/external 标识,per-user Contact 独占 user id),再加并发唯一索引。这是后续 DB 兜底专项,不在当前 PR 范围。服务层 `rescue RecordNotUnique` 在没有对应唯一索引时是死代码,因此暂不补。
- **已确认 (2026-07-03, 同 PR spec 加固)**:
  - `process_without_lock(*args, **kwargs)` 的 retry_on 真实参数形态已覆盖 spec:用 `job.process_without_lock(job_args_hash)` 验证 `args.first` 分支(因 ActiveJob 序列化后 `job.arguments` 是 `[{kwargs}]`,splat 后 hash 进入 `args.first`)。`job.public_send(:process_without_lock, *job.arguments)` 才是基类 retry_on 真实调用形态,两者等价。
  - mutex 5s TTL 假设成立:wecom 消息处理在 Chatwoot 侧无外部下载(wecom-ai 已把附件 base64 嵌进 post_body),主要耗时是 contact/conversation 查找 + message 写库 + dispatcher,正常远小于 5s。
- **状态**: 🟡 部分修复 (E7-2 ✅ commit 94be8b050; E7-3 ❌ 仍为 known gap,留作后续 DB 兜底专项; E7-1 可选未做; retry_on 参数解包 + TTL 假设已 spec 覆盖并经审查确认)

### E8 🟠 出站只支持 text, 失败不重试 (活)
- **位置**: `send_text_service.rb`(硬编码 `msgtype:text`,图片/文件/模板被当文本发,附件静默丢)、`send_on_wecom_service.rb:25`(`rescue StandardError` 吞异常标记 failed,不触发 Sidekiq 重试)
- **修复思路**: 出站支持多 msgtype;失败让异常传播触发 Sidekiq 重试。
- **状态**: 🔧 待修复 (需改 Rails)

### E9 🟡 Rails 测试与生产签名不匹配 (活) 🟡 部分修复
- **位置**: `spec/jobs/webhooks/wecom_events_job_spec.rb`(位置参数调 kwargs `perform`,Ruby 3.4.4 抛 ArgumentError)→ spec 实际没测生产路径;出站服务/HmacClient/handoff 公开端点零测试覆盖
- **修复**: `WecomEventsJob#perform` 已改 kwargs(spec 能跑);补了 webhook controller / handoff_status / incoming_message / handoff_query spec。
- **遗留**: 本会话跑 spec 时警告 "Sidekiq testing API enabled, but this is not the test environment" —— 测试环境配置(RAILS_ENV / 测试 DB)待 Chatwoot 会话确认,确保 spec 跑在 test 环境不污染 dev DB。
- **状态**: 🟡 部分修复 (commit 351ffb11a + 本会话补 spec;测试环境配置待确认)

---

## F. 架构/语义遗留

### F1 🟠 bot 路径完全不通 Chatwoot (活, 本次需求起点)
- **位置**: `message_processor.py:196`(`if not is_bot` 排除 bot)、`_is_handoff:468`(`if not inbound.open_kfid` 让 bot 永不查 handoff)
- **机制**: bot 消息不进 Chatwoot、不能转人工(被动/主动都不行)。
- **修复思路**: 见独立方案(合成 open_kfid + handoff_signal + response_url_store),但**应在 A1/E4 修复后再做**(bot 转人工回程依赖 dedup/发送链路可靠)。
- **状态**: 🔧 待修复 (依赖 A1, E4)

### F2 🟠 AI 主动转人工信号不被解析 (活)
- **位置**: `response_parser.py`/`multimodal.py`/`trace_extract.py`(无 handoff/action 字段)、`coze_tasks.py:51-53`(死的 TRANSFER_HUMAN stub)、`chatwoot_sync_service.py:147`(`trigger_bot_handoff` 定义但全代码无人调用)
- **机制**: chatflow `charge_charging_v16` 有"转人工出口"节点但未对外暴露结构化字段 → 主动转人工无法触发。
- **修复思路**: 约定信号契约(outputs.handoff 或 answer marker)+ 新 `handoff_signal.py` 检测 + 接 `trigger_bot_handoff`。
- **状态**: 🔧 待修复 (随 F1)

### F3 🟡 chatflow 输入字段被忽略 (活) ✅ 已修复
- **位置**: `dify.py`(`_run_chatflow` 原 `inputs={}`)、`config.py`(DifySettings 新增字段)
- **机制**: 忽略 chatflow `user_input_form` select 字段(input_language/input_hint_endpoint/input_hint_region)→ L1 板块路由拿不到 hint → 可能总走默认分支。字段 `required:false` 不报错,但路由精度受损。
- **修复**: 实测线上 chatflow `/parameters` 确认三个 select 字段。`DifySettings` 加 `chatflow_input_language`/`chatflow_input_hint_endpoint`/`chatflow_input_hint_region`(env `DIFY_CHATFLOW_INPUT_*`,默认空=行为不变)。`_run_chatflow` 取值优先级:input_data 透传(`language`/`hint_endpoint`/`hint_region`)> 部署级配置 > 不传;仅传非空值。偏离登记表"在 `_prepare_*` 填"的原始建议 —— 改在 chatflow 边界注入更 DRY(不必 KF/bot 两处重复),且 hint 是部署级常量非逐消息派生。
- **状态**: ✅ 已修复 (2026-07-02)

### F4 🟡 KF 与 bot 分发语义不一致 (活)
- **位置**: `routes/wechat.py`(KF `BackgroundTasks`+`"success"`;bot `asyncio.create_task`+加密 envelope)
- **机制**: 无统一抽象,维护时易漏一边。
- **修复思路**: 评估是否统一(但协议差异是真实的,可能不值得强行统一)。
- **状态**: 🔧 待评估

### F5 🟡 Coze 后端丢弃全部用户输入 (潜在, 切回 Coze 触发)
- **位置**: `coze.py:91-95`(`run_workflow` 发 `parameters:{}`,input_data/user_id/conversation_id 全弃)
- **机制**: 现网不跑 Coze,但若切回:多轮/图片/语音到不了 AI,`_prepare_*` 媒体下载/上传全白做。
- **修复思路**: Coze workflow 接受输入时恢复透传;或在 CLAUDE.md 明确 Coze 是固定响应 workflow。
- **状态**: 🔧 待修复 (或文档化)

---

## 修复优先级建议

1. **第一批(🔴 高, Python 侧, 改动小收益大)**: A1(含 A2/A3/A4 dedup 时序重设计)、B3(异常脱敏)、B2(handoff fail 策略)
2. **第二批(🟠 中, Python 侧)**: A5(KF 批量)、B1(KF 空回复兜底)、D1(删 DIAG)、F3(chatflow 输入字段)
3. **第三批(🔴/🟠 Chatwoot Rails 侧)**: E1(handoff 鉴权)、E4(转人工分配)、E8(出站多类型+重试)、E5/E6/E7(会话/竞态)
4. **第四批(F1 bot 接 Chatwoot)**: 依赖 A1 + E4 修完再做

---

## 修复记录

| 日期 | 项 | commit | 备注 |
|---|---|---|---|
| 2026-07-02 | A1/A2/A3/A4 | 58a17b6 | dedup 状态机重设计: mark_done 移到 send 成功后; BaseException 捕获; _processing 加 TTL |
| 2026-07-02 | B3 | 58a17b6 | AI 异常脱敏: 固定文案, 详细错误只入日志 |
| 2026-07-02 | B2 | 58a17b6 | handoff fail-open 加 HANDOFF_FAIL_OPEN 告警标记 (行为不变) |
| 2026-07-02 | B1 | 3d3fc46 | KF 空回复兜底: 给用户兜底文案, 不再静默丢弃 |
| 2026-07-02 | D1 | 3d3fc46 | 删 workflow 路径 DIAG 调试码 (inputs/raw 全量 dump) + 未用 import |
| 2026-07-02 | F3 | 3d3fc46 | chatflow 输入字段透传: config 驱动 + input_data 覆盖, 注入 L1 路由 hint |
| 2026-07-02 | A5 | 3d3fc46 | KF 批量消息全派发: receive 返回全部客户消息 (升序), 不再只取最新 |

---

## 运维配置待办 (非代码, 2026-07-03 整体回归验证发现)

### OPS-1 🔴 Chatwoot 缺真实 open_kfid 的 wecom channel → KF 转人工检查全 401 (活)
- **现象**: 生产每条 KF 消息 `check_handoff` 返回 `401 {"error":"invalid signature"}` → `{handoff:False}` → KF 永远走 AI,转人工 no-op。
- **根因**: Chatwoot DB 只有测试 channel `kf_test_open_kfid_001`(corp_id=`ww_test_corp_001`),**没有真实 open_kfid `wk2m-vCwAAkKIHsr8jtoekF84d5m2qeQ` 的 channel**。Python 对真实 open_kfid 签名 → Chatwoot `find_by(open_kfid:)` 找不到 channel → `valid_signature?` 返回 false → 401。
- **验证**: 端到端测试用测试 channel 通(401→200);生产真实 channel 未建,故全 401。
- **修复 (运维)**: 在 Chatwoot 创建 `open_kfid=wk2m-vCwAAkKIHsr8jtoekF84d5m2qeQ` 的 wecom channel。字段:
  - `corp_id` = `wwe23eb3735710f8dc`(wecom-ai `WECHAT_CORP_ID`;Chatwoot 代码仅 presence 校验,不参与逻辑)
  - `open_kfid` = `wk2m-vCwAAkKIHsr8jtoekF84d5m2qeQ`(日志真实流量)
  - `wecom_ai_service_url` = `http://120.55.45.59:8501`(wecom-ai 地址)
  - `wecom_ai_secret` = `test_secret_key_abc123`(与 Python `CHATWOOT_HMAC_SECRET` 一致)
  - `account_id` = 1(现有测试 channel 同 account;或按业务选 account 2)
- **创建方式**: Chatwoot 后台 UI(wecom inbox 创建表单,推荐,走正常流程)或 DB 直接插(快但绕过 inbox 关联,需同时建 inbox + contact_inbox 关联)。
- **状态**: 🔧 待运维创建 (2026-07-03)

### OPS-2 🟠 Dify chatflow 偶发 `Variable ['6901','value_false'] not found` (活)
- **现象**: 个别 KF 消息 AI 工作流 400 `invalid_param Variable ['6901','value_false'] not found`,B3 兜底回脱敏文案。
- **根因**: Dify chatflow `charge_charging_v16` 内部某条件分支引用了未定义变量 `6901`/`value_false`。偶发,非 F3 inputs 稳定触发(带 inputs 多数正常)。
- **归属**: Dify chatflow 配置问题,非 wecom-ai/Chatwoot 代码。
- **修复**: 在 Dify 平台修 chatflow 该分支的变量引用。
- **状态**: 🔧 待 Dify 配置修复 (2026-07-03)

---

## 整体回归验证记录 (2026-07-03)

wecom-ai(第一批 A1/B2/B3 + 第二批 B1/D1/F3/A5)+ Chatwoot(第三批 E1/E2/E5/E6/E7-2)协同验证:

**✅ 正常**:
- A5 KF 批量全派发(日志:`选中 4 条客户消息 (按时间升序派发)`)
- A1 dedup 状态机(重试正确去重,不丢消息)
- E2 webhook 验签 + 落库(真实 KF 消息 `同步成功 status=200`)
- B3 AI 异常脱敏(失败回兜底文案)
- bot 路径(2 条消息正常 `异步推送 HTTP 200 errcode=0`)
- KF 回复投递正常

**🔴 问题**: OPS-1(KF 转人工 401,channel 缺失)、OPS-2(chatflow 偶发变量错误)—— 均为运维/Dify 配置,非代码。
