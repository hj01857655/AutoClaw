# AutoClaw2-07 持久化层：runtime.sqlite 账本

状态：A（实测 schema DDL）。存储位置：`<runtimeDataDirectory>/runtime.sqlite`（node:sqlite `DatabaseSync`，快照用 `VACUUM INTO`，恢复走 staged `.restore-<uuid>.sqlite` → 原子 rename，sidecar 归档）。

## 表全集（120 张，实测去重）

**身份/账号**：identity_personal_principals、identity_profiles、account_connector_grants、account_connector_services、account_connector_credential_cleanup、model_connections、model_connection_commands、model_connection_credential_cleanup、zwork_permission_defaults

**工作区/项目**：work_workspaces、work_workspace_folders、work_workspace_capabilities、work_workspace_vertical_packs、work_workspace_initializations、work_project_initialization_journal、workspace_file_restore_entries、scheduled_automation_workspaces

**会话/运行**：work_sessions、work_session_drafts、work_runs、work_commands、work_events、work_guide_requests、work_notifications、work_notification_deliveries、work_preferences、neutral_session_entries/heads/working_entries/working_chunks、session_documents、session_connector_selections、work_session_agent_loop_selections(_next)、run_connector_snapshots、run_tool_intents、run_remote_placements

**多 agent 协作**：collaboration_agents、collaboration_agent_tombstones、collaboration_agent_profile_revisions、collaboration_agent_starter_seedings、collaboration_run_*（expert/agent catalogs、participations v8/v10、attempts v10、admission commands/rejections、stop commands、temporary agent catalog v34）

**Memory**：agent_memory_snapshots、memory_assets、agent_identity_asset_facts

**定时任务**：scheduled_tasks(_v38)、scheduled_occurrences(_v38)、scheduled_occurrence_attempts_v38、scheduled_registered_programs、scheduled_program_evidence_receipts、scheduled_program_run_results、scheduled_relay_devices、scheduled_relay_tickets

**IM 通道**：im_delivery_outbox(_0020)、im_inbound_receipts、im_integration_workspaces、im_model_target_provenance、im_owner_bindings、im_project_card_bindings、im_target_resolutions、im_work_requests、im_work_request_transitions、im_attachment_staging_intents、work_im_bot_settings、work_im_guest_allowances、work_im_guest_pairings、work_agent_im_bindings

**技能执行**：executable_skill_environments、executable_skill_environment_references、executable_skill_grants、executable_skill_processes、executable_skill_setup_processes、owner_imported_capability_bundles(_v54)、autoclaw_session_imports

**治理（governed_）**：governed_connected_sources(_revisions)、governed_folder_sources(_revisions)、governed_project_instruction_revisions、governed_tool_revisions、governed_tool_artifact_custody、governed_tool_session_approval_rules、declarative_resource_root_approvals

**其他**：agents、agent_presets、agent_members、schema_migrations

## 关键表结构（实测 DDL 摘录）

**work_sessions**：session_id PK、title、draft、owner_id→identity_personal_principals、workspace_id→work_workspaces、visibility、binding_origin/version、archived_at、external_provider/external_conversation_id/external_actor_id/external_connection_id（IM 外部会话绑定）、dispatch_origin_provider/session_id、`composer_permission_mode CHECK IN ('request_approval','approve_risky','full_access')`。
唯一索引：(owner, workspace, session)、(owner, external_provider, external_connection_id, external_conversation_id)、(dispatch_origin_provider, dispatch_origin_session_id, workspace)。

**work_workspaces**：workspace_id PK、owner_id、display_name、root_path、is_default、lifecycle(active/archived/deleted)、`trust_status CHECK IN ('trusted','untrusted')`。

解读：
- 三档权限模式与 Claude Code 的 permission mode 同构；`approve_risky` 是中间档。
- workspace 的 trusted/untrusted 分级 + governed_* 修订表说明资源治理（工具修订、审批规则、artifact 保管）全部入库可审计。
- `_v8/_v10/_v34/_v38/_v54` 后缀 = 重建式迁移痕迹（rename→create→copy→drop）。
- neutral_session_* 是与 workspace 解耦的「中立会话」存储（working_entries/chunks 拆分，支持大内容分块）。

## 账本运维

- 快照：`VACUUM INTO snapshots/<rev>/runtime.sqlite`；按 schema 版本探测可恢复快照。
- 恢复：staged ledger + sidecar(-wal/-shm) 原子换入，失败回滚 rename。
- `probeLedgerFile` 打开头部探测 schema 版本。

## 未决问题

- work_events / neutral_session_entries 的完整列（消息内容存储格式）。
- runtimeDataDirectory 的实际位置（推测 %APPDATA%/zwork 或 userData 下，未确认）。
- governed_tool_session_approval_rules 的规则结构（权限规则语法）。
