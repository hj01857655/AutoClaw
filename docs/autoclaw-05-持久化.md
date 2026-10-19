# AutoClaw 逆向分析（五）持久化层

2026-10-19 · 基于 **2.1.0** 实测（`runtime.sqlite` 直接查表，非静态阅读；旧文档基于 2.0.1）

## 一、数据库位置与规模

```
%APPDATA%/AutoClaw-official/accounts/<sha256(accountId)>/runtime/runtime.sqlite
```

实测（本机）：

| 项 | 值 |
|---|---|
| 表数 | **96** |
| 迁移记录 | 9 条（`0001_clean_dogfood_baseline` → `0079_run_remote_origin`） |
| 最新迁移时间 | 2026-10-19T07:18:31 |
| 索引 | `work_runs` 7 个、`scheduled_occurrences` 7 个、`scheduled_tasks` 5 个 |

**不是空库**：`neutral_session_entries` 653 行、`work_events` 652 行、`work_commands` 164 行，可作行为证据。

## 二、迁移体系

`schema_migrations` 表结构（实测）：

```
(id TEXT, applied_at TEXT, version INTEGER, checksum TEXT)
```

```
0001_clean_dogfood_baseline                    2026-09-29T01:50:33.294Z   72
0073_im_open_connection_receipts               2026-10-19T07:18:31.045Z   73
0074_starter_agent_localization_source         2026-10-19T07:18:31.066Z   74
0075_im_dispatch_retirement                    2026-10-19T07:18:31.143Z   75
0076_im_group_dual_form                        2026-10-19T07:18:31.161Z   76
0077_executable_skill_retirement               2026-10-19T07:18:31.165Z   77
0078_agent_attempt_usage                       2026-10-19T07:18:31.174Z   78
0079_run_remote_origin                         2026-10-19T07:18:31.184Z   79
```

**要点**：
- 每条迁移带 **SHA-256 checksum**（防篡改/防顺序错乱）
- 版本号从 72 起跳（说明基线之前有 71 条，被压进 `0001_clean_dogfood_baseline`）
- 0073–0079 七条在同一次启动的 **100ms 内**全部应用，说明 2.1.0 相对 2.0.1 有 7 条新迁移

## 三、表全集（96 张，按域归类）

### 3.1 会话与运行时（核心）
```
work_sessions          work_session_drafts      work_runs
work_events            work_commands            work_preferences
work_notifications     work_notification_deliveries
work_guide_requests    work_project_initialization_journal
neutral_session_entries      neutral_session_heads
neutral_session_working_entries   neutral_session_working_chunks
neutral_session_fts (+ _config/_content/_data/_docsize/_idx)
session_documents      session_connector_selections
owner_imported_capability_bundles    autoclaw_session_imports
```

### 3.2 模型连接
```
model_connections                    model_connection_commands
model_connection_credential_cleanup
run_connector_snapshots              run_tool_intents
run_remote_placements
```

### 3.3 记忆
```
memory_assets
memory_fts (+ _config/_data/_docsize/_idx)
agent_memory_snapshots
```

### 3.4 Agent 与协作
```
agents                 agent_members            agent_presets
agent_identity_asset_facts
collaboration_agent_starter_seedings          collaboration_agent_tombstones
collaboration_run_agent_admission_commands    collaboration_run_agent_admission_rejections
collaboration_run_agent_attempts              collaboration_run_agent_catalogs
collaboration_run_agent_participations        collaboration_run_agent_stop_commands
collaboration_run_expert_catalog_entries      collaboration_run_expert_participations
collaboration_run_saved_agent_catalog_entries collaboration_run_temporary_agent_catalog_entries
```

### 3.5 工作区与文件
```
work_workspaces        work_workspace_folders   work_workspace_initializations
work_workspace_vertical_packs                 workspace_file_restore_entries
declarative_resource_root_approvals
```

### 3.6 受管资源（governed）
```
governed_connected_sources           governed_connected_source_revisions
governed_folder_sources              governed_folder_source_revisions
governed_project_instruction_revisions
governed_tool_approvals              governed_tool_artifacts
governed_tool_artifact_custody       governed_tool_revisions
governed_tool_session_approval_rules
```

### 3.7 身份与账号连接器
```
identity_personal_principals         identity_profiles
account_connector_services           account_connector_grants
account_connector_credential_cleanup
```

### 3.8 IM
```
work_agent_im_bindings    work_im_bot_settings    work_im_guest_allowances
work_im_guest_pairings    im_attachment_staging_intents
im_delivery_outbox        im_group_context_buffer  im_inbound_receipts
im_integration_workspaces im_owner_bindings
```

### 3.9 定时任务（scheduled）
```
scheduled_tasks                  scheduled_occurrences
scheduled_occurrence_attempts    scheduled_registered_programs
scheduled_program_run_results    scheduled_program_evidence_receipts
scheduled_automation_workspaces  scheduled_relay_devices
scheduled_relay_tickets
```

### 3.10 其他
```
zwork_permission_defaults        schema_migrations       sqlite_sequence
```

## 四、关键表 DDL（实测摘录）

### 4.1 `neutral_session_entries` —— 会话消息的唯一真相

```sql
CREATE TABLE neutral_session_entries (
  session_id      TEXT NOT NULL REFERENCES work_sessions(session_id) ON DELETE CASCADE,
  entry_id        TEXT NOT NULL,
  parent_entry_id TEXT,
  session_seq     INTEGER NOT NULL CHECK(session_seq >= 1),
  run_id          TEXT NOT NULL,
  entry_type      TEXT NOT NULL CHECK(entry_type IN (
    'request', 'attachment', 'reasoning', 'assistant', 'resource',
    'tool_call', 'tool_result', 'compaction', 'answer'
  )),
  payload         TEXT NOT NULL CHECK(json_valid(payload)),
  committed_at    TEXT NOT NULL,
  PRIMARY KEY (session_id, entry_id),
  UNIQUE (session_id, session_seq),
  FOREIGN KEY (session_id, parent_entry_id)
    REFERENCES neutral_session_entries(session_id, entry_id) DEFERRABLE INITIALLY DEFERRED
)
```

**三个设计要点**：
1. **自引用外键** `parent_entry_id` → 支持消息树（fork / 分支），`DEFERRABLE INITIALLY DEFERRED` 允许批内乱序插入
2. **`entry_type` 九种枚举**，`reasoning` 与 `answer` 是独立类型（比很多实现把思考塞进 assistant 文本更干净）
3. **`session_seq` 唯一约束**——同一会话内严格有序

### 4.2 `work_events` —— 事件流（UUID v4 强校验）

```sql
CREATE TABLE work_events (
  cursor     INTEGER PRIMARY KEY AUTOINCREMENT,
  event_id   TEXT UNIQUE NOT NULL CHECK(
    length(event_id) = 36 AND event_id NOT GLOB '*[^0-9a-fA-F-]*'
    AND substr(event_id, 9, 1)  = '-'
    AND substr(event_id, 14, 1) = '-'
    AND lower(substr(event_id, 15, 1)) = '4'                  -- UUID v4
    AND substr(event_id, 19, 1) = '-'
    AND lower(substr(event_id, 20, 1)) IN ('8','9','a','b')   -- variant
    AND substr(event_id, 24, 1) = '-'
  ),
  run_id     TEXT NOT NULL CHECK(...同款 UUID 校验...),
  session_id TEXT NOT NULL CHECK(...同款 UUID 校验...),
  run_seq    INTEGER NOT NULL,
  type       TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  created_at TEXT NOT NULL
)
```

**在 SQL CHECK 里内联 UUID v4 格式校验**（第 15 位必须是 `4`，第 20 位必须是 `8/9/a/b`）——这不是常见做法，但很有效：任何非 v4 UUID 在写入时就被拒绝。同一段 CHECK 在 6 张表的 3 个字段上重复了 6 次。

### 4.3 `work_runs` —— 状态机与连接快照

```sql
status TEXT NOT NULL CHECK(status IN (
  'queued', 'running', 'waiting_approval', 'completed', 'failed',
  'interrupted', 'stopping', 'stopped', 'cancelled', 'merged'
))

-- 每条 run 冗余存放当时的连接参数（快照式，防配置漂移）
connection_id, connection_provider, connection_model, connection_revision,
credential_version, connection_base_url, connection_protocol,
connection_route_revision, connection_supports_images
```

`answer_preview` 有 **240 字符上限** CHECK——列表页只读这份预览，不读全量。

### 4.4 `agents`

```sql
agent_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL,
name/normalized_name TEXT NOT NULL,
composition_json TEXT CHECK(composition_json IS NULL OR json_valid(composition_json)),
starter_name_source TEXT CHECK(starter_name_source IN ('factory','user')),
starter_description_source TEXT CHECK(...),
FOREIGN KEY(owner_id) REFERENCES identity_personal_principals(principal_id)
```

`starter_*_source` 记录名称/描述来自出厂还是用户改过——对应 `agent.update_profile` 里注释说的「省略时内核按本次字段是否改变兼容旧调用方」。

### 4.5 `work_commands`

```sql
command_id TEXT PRIMARY KEY,
payload_hash TEXT NOT NULL,
payload_json TEXT NOT NULL,
ack_json  TEXT NOT NULL,
owner_id  TEXT REFERENCES identity_personal_principals(principal_id),
created_at TEXT NOT NULL
```

**命令有 `payload_hash` + `ack_json`**，是幂等/去重的实现基础（同 hash 的命令不重放）。

## 五、实际运行数据（本机实测）

### 5.1 事件类型分布（`work_events` 652 行）

| type | 条数 | 说明 |
|---|---|---|
| `tool_activity.bound` | **414** | 工具活动绑定，占 63% |
| `egress.decision` | **168** | **出站决策审计**，占 26% |
| `run.queued` | 11 | |
| `run.accepted` | 10 | |
| `run_checklist.updated` | 9 | |
| `run.started` | 9 | |
| `collaboration.mode` | 9 | |
| `run.failed` | 7 | |
| `guide.requested` | 3 | |
| `run.stopped` / `run.stop_requested` | 2 / 2 | |
| `run.merged` | 2 | |
| `work_plan.settled` / `work_plan.proposed` | 1 / 1 | |
| `run.retried` | 1 | |

**`egress.decision` 是重要发现**：168 条出站决策记录，说明每次对外请求（可能含模型调用与连接器调用）都留审计。这对分析"哪些数据出去了"极有价值。

### 5.2 非空表（本机）

| 表 | 行数 | 说明 |
|---|---|---|
| `neutral_session_entries` | 653 | 会话消息 |
| `work_events` | 652 | 事件流 |
| `work_commands` | 164 | 命令日志 |
| `collaboration_run_saved_agent_catalog_entries` | 77 | agent 目录 |
| `governed_tool_revisions` | 47 | 工具版本 |
| `governed_tool_artifacts` / `_custody` | 31 / 31 | 产物与保管 |
| `workspace_file_restore_entries` | 12 | 文件恢复点 |
| `collaboration_run_agent_catalogs` | 11 | |
| `run_connector_snapshots` | 11 | 连接器快照 |
| `work_runs` | 11 | |
| `agents` | 8 | |
| `work_preferences` | 8 | |
| `agent_members` | 7 | |
| `model_connections` | **4** | 模型连接 |
| `memory_fts` | 9 | 记忆索引 |

## 六、与 2.0.1 的差异

旧文档记录 120 张表；2.1.0 实测 **96 张**。差异原因有二：

1. 统计口径不同（旧文档可能把 FTS 影子表、索引都算进来了）
2. **0073–0079 七条新迁移可能带来表结构调整**（`0075_im_dispatch_retirement`、`0077_executable_skill_retirement` 名称里带 retirement，说明有表被废弃）

**不能照抄旧表清单**——本篇所有表名均来自 `sqlite_master` 直接查询。

## 七、安全与审计观察

1. **`egress.decision` 表（事件类型）**：出站决策被完整审计，是分析数据流向的入口
2. **UUID v4 强校验写在 CHECK 里**：异常数据在写入层就被拒绝，不留脏数据
3. **`work_runs` 冗余连接快照**：历史 run 不受后续配置变更影响，可复现
4. **`governed_tool_approvals` + `governed_tool_session_approval_rules`**：审批有会话级规则，与 run 级审批两层
5. **`model_connection_credential_cleanup`**：凭据清理有独立表跟踪，说明有删除时机的补偿机制

## 八、未决问题

- `neutral_session_working_entries` / `_working_chunks` 与 `neutral_session_entries` 的关系（工作副本 vs 提交副本？）
- `collaboration_run_temporary_agent_catalog_entries` 的生命周期
- `scheduled_relay_devices` / `scheduled_relay_tickets` 的中继协议
- `egress.decision` 的 payload 结构（决定能从这里挖出多少出站信息）
