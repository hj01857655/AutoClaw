# AutoClaw2-02 IPC 面

状态：A（实测）。preload 与主进程 bundle 均保留符号名，通道常量可直接读取。

## 总体形态

15 个 `contextBridge.exposeInMainWorld` API 族（preload `zwork-bootstrap-preload.cjs`）：

```
zworkLocalPerformance  zworkDesktop           zworkDesktopBrowser    zworkDesktopLifecycle
zworkSelectionAnnotation  zworkDesktopNotificationActivation  zworkAuth  zworkWorkbenchCapabilities
zworkAutoclawMigration zworkAbtest            zworkAnalytics         zworkProductLanguage
zworkLogging           zworkAgent             zworkUpdate
```

主进程 `ipcMain.handle` 仅 18 处 —— IPC 主体不是一通道一业务，而是**少数总通道 + 按 request.type 判别分发**。

## 通道常量（主进程实测值）

| 常量 | 值 |
|---|---|
| ZWORK_UPDATE_INVOKE | `zwork:update:invoke` |
| ZWORK_DESKTOP_INVOKE_CHANNEL | `zwork:desktop:invoke` |
| ZWORK_DESKTOP_CAPABILITIES_INVOKE_CHANNEL | `zwork:desktop:capabilities:invoke` |
| ZWORK_DESKTOP_BROWSER_INVOKE_CHANNEL | `zwork:desktop:browser:invoke` |
| ZWORK_DESKTOP_RESTART_APPLICATION_CHANNEL | `zwork:desktop:restart-application` |
| ZWORK_AUTH_INVOKE_CHANNEL | `zwork:auth:invoke` |
| ZWORK_AUTH_SESSION_CHANNEL | `zwork:auth:session` |
| ZWORK_ANALYTICS_TRACK_CHANNEL | `zwork:analytics:track` |
| ZWORK_ABTEST_GET_VAR_CHANNEL | （常量名见 bundle，值待抄录） |
| ZWORK_PRODUCT_LANGUAGE_INVOKE_CHANNEL | `zwork:product-language:invoke` |
| ZWORK_AUTOCLAW_DETECT_CHANNEL | `desktop.autoclaw_detect` |
| ZWORK_AUTOCLAW_IMPORT_CHANNEL | `desktop.autoclaw_import` |
| ZWORK_AUTOCLAW_HINT_CHANNEL | `desktop.autoclaw_migration_hint` |
| ZWORK_AGENT_CHANNELS | invoke=`zwork:agent:invoke`, query=`zwork:agent:query`, subscribe=`zwork:agent:subscribe`, unsubscribe=… |

## desktop invoke 总分发器

`zwork:desktop:invoke` 一条 handler 内串联判别链，先命中先处理：

```
handleZworkDesktopAttachmentRequest        (draft_attachment* / run_attachment_preview.* / trusted attachment actions)
handleZworkDesktopGeneralSettingsRequest
handleZworkDesktopModelConnectionRequest
handleZworkDesktopProjectDirectoryRequest
handleZworkDesktopWorkspaceFilesRequest
handleZworkDesktopSkillStoreRequest        (skill_store.*)
handleZworkDesktopLegalReferenceRequest
integrations.*                             (OAuth start/cancel)
```

信任判定：`isTrustedZworkRendererRequest(options, event)` + `trustedZworkRendererBindingForEvent`（绑定 sender 到主窗口/顶层 frame/精确 URL）。附件上传走 sender 绑定的 uploadHandle 流（begin/append/finish/abort）。

## agent 协议词汇表（type 字面量全集，109 条）

来自 main.cjs `type: "..."` 字面量（判别 union 的 wire vocabulary）：

请求/命令类：
```
response.create  run.retry  run.cancel  run.stop  run.settled  run.guide
session.send  session.composer.drafted
approval.resolve  question.answered  user_question.answer
memory.propose  memory.proposal.decide  memory.delete  memory.revoke
agent.create  agent.update_profile  agent.memory.upsert  agent.memory.delete
agent.preset.create  agent.preset.update  agent.preset.delete  agent.usage.set
collaboration.agent.create  collaboration.agent.update_profile  collaboration.mode
model_connection.activate  model_connection.configure  model_connection.retest
scheduled_task.create
tool.cancel  guide.requested
```

事件/推送类（agent → UI）：
```
run.accepted  run.queued  run.started  run.completed  run.failed  run.interrupted
run.cancelled  run.stopped  run.stop_requested  run.retried  run.merged  run.terminal
run.collaboration_degraded  run.efficiency.updated  run.usage.updated  run.events
block.started  block.delta  block.completed
assistant.output.updated  assistant.reasoning.updated
tool.requested  tool.request  tool.started  tool.progress  tool.finished  tool.permission_required
tool_activity.bound  participant.narration  participant.tool_activity
model.request  model.request.started  model.request.activity  model.request.completed  model.response
compaction.started  compaction.completed  compaction.failed
retry.started  retry.recovered
approval.requested  approval.resolved  approval.expired
question.requested  question.cancelled  question.settled  question.answered
guide.delivered  guide.returned
memory.projection_recorded  memory.proposal_requested
session.activity  session.opened  session.restored  session.archived  session.deleted
session.titled  session.send  session.composer.drafted
session.origin.recorded  session.route.changed  session.external_route.recorded
orchestration.dispatched  orchestration.rejected  orchestration.wait
work_plan.proposed  work_plan.settled  run_checklist.updated
workflow.lifecycle  workflow.plan.snapshot  workflow.task.not_started
scheduled_task.created  egress.decision  trace.file  trace.meta  trace.redaction
model_connection.catalog_changed  im.connection.changed
```

解读：
- 这就是 desktop UI ↔ 本地 agent 运行时的完整会话协议，粒度到 block.delta / tool.progress。
- `egress.decision`（出网决策事件）与 `trace.redaction`（trace 脱敏）说明 agent 运行时有出网治理与日志脱敏层，详见 04。
- 协议请求进入前先过 zod schema parse（`ZworkDesktopRequestSchema.parse(input)` 等）。

## 未决问题

- `zwork:desktop:capabilities:invoke` 与 `zwork:desktop:browser:invoke` 的请求词汇表（尚未抄录）。
- `ZWORK_ABTEST_GET_VAR_CHANNEL` 实际值。
- `zwork:agent:*` 通道背后的本地传输（WebSocket? 直连 agent 进程?）——见 04。
