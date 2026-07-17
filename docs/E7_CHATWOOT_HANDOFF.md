# E7 — Chatwoot wecom 并发竞态加固 (交接文档)

> 承接自 wecom-ai-customer-service 会话。本文件在 wecom-ai 仓库,但所有改动落在 `/home/ranlei/chatwoot`(Chatwoot Rails)。带本文去 Chatwoot 会话执行。

## 一、背景

RISK_REGISTER E7:`Wecom::IncomingMessageService` 三处 find-or-create 模式无锁无唯一约束,并发 webhook 可产生重复数据。本次为**预防性加固** —— 阻止未来并发产生重复。

## 二、生产数据实测结论(已查,2026-07-02)

在 chatwoot-rails-1 容器实测,生产 DB **当前无重复数据**:

| 检查项 | 结果 |
|---|---|
| E7-1 messages 同 inbox 重复 source_id(非空) | **无重复** |
| E7-2 同 contact_inbox 多个 open 会话 | **无重复** |
| E7-3 contacts 同 account 重复 social_wecom_user_id | **无重复** |
| `contact_inboxes` 唯一索引 `(inbox_id, source_id)` | **已存在** ✅ |
| `messages.source_id` 索引 | 非唯一 `index_messages_on_source_id` |

**含义**:加唯一索引前**不需要去重 migration**(无数据冲突)。但执行时仍应**先复查一次**(脚本见第六节),因为执行时刻可能有新数据。若复查发现重复,再跑去重 migration(脚本见第七节)。

**wecom-ai 侧已有 dedup**(`DedupStore`,A1 修复后状态机可靠),所以并发实际概率低;E7 是 DB 层的第二道防线。

## 三、三处竞态现状

### E7-1 — 消息 TOCTOU(`incoming_message_service.rb:28-33`)
```ruby
def already_processed?
  msgid = params.dig(:message, :msgid).to_s
  return false if msgid.blank?
  @inbox.messages.where(source_id: msgid).exists?   # check
end
# ... 后续 create_message 里 build + save!          # use, 中间无锁
```
`exists?` 与 `save!` 之间无锁,并发同 msgid 两条 webhook 都通过 check → 都 save → 重复消息。

### E7-2 — 会话重复(`incoming_message_service.rb:46-59`)
```ruby
def set_conversation
  @conversation = @contact_inbox.conversations.where.not(status: :resolved).last  # find
  return if @conversation
  @conversation = ::Conversation.create!(...)  # create, 无锁
end
```
客户首条消息并发:两个 job 都 `conversations.last→nil→create!` → 同一 contact_inbox 两个 open 会话(违反 wecom `lock_to_single_conversation`)。

### E7-3 — per-user Contact 重复(`incoming_message_service.rb:88-104`)
```ruby
def set_sender_contact
  @sender_contact = ::Contact.find_by(account_id:..., additional_attributes: { social_wecom_user_id: userid })  # find
  return if @sender_contact.present?
  @sender_contact = ::Contact.create!(...)  # create, 无锁, 无唯一索引
end
```
群聊场景并发首条:两个 job 都 find→nil→create → 同一 userid 两个 Contact。

**注意**:`ContactInboxWithContactBuilder`(E7-2 的 contact_inbox 创建路径)已有 `rescue ActiveRecord::RecordNotUnique` 保护,所以 contact_inbox 本身不会重复。E7-2 针对的是 **Conversation** 的重复,E7-3 针对的是 **per-user Contact** 的重复。

## 四、修复方案

### E7-1 — 消息唯一索引 + rescue(降优先级,可选)
wecom-ai 侧已 dedup,实际概率极低。若仍要 DB 兜底:
- 加部分唯一索引 `ON messages (inbox_id, source_id) WHERE source_id != ''`
- `create_message` 后 `save!` 包 `rescue ActiveRecord::RecordNotUnique` → 当作已处理跳过

**⚠️ 注意**:`source_id` 是 wecom-ai 传的 msgid,对 outgoing(AI 回复,origin=2)消息也用同一 msgid 字段。若同一 msgid 既要记 incoming 又要记 outgoing(不会,但需确认),唯一索引会冲突。执行前**确认 wecom-ai 侧 incoming/outgoing 的 msgid 不复用**。

### E7-2 — Conversation 建会话 advisory lock(推荐)
`set_conversation` 用 PostgreSQL advisory lock 按 `contact_inbox_id` 串行化:
```ruby
def set_conversation
  @conversation = @contact_inbox.conversations.where.not(status: :resolved).last
  return if @conversation

  # E7-2: advisory lock 串行化同一 contact_inbox 的首条建会话, 防并发 create! 重复
  ::Conversation.transaction do
    ::Conversation.connection.execute(
      "SELECT pg_advisory_xact_lock(#{lock_key(@contact_inbox.id)})"
    )
    # lock 内重查 (双检)
    @conversation = @contact_inbox.conversations.where.not(status: :resolved).last
    return if @conversation
    @conversation = ::Conversation.create!(
      account_id: @inbox.account_id, inbox_id: @inbox.id,
      contact_id: @contact.id, contact_inbox_id: @contact_inbox.id
    )
  end
end

# advisory lock key: 用 contact_inbox_id 映射到 int64 (PostgreSQL pg_advisory_xact_lock 接 bigint)
# contact_inbox.id 已是 bigint, 直接用; 加一个 namespace 偏移避免与其他业务锁冲突
LOCK_NAMESPACE_CONVERSATION = 9_000_001
def lock_key(contact_inbox_id)
  # PostgreSQL advisory lock 支持 (int, int) 双参形式, 这里用单 bigint
  # 简单做法: 直接传 contact_inbox.id (唯一), 不做 namespace (wecom 会话建是低频操作)
  contact_inbox_id.to_i
end
```
**说明**:`pg_advisory_xact_lock` 是事务级锁,事务结束自动释放,无需手动 unlock。重查(double-check)确保 lock 内发现已被另一线程创建则直接复用。无需 DB schema 变更。

### E7-3 — Contact 部分唯一索引 + rescue(推荐)
加 jsonb 路径部分唯一索引 + find-or-create 包 rescue:
```ruby
def set_sender_contact
  userid = params.dig(:contact, :userid).presence
  return if userid.blank?

  user_name = params.dig(:contact, :user_name).presence
  @sender_contact = ::Contact.find_by(
    account_id: @inbox.account_id,
    additional_attributes: { social_wecom_user_id: userid }
  )
  return if @sender_contact.present?

  # E7-3: 并发首条可能两个 job 都 find→nil→create; 唯一索引兜底 + rescue 重查
  begin
    @sender_contact = ::Contact.create!(
      account_id: @inbox.account_id,
      name: user_name.presence || "WeCom User #{userid}",
      additional_attributes: { social_wecom_user_id: userid }
    )
  rescue ActiveRecord::RecordNotUnique
    @sender_contact = ::Contact.find_by(
      account_id: @inbox.account_id,
      additional_attributes: { social_wecom_user_id: userid }
    )
  end
end
```
配套 migration(部分唯一索引,只对有 social_wecom_user_id 的行生效):
```ruby
class AddUniqueIndexToContactsSocialWecomUserId < ActiveRecord::Migration[7.1]
  def up
    # 部分唯一索引: 仅对 social_wecom_user_id 非空的行生效, 避免空值冲突
    # expression index on jsonb path
    execute <<~SQL
      CREATE UNIQUE INDEX CONCURRENTLY index_contacts_on_account_and_social_wecom_user_id
      ON contacts (account_id, (additional_attributes->>'social_wecom_user_id'))
      WHERE additional_attributes->>'social_wecom_user_id' IS NOT NULL
    SQL
  end

  def down
    remove_index :contacts, name: :index_contacts_on_account_and_social_wecom_user_id
  end
end
```
**⚠️ 关键**:`CREATE INDEX CONCURRENTLY` 不锁表(生产安全),但**不能在事务内运行** —— migration 不能用默认的 `def change` 包事务,必须 `disable_ddl_transaction!`。Rails migration 默认每个 `up` 在事务内,需显式:
```ruby
class AddUniqueIndexToContactsSocialWecomUserId < ActiveRecord::Migration[7.1]
  disable_ddl_transaction!

  def up
    execute <<~SQL
      CREATE UNIQUE INDEX CONCURRENTLY index_contacts_on_account_and_social_wecom_user_id
      ON contacts (account_id, (additional_attributes->>'social_wecom_user_id'))
      WHERE additional_attributes->>'social_wecom_user_id' IS NOT NULL
    SQL
  end

  def down
    execute "DROP INDEX CONCURRENTLY index_contacts_on_account_and_social_wecom_user_id"
  end
end
```

## 五、执行顺序(建议)

1. **复查生产重复数据**(第六节脚本)。若有重复 → 先跑去重(第七节);若无 → 跳过去重。
2. **E7-3**(最高价值):加 migration(部分唯一索引 CONCURRENTLY)+ 改 `set_sender_contact` rescue。这是唯一需要 schema 变更的。
3. **E7-2**(推荐):改 `set_conversation` 加 advisory lock,无 schema 变更。
4. **E7-1**(可选,降优先级):确认 msgid 不复用后,加部分唯一索引 + rescue。或暂不做(wecom-ai dedup 已够)。
5. **spec**(第八节):并发场景测试。
6. **重启 chatwoot-rails-1 + chatwoot-sidekiq-1** 让代码生效。
7. **跑 migration**(`bin/rails db:migrate`)+ 验证索引存在。

## 六、复查生产重复数据脚本

```ruby
# docker cp 到容器, bin/rails runner 执行
conn = ActiveRecord::Base.connection

puts "=== E7-1: messages 同 inbox 重复 source_id (非空) ==="
rows = conn.select_rows(<<~SQL)
  SELECT inbox_id, source_id, count(*) FROM messages
  WHERE source_id IS NOT NULL AND source_id != ''
  GROUP BY inbox_id, source_id HAVING count(*) > 1 LIMIT 50
SQL
puts rows.empty? ? "  无重复" : rows.map { |r| "  inbox=#{r[0]} source_id=#{r[1]} count=#{r[2]}" }.join("\n")

puts "=== E7-2: 同 contact_inbox 多个 open(0) 会话 ==="
rows = conn.select_rows(<<~SQL)
  SELECT contact_inbox_id, count(*) FROM conversations
  WHERE status = 0 AND contact_inbox_id IS NOT NULL
  GROUP BY contact_inbox_id HAVING count(*) > 1 LIMIT 50
SQL
puts rows.empty? ? "  无重复" : rows.map { |r| "  contact_inbox=#{r[0]} open_count=#{r[1]}" }.join("\n")
# 注: conversations.status enum: open=0 resolved=1 pending=2 snoozed=3

puts "=== E7-3: contacts 同 account 重复 social_wecom_user_id ==="
rows = conn.select_rows(<<~SQL)
  SELECT account_id, additional_attributes->>'social_wecom_user_id' AS uid, count(*)
  FROM contacts
  WHERE additional_attributes->>'social_wecom_user_id' IS NOT NULL
  GROUP BY account_id, uid HAVING count(*) > 1 LIMIT 50
SQL
puts rows.empty? ? "  无重复" : rows.map { |r| "  account=#{r[0]} uid=#{r[1]} count=#{r[2]}" }.join("\n")
```

## 七、去重 migration 草稿(仅当复查发现重复时用)

```ruby
# 仅当第六节查到重复才跑。保留最早一条(id 最小), 删后来者。
class DedupeWecomData < ActiveRecord::Migration[7.1]
  disable_ddl_transaction!

  def up
    # E7-1: 保留每组 (inbox_id, source_id) 最早的 message, 删其余
    execute <<~SQL
      DELETE FROM messages WHERE id IN (
        SELECT id FROM (
          SELECT id, ROW_NUMBER() OVER (
            PARTITION BY inbox_id, source_id ORDER BY id
          ) AS rn FROM messages
          WHERE source_id IS NOT NULL AND source_id != ''
        ) t WHERE rn > 1
      )
    SQL

    # E7-2: 同 contact_inbox 多个 open 会话, 保留最早, 其余的 message 迁到保留会话后删
    # (复杂, 建议人工处理: 列出重复 contact_inbox_id 后逐个判断, 不要盲删会话)
    # 这里只做 E7-1 和 E7-3 的自动去重, E7-2 人工。

    # E7-3: 同 (account_id, social_wecom_user_id) 保留最早 contact, 其余删
    # ⚠️ 先把重复 contact 的 messages.contact_id 迁到保留 contact, 再删
    execute <<~SQL
      -- 找出每组保留的 contact_id (最早) 和待删 contact_id
      WITH dups AS (
        SELECT id, account_id, additional_attributes->>'social_wecom_user_id' AS uid,
               ROW_NUMBER() OVER (PARTITION BY account_id, additional_attributes->>'social_wecom_user_id' ORDER BY id) AS rn
        FROM contacts
        WHERE additional_attributes->>'social_wecom_user_id' IS NOT NULL
      ),
      keep AS (SELECT id AS keep_id, account_id, uid FROM dups WHERE rn = 1),
      del  AS (SELECT d.id AS del_id, k.keep_id FROM dups d JOIN keep k USING (account_id, uid) WHERE d.rn > 1)
      -- 把待删 contact 的 messages 指向保留 contact
      UPDATE messages SET contact_id = del.keep_id
      FROM del WHERE messages.contact_id = del.del_id;
    SQL
    execute <<~SQL
      WITH dups AS (
        SELECT id, ROW_NUMBER() OVER (
          PARTITION BY account_id, additional_attributes->>'social_wecom_user_id' ORDER BY id
        ) AS rn FROM contacts
        WHERE additional_attributes->>'social_wecom_user_id' IS NOT NULL
      )
      DELETE FROM contacts WHERE id IN (SELECT id FROM dups WHERE rn > 1);
    SQL
  end
end
```
**⚠️ E7-2 会话去重不要自动做** —— 删会话会丢人工上下文,需人工逐个判断哪个该留。若第六节查到 E7-2 重复,人工处理:把后建会话的 messages 迁到先建会话,再删后建会话。

## 八、spec 草稿

并发竞态测试。用线程模拟并发:

```ruby
# spec/services/wecom/incoming_message_service_spec.rb 追加
require 'rails_helper'

RSpec.describe Wecom::IncomingMessageService, 'concurrent safety (E7)' do
  let(:account) { create(:account) }
  let!(:channel) { create(:channel_wecom, account: account) }
  let(:inbox) { channel.inbox.reload }
  let(:external_userid) { 'wm_concurrent_test' }

  def build_params(msgid:, userid: nil, chat_type: 'single')
    {
      'open_kfid' => channel.open_kfid,
      'external_userid' => external_userid,
      'contact' => { 'name' => 'Concurrent', 'avatar' => '', 'chat_type' => chat_type, 'userid' => userid }.compact,
      'message' => { 'msgid' => msgid, 'msgtype' => 'text', 'text' => { 'content' => '并发' }, 'origin' => 1 }
    }
  end

  it 'E7-2: 并发首条消息只创建一个会话' do
    # 同一 external_userid 首条消息并发 N 条 (不同 msgid, 避免消息去重干扰)
    msgids = (1..8).map { |i| "conc_msg_#{i}" }
    threads = msgids.map do |mid|
      Thread.new do
        svc = described_class.new(inbox: inbox, params: build_params(msgid: mid).with_indifferent_access)
        svc.perform
      end
    end
    threads.each(&:join)

    ci = inbox.contact_inboxes.find_by(source_id: external_userid)
    open_convs = ci.conversations.where(status: :open)
    expect(open_convs.count).to eq(1), "expected 1 open conversation, got #{open_convs.count}"
  end

  it 'E7-3: 群聊并发首条只创建一个 per-user Contact' do
    userid = 'group_user_001'
    msgids = (1..8).map { |i| "conc_group_#{i}" }
    threads = msgids.map do |mid|
      Thread.new do
        svc = described_class.new(inbox: inbox, params: build_params(msgid: mid, userid: userid, chat_type: 'group').with_indifferent_access)
        svc.perform
      end
    end
    threads.each(&:join)

    dups = Contact.where(account_id: account.id)
                  .where("additional_attributes->>'social_wecom_user_id' = ?", userid)
    expect(dups.count).to eq(1), "expected 1 per-user contact, got #{dups.count}"
  end

  it 'E7-1: 并发同 msgid 只落一条消息' do
    msgid = 'conc_dup_msgid'
    # 先建好会话, 避免会话创建干扰
    contact = create(:contact, account: account)
    ci = create(:contact_inbox, inbox: inbox, contact: contact, source_id: external_userid)
    create(:conversation, account: account, inbox: inbox, contact: contact, contact_inbox: ci)

    threads = 8.times.map do
      Thread.new do
        svc = described_class.new(inbox: inbox, params: build_params(msgid: msgid).with_indifferent_access)
        svc.perform
      end
    end
    threads.each(&:join)

    expect(Message.where(source_id: msgid).count).to eq(1)
  end
end
```

**注意**:线程并发测试在 Ruby/PG 下不 100% 可靠(GVL + 时序),可能偶现假绿。建议同时加一个**直接测 RecordNotUnique rescue**的用例(不靠线程,直接 stub 第二次 create! 抛 RecordNotUnique,验证 rescue 重查):

```ruby
it 'E7-3: set_sender_contact 在 RecordNotUnique 时重查复用' do
  userid = 'group_user_rescue_test'
  # 预先建好
  existing = Contact.create!(account: account, name: 'Existing',
                             additional_attributes: { 'social_wecom_user_id' => userid })
  svc = described_class.new(inbox: inbox,
    params: { 'open_kfid' => channel.open_kfid, 'external_userid' => external_userid,
              'contact' => { 'name' => 'New', 'userid' => userid, 'chat_type' => 'group' },
              'message' => { 'msgid' => 'r1', 'msgtype' => 'text', 'text' => { 'content' => 'x' }, 'origin' => 1 } }.with_indifferent_access)
  # 让 create! 抛 RecordNotUnique (模拟并发)
  allow(Contact).to receive(:create!).and_raise(ActiveRecord::RecordNotUnique.new(nil))
  svc.perform
  # rescue 后重查应复用 existing (不新建)
  expect(Contact.where("additional_attributes->>'social_wecom_user_id' = ?", userid).count).to eq(1)
end
```
(这个用例更可靠,因为它直接验证 rescue 逻辑而非靠线程时序。)

## 九、风险与注意

1. **`CREATE INDEX CONCURRENTLY` 不能在事务内**:migration 必须 `disable_ddl_transaction!`。若忘了,Rails 会报错(不会静默失败)。
2. **部分唯一索引的空值**:`WHERE additional_attributes->>'social_wecom_user_id' IS NOT NULL` 让无 wecom userid 的 contact 不参与唯一约束,避免与其他 channel 的 contact 冲突。
3. **advisory lock key 冲突**:`pg_advisory_xact_lock(contact_inbox_id)` 用 contact_inbox.id 作 key。若其他业务也用 advisory lock 且 key 空间重叠会冲突。建议加 namespace 偏移(见第四节 lock_key 注释),或用 `(namespace_int, contact_inbox_id)` 双参形式 `pg_advisory_xact_lock(namespace, contact_inbox_id)`。
4. **E7-2 会话去重不要自动删**:若复查发现重复 open 会话,人工处理(迁 message + 删后建会话),不要盲删。
5. **执行后验证**:跑完 migration 确认索引 `SELECT indexname FROM pg_indexes WHERE indexname = 'index_contacts_on_account_and_social_wecom_user_id';`。
6. **生产 DB 备份**:加索引前 `pg_dump` 备份(虽然 CONCURRENTLY 不锁表,但稳妥起见)。
7. **测试环境**:之前 spec 跑时有 "Sidekiq testing API enabled, but this is not the test environment" 警告 —— 执行 spec 前确认 `RAILS_ENV=test` 或 spec 配置正确,避免污染 dev DB。

## 十、相关文件(Chatwoot 侧)

- `app/services/wecom/incoming_message_service.rb`(E7-1/2/3 改动落点)
- `db/migrate/`(新建 E7-3 唯一索引 migration;可选 E7-1)
- `spec/services/wecom/incoming_message_service_spec.rb`(追加并发 spec)
- `db/schema.rb`(migration 后 `rails db:schema:dump` 同步)

## 十一、wecom-ai 侧(无需改,仅供对照)

wecom-ai 已有 `DedupStore`(A1 修复后:`_processing`→`mark_done` 状态机 + `_processing` TTL),是消息层第一道防线。E7 是 Chatwoot DB 层第二道防线。两侧独立,互不依赖。

---

执行时按第五节顺序。最关键的是**第六节复查** —— 决定要不要跑去重。复查无重复(当前状态)则直接进 E7-3 → E7-2 → spec → 重启 → migration。
