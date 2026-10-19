# AutoClaw2-09 桌面 IPC 请求词汇全集与更新链路

状态：A（实测）。317 条 `literal("x.y")` 判别请求类型已提取至 `work/ipc-request-type-literals.txt`，本篇按域归类解读。

## desktop invoke 请求域（renderer → 主进程）

**workspace.\***（项目/工作区管理，走 authority + input safety）：
create、bind、rename、repair、set_default、trust（trust_status 切换，对应 docs/07 workspaces.trust_status）、folder.add / folder.remove / folder.set_primary、instruction.propose / instruction.decide（项目指令治理，对应 governed_project_instruction_revisions）

**attachment.\***（附件管线，sender 绑定信任）：
pick、pick_many、paste_config、stage、stage_native_file、stage_pasted_files、upload_begin / upload_append / upload_finish / upload_abort（分片上传状态机）、consume、discard、reveal_folder

**account.\***：profile.get、avatar.generate、credits.redeem（对接 /agent-assetmgr）、privacy.read / privacy.update、delete.notice / delete.confirm（对应 device/pending-account-deletion.json 两步删除）、sign_out

**model_connection.\***：discover、probe、configure、activate、retest、delete、candidate.save、capability.update、vision_hint、catalog_changed(事件)——模型接入全生命周期，配合 loadBuiltInModelCatalog / loadModelProviderPresets

**skill_store.\***：load、install、import_local、uninstall、set_enabled——技能商店（对端 /agentdr/v1/assistant/skills/* + dist/resources/skills-preinstall）

**project_directory.\***：pick（用户自选）、provision_managed（托管目录供给）、archived_path

**integrations.\***：start_oauth、cancel_oauth、status、disconnect（第三方连接，对端 /userapi/v1/connected-apps/*）

**browser.\***（见 docs/08）、**workbench.capabilities.subscribe / retry**（能力面推送重试）

## agent 协议命令域（zwork:agent:invoke，desktop → agent runtime）

session.bootstrap / send、response.create、run.{retry,cancel,stop}、approval.resolve、memory.{propose,delete,revoke}、agent.create / update_profile / preset.*、collaboration.*、model_connection.activate / configure、scheduled_task.create、tool.cancel、guide.requested 等（完整动词表见 docs/02）。

## 更新链路（src/main/updates/update-source.ts）

- **更新源**（ZWORK_UPDATE_ROOTS）：cn=`https://autoglm.aminer.cn/zwork/updates/`，oversea=`https://autoglm-public-oss.z.ai/zwork/updates/`
- feed URL 构造：`{root}/{environment}/{channel}/{platform}/` + `latest-{version}[platform后缀].yml`（electron-updater 6.6.2 标准格式；win32-x64 无后缀，linux-arm64/linux-x64/mac 有专属后缀；linux 产物名 `AutoClaw-{ver}-{arch}[-cn][-env].AppImage`）
- **源完整性校验（值得记录）**：policy.baseUrl 必须与受信 root **同 origin**、路径必须以 `/zwork/` 开头、禁 userinfo/query/hash、pathname 禁 `%` 和 `\/`（防路径穿越/编码绕过）、结尾补 `/`。即服务端下发的 feed 只能在固定 OSS 桶内。
- environment 白名单 test/pre/production，channel 校验 isZworkChannelName，版本强 semver。
- 更新安装失败 → 运行时回滚 clean-exit 标记（docs/04 启动观测联动）。

## 安装器（dist/installer/）

- payload-manifest.json 883KB：安装 payload 清单；payload-verify.cjs 校验；workspace-collision-probe.cjs 探测工作区路径冲突。对应 01 篇"233MB exe 疑嵌额外 payload"——manifest 可对读确认。

## 未决问题

- legal_reference / general_settings / design_prototype / finance_source 等低频域的字面量未在本篇展开（在 work/ipc-request-type-literals.txt 内，317 条全量可查）。
- electron-updater 的差分更新（blockmap）是否启用。
- payload-manifest.json 与 exe 体积的关系（是否自解压 agent 工具链）。
