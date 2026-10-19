# AutoClaw 逆向分析（四）Agent 运行时 · 沙箱 · Office 桥 · 观测

2026-10-19 · 基于 **2.1.0** 实测（旧文档基于 2.0.1，本篇数据全部重新提取）

## 一、Agent 集成形态

**2.1.0 里搜索 `pi-coding-agent` 结果为 0**，旧文档记录的 `pi-coding-agent` 集成在 2.1.0 已不存在（或已改名/内联）。实际搜索 `send_task` 12 次、`AgentCommand` 21 次，说明 agent 协议是自有的 command 模型。

### 1.1 agent 协议字面量（8 条，实测 `literal("agent.*")`）

```
agent.create          agent.update_profile     agent.usage.set
agent.memory.upsert   agent.memory.delete
agent.preset.create   agent.preset.update      agent.preset.delete
```

`collaboration.*` 域是同一批命令的协作态镜像（13 条，含 `collaboration.mode`、`collaboration.participant.stop`、`collaboration.run.retry`、`collaboration.run.stop`）。

### 1.2 流式信号（存储层过滤）

`recordSignal` 里有一批信号**直接 return，不落 work_events**（高频、体积大）：

```
model.request.started     model.request.activity    model.request.completed
assistant.reasoning.started   assistant.reasoning.updated
assistant.reasoning       assistant.commentary
assistant.output.updated
tool.started   tool.progress   tool.result
retry.started  retry.recovered
compaction.started  compaction.completed  compaction.failed
run.usage.updated
```

只有 `approval.resolved` 这类低频道事实会在该函数内写库（`governed_tool_approvals` 表）。

## 二、沙箱执行层（autonomous）

### 2.1 组成

`dist/resources/autonomous/`：

```
execution-worker.cjs                          184 行，Bun 运行
windows-helper/bin/zwork-sandbox-helper.exe
windows-helper/bin/installer-process-helper.exe
release-manifest.json
```

主进程按需装配（`packagedExecutionEnvironmentRouter`，仅 `darwin` / `win32`）：

```js
const autonomousRoot = join(process.resourcesPath, "app", "dist", "resources", "autonomous");
const manifestPath   = join(autonomousRoot, "release-manifest.json");
const workerPath     = join(autonomousRoot, "execution-worker.cjs");
const runtimePath    = join(toolchainPackagedRoot(resourcesPath, "bun"), "bun.exe");
const helperPath     = platform === "win32"
  ? join(autonomousRoot, "windows-helper", "bin", "zwork-sandbox-helper.exe") : undefined;
// 四个文件都存在才启用，否则返回 null（退化为非沙箱）
```

**证据等级 A**：`every(existsSync)` 前置校验，缺一个就放弃沙箱。

### 2.2 平台策略

```js
AUTONOMOUS_POLICY_REVISION = "coding-workspace-seatbelt-appcontainer-v1"
```

| 平台 | Provider | 机制 |
|---|---|---|
| macOS | `MacosExecutionEnvironmentProvider` | Seatbelt（sandbox-exec） |
| Windows | `WindowsExecutionEnvironmentProvider` | **AppContainer**（经 `zwork-sandbox-helper.exe`） |

### 2.3 worker 协议

`execution-worker.cjs` 是 **stdin/stdout 的 JSON-RPC 式协议**，每条消息带 HMAC 签名：

```js
const MAX_MESSAGE_BYTES = 1024 * 1024;
const MAX_OUTPUT_BYTES  = 512 * 1024;

function mac(value) {
  return crypto.createHmac('sha256', secret).update(JSON.stringify(value)).digest('hex');
}
function send(value) {
  process.stdout.write(JSON.stringify({ value, mac: mac(value) }) + '\n');
}
```

支持的命令（**6 条，权限面很窄**）：

```
filesystem.read      filesystem.write     filesystem.list
filesystem.edit      runtime.registered_program    sandbox.probe
```

### 2.4 路径防护（值得注意）

`contained(root, relativePath, allowMissingLeaf)` 的检查链：

```js
1. 拒绝 NUL 字节、拒绝绝对路径
2. Windows 额外拒绝：
   - 含 ':'（盘符）
   - 段尾带空格  segment !== segment.trimEnd()
   - 段尾带 '.'  segment.endsWith('.')
   - 保留设备名  /^(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?$/i
3. 拒绝 '..' 开头
4. realpath(root) 后 join，再 realpath 目标，用 path.relative 判是否逃逸
5. allowMissingLeaf 时对 dirname 做 realpath（支持创建新文件）
```

**这是本次审查里质量最高的一段路径校验**——Windows 那条设备名 + 尾随空格/点 的正则，是防 `CON`、`NUL`、`foo. ` 这类经典绕过的标准做法。

### 2.5 子进程环境净化

```js
function cleanChildEnvironment(root) {
  if (platform === 'win32') return {
    SystemRoot: 'C:\\Windows', WINDIR: 'C:\\Windows',
    PATH: 'C:\\Windows\\System32;C:\\Windows',
    USERPROFILE: join(root, '.zwork-profile'),
    TEMP: join(root, '.zwork-tmp'), TMP: join(root, '.zwork-tmp'),
    HOME: join(root, '.zwork-profile'),
  };
  return { PATH: '/usr/bin:/bin:/usr/sbin:/sbin', LANG: 'C.UTF-8',
           HOME: '/nonexistent', TMPDIR: join(root, '.zwork-tmp'), PWD: root };
}
```

**白名单式环境变量**，不继承父进程 env——这是防止凭据泄漏到子进程的正确做法（对比很多 agent 直接 `{...process.env}`）。

## 三、Office 插件桥（本地 HTTP/HTTPS 服务）

### 3.1 模块规模

`src/main/office-plugin/` 下 **56 个模块**，是本应用最大的单一子系统：

```
bridge-service.ts / bridge-http.ts / bridge-tls.ts / bridge-ca-trust.ts
office-stream-*.ts   ← 16 个：hub / routing / handshake / frames / wire / validation …
office-pane-*.ts     ← 9 个：chat / contracts / history / input-safety / payload / runs / workbench …
office-tool*.ts      ← 4 个：contract / provider-contracts / provider-manifests / provider-queue
conversation-store / session-binding-store / pairing / safe-write / zwork-kernel-adapter
```

### 3.2 监听与 CORS

```js
var OFFICE_PLUGIN_HOST = "127.0.0.1";                    // 只绑回环
var OFFICE_BRIDGE_CORS_LOOPBACK_ORIGIN = /^https?:\/\/(127\.0\.0\.1|localhost|\[::1\])(:\d+)?$/;
function officeBridgeCorsAllowOrigin(origin) {
  if (typeof origin !== "string" || !origin) return "*";
  return LOOPBACK_ORIGIN.test(origin) ? origin : "*";
}
response.setHeader("Access-Control-Allow-Private-Network", "true");   // ← 允许 PNA
```

**注意**：`Access-Control-Allow-Private-Network: true` 是为 Office 加载项（https 页面）访问 localhost 服务的合法用法，但配合"非回环 origin 也返回 `*`"的兜底，**任意网页都能跨域打到这个本地桥**。桥自身靠 pair 密钥鉴权（见下），所以不直接等于漏洞，但值得记一笔。

### 3.3 协议与加密

`protocol.ts`：

```js
var OFFICE_PLUGIN_PROTOCOL_VERSION  = 2;
var OFFICE_PLUGIN_SERVICE_NAME      = "autoclaw-office-plugin";
var OFFICE_PLUGIN_MAX_CLOCK_SKEW_MS = 2 * 60 * 1e3;     // 2 分钟时钟容差
var ENCRYPTION_LABEL     = "autoclaw-office-plugin/v1/encryption";
var AUTHENTICATION_LABEL = "autoclaw-office-plugin/v1/authentication";
// 密钥必须 32 字节（AES-256）
function normalizeSecret(secretBase64) {
  const secret = Buffer.from(String(secretBase64 || ""), "base64");
  if (secret.length !== 32) ...
}
```

### 3.4 TLS：自建 CA + 本地证书

`bridge-tls.ts`（用 `node-forge`）：

```js
const CA_KEY   = join(TLS_DIR(), "ca-key.pem");
const CA_CERT  = join(TLS_DIR(), "ca-cert.pem");
const LEAF_KEY = join(TLS_DIR(), "leaf-key.pem");
const LEAF_CERT= join(TLS_DIR(), "leaf-cert.pem");

const LEAF_DAYS = 825;          // 叶子证书 825 天（Apple 上限）
const CA_DAYS   = 3650;         // CA 10 年
const NOT_BEFORE_SKEW_MS = 60 * 60 * 1e3;       // 提前 1 小时生效
const LEAF_RENEW_MARGIN_MS = 7 * 24 * 3600 * 1e3; // 到期前 7 天续签
const BRIDGE_CA_CERT_COMMON_NAME = "AutoClaw Local Bridge CA";
```

密钥 RSA-2048，`pki.createCertificate()` 自签，CA + 叶子双层。

### 3.5 Windows 上的 CA 信任

`bridge-ca-trust.ts` 有 `setBridgeTlsWinCaTrusted(trusted)` 的开关，说明**会把自建 CA 装进 Windows 受信任根**（否则 Office 的 WebView 不认本地 https）。这是需要重点关注的攻击面：一个软件在用户机器上装根证书，必须确认卸载时清理。

### 3.6 Office 加载项

Word Web 插件：

```js
var WORD_WEB_PLUGIN_SERVER_PORT = 3890;
var WORD_WEB_LOCAL_ORIGIN  = `http://127.0.0.1:3890`;
var WORD_WEB_SOURCE_LOCATION = `${LOCAL_ORIGIN}/taskpane.html`;
var WORD_WEB_ADDIN_ID = "be779d65-397c-4369-aec2-e9cd6d799dc5";
var WORD_PLUGIN_DEFAULT_URL_BASE = isDev
  ? "https://zwork-test.zhipuai.cn/office-plugin-v2/word-plugin/"
  : "https://autoclaw.zhipuai.cn/office-plugin-v2/word-plugin/";
```

支持环境变量覆盖：`ZWORK_WORD_PLUGIN_URL_BASE` / `AUTOCLAW_WORD_PLUGIN_URL_BASE`。

另有 WPS 插件端点：`https://autoclaw.zhipuai.cn/office-plugin-v2/wps-plugin/`。

## 四、观测 / 遥测

### 4.1 APM（火山引擎 APMPlus）

`src/main/desktop/instrumentation/apm/parfait*.ts`：

```js
var ZWORK_PARFAIT_VERSION   = "1.4.1.0";
var PARFAIT_HOST_CN         = "https://apmplus.volces.com";
var PARFAIT_HOST_OVERSEAS   = "https://apmplus.ap-southeast-1.volces.com";
var PARFAIT_AID             = 1002242;
```

启用条件（不满足直接 `enabled: false`）：

| 条件 | 值 |
|---|---|
| 平台 | 仅 `darwin` / `win32`（Linux 禁用） |
| 架构 | darwin 需 `arm64`/`x64`；win32 仅 `x64` |
| 上报地域 | 由构建环境决定，**业务调用方不能在运行时覆盖** |
| 探针 | `writeInitProbe = environment !== "production"`（正式包只初始化真实 SDK） |

原生 addon 路径：

```js
resolve( mainModuleDirectory, "resources", "apmplus", `${platform}-${arch}`, "parfait.node" )
```

### 4.2 埋点

`zwork:analytics:track` / `get-identity` / `startup-surface-ready` 三通道，由 `zworkAnalytics` 暴露。
业务事件示例（`TASK_ANALYTICS_EVENTS`）：

```
send_task                send_task_result        terminate_task
scheduled_task_create_result
```

`REPORTED_RUN_LIMIT = 2048`（去重集合上限，防内存膨胀）。

## 五、与 2.0.1 的差异

| 项 | 旧文档（2.0.1） | 实测（2.1.0） |
|---|---|---|
| pi-coding-agent | 记录为集成形态 | **搜索结果为 0**，已不存在 |
| Office 桥模块 | 记录存在 | 56 个模块，规模远超预期 |
| 沙箱 | 未详述 | `execution-worker.cjs` + Windows AppContainer helper + 6 条命令 |
| APM | 记录为 apmplus | 补齐 host / aid / 版本号 / 平台门禁 |

## 六、安全观察

1. **Office 桥的自建 CA 会进 Windows 受信任根**——需验证卸载时是否清理，否则是持久化信任残留。
2. **CORS 对非回环 origin 返回 `*`**——桥靠 pair 密钥鉴权兜底，但跨域可探测面变大。
3. **沙箱路径校验质量高**（设备名/尾随空格/realpath 逃逸全防），子进程 env 白名单化，是正面案例。
4. **沙箱依赖四个文件齐全才启用**，任一缺失即静默退化为非沙箱执行——排查"为什么没进沙箱"时先查这四个文件。

## 七、未决问题

- `release-manifest.json` 的字段与校验（`workerSha256` / `providerRevision` 等）
- `WindowsExecutionEnvironmentProvider` 与 `zwork-sandbox-helper.exe` 的 IPC 协议
- Office 桥 pair 密钥的分发方式（`pairing.ts`）
- APM 上报的具体事件集（需抓包确认实际发送内容）
