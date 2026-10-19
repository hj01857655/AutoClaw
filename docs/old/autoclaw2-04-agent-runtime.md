# AutoClaw2-04 Agent 运行时与本地服务面

状态：A（实测）。覆盖：agent 与主进程的集成方式、embedded 扩展权限模型、观测/遥测、Office 插件桥。

## Agent 集成形态

- Agent 内核 = `@earendil-works/pi-coding-agent` 0.83.0（node_modules 完整包）+ `packages/zwork-agent/src/**`（863 个模块打进 main.cjs）。
- **无独立 agent 进程**：`protocol.command/query/subscribe` 直接在 main 进程内实现（`zwork-agent-ipc.ts`）。IPC 层是 schema 校验（zod）+ 发布帧管线。
- 发布管线 `PublicationIpcFramer`：合并窗口批量推送；瞬态帧（`block.delta`、`tool.progress`、`model.request.activity`、reasoning/output 更新）按 `runId:域:实体id` 键去重替换；accumulated 文本帧对 renderer 做**差分编码**（只发增量）。订阅协议带 `afterCursor` / `afterModelConnectionCursor` 断线恢复。
- Trajectory 观察 IPC 注册被 `if (false)` 常量折叠——**生产构建里该面不存在**。

## embedded 扩展（pi-web-access）

manifest（`embedded-product-extensions.json`）：
```
id: pi-web-access, version: 0.15.0, source: npm:pi-web-access@0.15.0
integrity: sha512-62ir9Z5K8oCPZVFJRCYeV0uzsTHNEi01p84nn/5NkTYFhVQzxLVooA2DU83hTeWt2DlwaiijYyoPx6yxHrC4LA==
patch: patches/pi-web-access@0.15.0.patch (sha256 c604201c…)
entrypoint: pi-web-access/index.ts, headless: true
tools: fetch_content, get_search_content, source_check
```

权限模型（authority 声明式，Proxy 拦截越权）：
- network: public-http / provider-api / remote-extractor
- filesystem: namespaced-extension-config / downloads / temporary-cache / browser-profiles / user-selected-input
- process 白名单：gh、git、ffmpeg、ffprobe、yt-dlp、npm、security、secret-tool、sqlite3、python3、credential-command
- ambientCredentials: environment / pi-codex-auth / gh-auth / browser-cookie / keychain
- headless 工厂用 Proxy 包装扩展 API：`registerTool` 只允许 manifest 声明过的工具名（`embedded_extension_tool_not_declared`）；UI 启动命令被拦（`open`/`xdg-open`/`cmd start` → `embedded_extension_ui_blocked`）；registerCommand/Shortcut/MessageRenderer/appendEntry/sendMessage 全部吞掉；载入后实际工具集与 manifest 严格比对（`embedded_extension_tool_set_mismatch`）。
- `fetch_content` 标记 parallel-safe，其余 serialized。
- 运行时作用域链：webAccess runtime scope → storage scope → activity monitor scope → github clone scope（`Symbol.for("zwork.pi-web-access.config-dir")` 传配置目录）。

## 观测 / 遥测

- **Parfait 原生 APM**（火山引擎 APMPlus）：addon `dist/resources/apmplus/{platform}-{arch}/parfait.node`，版本 1.4.1.0，`AID=1002242`，host `apmplus.volces.com`（海外 `apmplus.ap-southeast-1.volces.com`）。仅 darwin/win32（win32 限 x64）。
  - DID=ProductDeviceId（登录链路生成），UID=登录 accountId（匿名回退 DID）；数据进火山"AutoClaw 已验证项目"。
  - env `ZWORK_VOLC_OBSERVATION=0` 可关观测。
  - 云控回捞：ALog 目录由 Parfait 管理轮转与云控回捞（服务端可拉取日志）。
- **任务埋点**（`task-analytics-reporter.ts`）：send_task / send_task_result / terminate_task，带 `zwork_scene`（daily_office/design/legal/coding/financial_reporting/financial_analysis）、agent_id、collaboration_mode。Desktop 与 IM 任务同源上报（镜像一次防重）。
- **启动/生命周期**：clean-exit 标记、`markRunningAfterAbortedShutdown`（更新失败回滚）、`reportAppLaunch`。
- 连接器分析名映射：beida_fabao→pkulaw、qichacha→qcc、yuandian、lexiang、tianyancha、fayan、gmail、google_*、notion、github、figma、vercel、cloudflare。

## Office 插件桥（本地服务，Windows 攻击面重点）

**发现服务**（`src/main/office-plugin/discovery.ts` + `bridge-http.ts`）：
- HTTP(S) 监听 `127.0.0.1:3917`（`OFFICE_PLUGIN_DISCOVERY_HTTP_PORT`），loopback-only（非 127.0.0.1/::1 直接 403）。
- `GET /` 返回 discovery JSON：`{service:"autoclaw-office-plugin", protocolVersion:2, host, port, tls?, instanceId, publicKeyPem, publicKeySha256, rsaModulus, rsaExponent, bootstrapToken(32B random), startedAt}`。
- 同时落盘 `bridge.json`（discovery 目录，mode 384）供文件发现；带 instanceId 的孤儿清理。
- CORS：非 loopback Origin 回 `*`；`Access-Control-Allow-Private-Network: true`。
- 若 TLS 选项存在则 https。

**TLS 层**（`bridge-tls.ts`）：
- 自签 RSA-2048 CA "AutoClaw Local Bridge CA"（10 年）+ 叶子证书（825 天，7 天续期边距），node-forge 生成。
- CA 证书可装入 Windows 信任库（`setBridgeTlsWinCaTrusted` 信任门控）。
- 目录：`bridgeTlsDirectory()` 下 ca-key.pem / ca-cert.pem / leaf-key.pem / leaf-cert.pem。

**会话协议 v2**（`protocol.ts`）：
- 配对：插件用 discovery 里的 RSA 公钥做 **RSA-OAEP-SHA256** 加密 32B 随机共享密钥；主机私钥解出。
- 之后信封：`{version:2, clientId, createdAt, nonce(24B), iv(16B), ciphertext, mac}`。
- 密钥派生：`encryptionKey = HMAC-SHA256(secret, "autoclaw-office-plugin/v1/encryption")`；`authenticationKey = HMAC-SHA256(secret, "autoclaw-office-plugin/v1/authentication")`。
- 载荷 AES-256-CBC 加密；HMAC-SHA256 全信封 MAC（timingSafeEqual）；时钟偏移容忍 2 分钟（防重放窗口）。
- 信封 MAC 输入：`version|clientId|createdAt|nonce|iv|ciphertext`。

安全评注（实测代码行为，非猜测）：
- AES-256-CBC + HMAC（encrypt-then-MAC）是完整方案，非弱设计；但 CBC + 2min 时钟窗 + nonce 无状态表（未见重放缓存，待 runtime 确认）意味着重放检测靠 createdAt。
- 本地 CA 私钥落盘用户目录（mode 384）；若 Windows 信任库已装该 CA，任何本机进程持有该文件即可 MITM 该桥的 TLS。
- 3917 端口 loopback-only，攻击面限于本机恶意代码 / 恶意网页（Private Network Access 头已声明，Chrome 会阻止公网→loopback，但本地恶意进程无阻碍）。

## 未决问题

- 桥的业务端口（非 3917 discovery 端口）与命令词汇表（`bridge-service.ts` 其余部分）。
- `parings.json` IM 配对协议（accountId\nclientId）。
- Parfait 云控回捞的具体协议（APMPlus SDK 内部）。
- agent 会话持久化层（`zwork-node-sqlite.ts`，bundle 内含 node:sqlite）。
