# AutoClaw 逆向分析（三）IPC 面

2026-10-19 · 基于 **2.1.0** 实测（旧文档基于 2.0.1，本篇所有数据重新提取，未抄录）

## 一、preload 暴露面

`dist/zwork-bootstrap-preload.cjs`（1.33 MB）通过 `contextBridge.exposeInMainWorld` 暴露 **16 个**全局对象：

```
zworkLocalPerformance          zworkDesktop              zworkDesktopBrowser
zworkDesktopLifecycle          zworkSelectionAnnotation  zworkDesktopNotificationActivation
zworkAuth                      zworkWorkbenchCapabilities zworkAutoclawMigration
zworkAbtest                    zworkAnalytics            zworkProductLanguage
zworkLogging                   zworkRemoteControl        zworkAgent
zworkUpdate
```

注意 `zworkDesktop` 是**组合对象**，挂载时还会 `Object.assign` 补两个能力：

```js
const client = createZworkDesktopClient(invoke, shutdownFlush, resolveNativeFilePath, generalPreferencesEvents);
if (capabilitiesEvents) {
  Object.assign(client, createWorkbenchCapabilitiesDesktopClient(capabilitiesInvoke, capabilitiesEvents));
  Object.assign(client, createZworkDesktopSkillStoreChangeSubscription(capabilitiesEvents));
}
bridge.exposeInMainWorld("zworkDesktop", client);
```

## 二、IPC 通道全集（47 个）

preload 侧出现的全部 `zwork:*` 通道字面量，按域归类：

### 2.1 agent 域
```
zwork:agent:invoke          zwork:agent:query        zwork:agent:subscribe
zwork:agent:unsubscribe     zwork:agent:publication
```

### 2.2 auth 域
```
zwork:auth:invoke           zwork:auth:session
```

### 2.3 desktop 域（最大）
```
zwork:desktop:invoke                     zwork:desktop:shutdown-flush
zwork:desktop:capabilities               zwork:desktop:capabilities:invoke
zwork:desktop:browser:invoke             zwork:desktop:browser-state
zwork:desktop:sidebar-unread             zwork:desktop:restart-application
zwork:desktop:app-foreground-changed     zwork:desktop:app-foreground-get
zwork:desktop:general-preferences-changed
zwork:desktop:notification-activation
zwork:desktop:selection-annotation-request
zwork:desktop:dingtalk-scan-start        zwork:desktop:dingtalk-scan-poll
zwork:desktop:dingtalk-scan-cancel
zwork:desktop:wecom-scan-create          zwork:desktop:wecom-scan-cancel
```

### 2.4 其他
```
zwork:abtest:get-var              zwork:abtest:refresh
zwork:analytics:track             zwork:analytics:get-identity
zwork:analytics:startup-surface-ready
zwork:diagnostics:log             zwork:local-performance
zwork:product-language:invoke     zwork:product-language:changed
zwork:skill-store:changed
zwork:update:invoke               zwork:update:status
zwork:workbench-capabilities:get  zwork:workbench-capabilities:changed
zwork:autoclaw:import-applied
zwork:remote-control:enable       zwork:remote-control:disable
zwork:remote-control:status       zwork:remote-control:request-pair-code
zwork:remote-control:controllers:list    zwork:remote-control:controllers:approve
zwork:remote-control:controllers:remove
```

**只有 remote-control 那 7 个用 `ipcMain.handle` 直接注册**，其余全部走统一分发器（`invoke` + 请求体里的 `type` 字段）。这是本应用 IPC 设计的核心：**通道少、type 多**。

## 三、desktop invoke 分发器

`src/shared/zwork-desktop-contract.ts`：

```js
var ZWORK_DESKTOP_INVOKE_CHANNEL = "zwork:desktop:invoke";
var ZWORK_DESKTOP_SHUTDOWN_FLUSH_CHANNEL = "zwork:desktop:shutdown-flush";
var ZWORK_DESKTOP_GENERAL_PREFERENCES_CHANGED_CHANNEL = "zwork:desktop:general-preferences-changed";
```

主进程侧是一个大 `switch (request.type)`，实测 **25 个分支**：

```
restart                      kernel_repair.state          kernel_repair.perform
kernel_repair.report         desktop.confirm_action       desktop.set_theme_source
failure_feedback.submit      general_feedback.submit      general_feedback.health
promotion_config.load        invite_code.activity_status  invite_code.bind
notification_inbox.list      notification_inbox.read      input_safety.check
scheduled_task.autoclaw_preview
im_connection.save           im_connection.get            im_connection.action
tools_connection.invoke      oauth_connector.invoke
artifact_preview.inspect     artifact_preview.read        artifact_preview.close
artifact_preview.open_stream
```

契约 schema 里另有 5 个类型未进 switch（说明是响应侧或已废弃）：

```
model_credential.stage       model_credential.restage
model_connection.discover    model_connection.vision_hint  model_connection.probe
project_directory.pick       project_directory.provision_managed  project_directory.archived_path
shutdown_flush               relaunching  cancelled  rejected  available  unavailable
```

## 四、请求词汇全集（757 条 type 字面量）

全包 `literal("xxx")` 去重后 **757 条**。按命名空间分布（前 25）：

| 命名空间 | 条数 | 命名空间 | 条数 |
|---|---|---|---|
| run | 21 | im | 10 |
| response | 17 | scheduled_task | 9 |
| memory | 17 | browser | 9 |
| session | 13 | account | 8 |
| attachment | 13 | agent | 8 |
| collaboration | 13 | artifact | 7 |
| workspace | 12 | governed_tools | 6 |
| desktop | 12 | skill_store | 5 |
| model_connection | 11 | html_artifact | 5 |
| | | artifact_preview | 5 |

### 4.1 run 域（21）
```
run.accepted  run.cancel   run.cancelled  run.collaboration_degraded
run.completed run.context_profile_frozen  run.domain_engaged
run.efficiency.updated    run.failed     run.guide      run.interrupted
run.merged    run.queued   run.retried    run.retry      run.started
run.stop      run.stop_requested          run.stopped    run.usage.updated
```

### 4.2 session 域（13）
```
session.active.set   session.bootstrap   session.create   session.delete_with_memory
session.draft.save   session.fork        session.rename    session.restore_files
session.resume_queue session.send        session.subscribe session.titled
```

### 4.3 memory 域（17）
```
memory.activateSupersession   memory.conflict.resolve  memory.decideProposal
memory.delete                 memory.dismissPending    memory.projection_recorded
memory.proposal.create        memory.proposal.decide   memory.proposal.dismiss_pending
memory.proposal_requested     memory.propose            memory.proposeCorrection
memory.resolveConflict        memory.revoke             memory.save
memory.supersession.decide    memory.supersession.propose
```
注意：记忆有**提案—决策**模型（proposal → decide/dismiss），且支持**冲突消解**（conflict.resolve）与**取代**（supersession）。

### 4.4 workspace 域（12）
```
workspace.bind     workspace.create    workspace.folder.add   workspace.folder.remove
workspace.folder.set_primary            workspace.instruction.decide
workspace.instruction.propose           workspace.rename
workspace.repair   workspace.set_default workspace.trust
```

### 4.5 model_connection 域（11）
```
model_connection.activate          model_connection.candidate.save
model_connection.capability.update model_connection.catalog_changed
model_connection.configure         model_connection.delete
model_connection.discover          model_connection.probe
model_connection.retest            model_connection.vision_hint
```

### 4.6 agent 域（8）与 collaboration 域（13）
```
agent.create              agent.memory.delete      agent.memory.upsert
agent.preset.create       agent.preset.delete      agent.preset.update
agent.update_profile      agent.usage.set
```
`collaboration.*` 是同一批命令的**协作态镜像**（13 条，一一对应 agent.* 并多出 `mode`、`participant.stop`、`run.retry`、`run.stop`）。

### 4.7 browser 域（9）
```
browser.adopt   browser.back     browser.close   browser.control  browser.forward
browser.navigate browser.open    browser.refresh browser.release
```
`adopt` / `release` 说明它**接管**用户已有浏览器实例，而非自建。

### 4.8 artifact 族（导出能力）
```
artifact.export          artifact.export_pdf   artifact.export_png   artifact.export_pptx
artifact.open            artifact.reveal       artifact.source_stat
html_artifact.append     html_artifact.close   html_artifact.finalize
html_artifact.open_committed                  html_artifact.open_stream
artifact_preview.inspect artifact_preview.read artifact_preview.close
artifact_preview.close_stream                 artifact_preview.open_stream
```

### 4.9 governed_tools（受管工具，6）
```
governed_tools.approval.resolve   governed_tools.question.answer
governed_tools.run.cancel         governed_tools.run.retry   governed_tools.run.stop
```

### 4.10 其他
```
account.avatar.generate  account.credits.redeem  account.delete.confirm
account.delete.notice    account.privacy.read    account.privacy.update
account.profile.get      account.sign_out

im.binding.set  im.bot.settings.set  im.connection.changed  im.connection.unbind
im.guest.allowance.approve  im.guest.allowance.dismiss  im.guest.allowance.revoke
im.guests.disable_all  im.inbox.delete  im.session.end

scheduled_task.create  scheduled_task.created  scheduled_task.delete
scheduled_task.manual_run  scheduled_task.replace  scheduled_task.set_enabled
scheduled_task.mark_history_read  scheduled_task.mark_all_history_read

attachment.consume  attachment.discard  attachment.paste_config
attachment.pick     attachment.pick_many  attachment.reveal_folder
attachment.stage    attachment.stage_native_file  attachment.stage_pasted_files
attachment.upload_abort  attachment.upload_append  attachment.upload_begin
attachment.upload_finish

skill_store.import_local  skill_store.install  skill_store.load
skill_store.set_enabled   skill_store.uninstall

connected_source.create  connected_source.delete
connected_source.set_enabled  connected_source.test

domestic_connector.cancel_oauth  domestic_connector.connect
domestic_connector.disconnect    domestic_connector.retry_services
domestic_connector.set_session_selection

integrations.cancel_oauth  integrations.disconnect
integrations.start_oauth   integrations.status

desktop.confirm_action  desktop.data_location.change  desktop.data_location.get
desktop.data_location.reset  desktop.general_preferences.get
desktop.general_preferences.set  desktop.login_item.get  desktop.login_item.set
desktop.notification_permission.get  desktop.notification_settings.open
desktop.set_theme_source
```

### 4.11 agent 事件域（流式信号）
```
model.request.started   model.request.activity   model.request.completed
assistant.reasoning.started   assistant.reasoning.updated
assistant.reasoning           assistant.commentary
assistant.output.updated
tool.started   tool.progress   tool.result
retry.started  retry.recovered
compaction.started  compaction.completed  compaction.failed
approval.requested  approval.resolved
run.usage.updated
```
这些是**存储层过滤的信号**（`recordSignal` 里 `return` 掉不进 work_events 的轻量信号），说明高频信号不落库。

## 五、与 2.0.1 的差异

旧文档记录「agent 协议词汇表 109 条」「IPC 请求词汇 317 条」。2.1.0 实测：
- desktop invoke 分发器 **25 个分支**（旧文档未记录此项）
- 全包 type 字面量 **757 条**（去重后，含响应侧/事件侧）
- preload 暴露对象 **16 个**

统计口径不同不可直接相比，但可以看出 2.1.0 的词汇量显著增长。

## 六、安全观察

1. **`zwork:desktop:invoke` 是万能入口**：25 个分支里含 `desktop.data_location.change`（改数据目录）、`input_safety.check`、`kernel_repair.perform`（重启/恢复内核）等敏感操作，全部经同一通道。Renderer 侧一旦被 XSS 突破，攻击面是整个 25 分支。
2. **`browser.adopt` / `browser.release`**：内置浏览器接管真实浏览器实例，需确认是否与用户主浏览器共享会话。
3. **`attachment.stage_native_file`**：预加载本地文件（含 dialog 路径解析），是典型的路径穿越关注点。
4. **remote-control 7 个通道直接 `ipcMain.handle`**：远程控制（配对码、控制器审批）是唯一不经统一分发器的域，说明其权限模型独立。

## 七、未决问题

- `zwork:agent:query` 与 `zwork:agent:publication` 的具体语义（查询态 vs 发布态）
- `zwork:desktop:capabilities:invoke` 与 `zwork:workbench-capabilities` 的能力清单
- `collaboration.mode` 的取值域
- `run.efficiency.updated` 的计算口径
