# AutoClaw 逆向分析（二）账号体系 · 签到 · 本地存储

2026-10-19 · 承接 `autoclaw-01-骨架与API链路.md`
本篇覆盖：账号鉴权细节、**签到机制**、本地存储格式、以及「能否体外复用」的结论。

## 一、签到机制（重点）

### 1.1 接口

`src/main/product-account/product-account-daily-credits.ts`：

```js
var DAILY_CREDITS_CLAIM_PATH = "/userapi/v1/user-info";
var DAILY_CREDITS_CLAIM_TIMEOUT_MS = 15e3;
var MAX_DAILY_CREDITS_RESPONSE_BYTES = 64 * 1024;

const requestOnce = async () => {
  try {
    await options2.client.request({
      serviceId: "identity_gateway",
      path: DAILY_CREDITS_CLAIM_PATH,
      method: "POST",
      json: { source_id: "autoclaw", device_id: options2.deviceId },
      timeoutMs: 15e3,
      maxResponseBytes: 64 * 1024,
      retry: false
    });
  } catch {
    // 全吞：失败与成功对调用方无区别
  }
  return true;   // ← 永远返回 true
};
```

**反直觉点**：路径叫 `user-info`（读用户信息），但用 **POST + `source_id:"autoclaw"`** 表达"领取当日赠送积分"。服务端按天去重，**响应体为空**。

代码注释原文：

> 离开支付页等时机请 Main 向服务端领一次当日赠送积分；无参数，结果只有"是否发出"。
>
> Main 是否真的向服务端发出了领取请求（已认证且能力已接线）。**服务端按天去重，领到与否不回传：接口契约上响应体为空**，Renderer 只需据此决定接着刷新余额。

### 1.2 三个触发时机

```js
// ① 登录成功
function claimDailyCreditsOnSignIn(account, dailyCredits) {
  let previousStatus = account.snapshot().status;
  return account.subscribe((snapshot) => {
    const entered = snapshot.status === "authenticated"
      && previousStatus !== "authenticated";
    previousStatus = snapshot.status;
    if (entered) void dailyCredits.claim();
  });
}
```

```js
// ② 每次发任务（Desktop 与 IM 的 send_task 都经同一 Host）
onTaskSent: () => { void dailyCredits.claim(); }
```

```js
// ③ 离开支付页（Renderer 侧 daily_credits.claim 消息）
case "daily_credits.claim":
  response = { type: request.type,
    accepted: options2.account.snapshot().status === "authenticated"
      ? await options2.claimDailyCredits?.() ?? false : false };
  break;
```

**结论**：这不是一个独立的"签到按钮"。它挂在**登录成功**、**每次发任务**、**离开支付页**三个事件上，且**每天只生效一次**（服务端去重），后续调用静默失败。

### 1.3 相关积分接口

| 消息类型 | 用途 | 返回 |
|---|---|---|
| `credits.balance.read` | 读余额 | `{balance: CreditsBalance \| null}` |
| `credits.ledgers.read` | 读流水（分页） | `{page: CreditsLedgerPage \| null}` |
| `daily_credits.claim` | 领当日积分 | `{accepted: boolean}` |
| `membership.read` | 会员信息 | `{membership}` |

**重要约定**（代码注释明确）：

> 未认证、远端不可用或响应结构不符时为 `null`。调用方**不得把 null 当成 0 展示**：「拿不到余额」和「余额为零」对用户是两件事。

## 二、账号鉴权体系

### 2.1 端点清单

| 端点 | 方法 | 用途 |
|---|---|---|
| `/userapi/v1/agent-send-code` | POST | 发短信验证码，body `{phone}` |
| `/userapi/v1/agent-login` | POST | 验证码登录，body `{phone, code:Number}` |
| `/userapi/v1/refresh` | POST | 刷新 token（主） |
| `/userapi/v1/agent-refresh` | POST | 刷新 token（兜底） |
| `/userapi/v1/user-info` | GET/POST | GET 读信息 / **POST 领每日积分** |
| `/userapi/v1/user-profile` | GET | 用户档案 |
| `/userapi/v1/agent-identities` | GET | 身份列表（个人/企业） |
| `/userapi/v1/agent-identity-switch` | POST | 切换身份 |
| `/userapi/v1/agent-identity-enter` | POST | 进入身份 |
| `/userapi/v1/agent/context-profile` | GET | 上下文配置 |
| `/userapi/v1/user/set-config` | POST | 用户配置 |
| `/userapi/v1/user/notifications` | GET | 通知列表 |
| `/userapi/v1/user/web-event/report` | POST | 埋点上报 |
| `/userapi/v1/user/write-off` | POST | 注销 |

### 2.2 登录的一个坑

`signInWithCode` 注释：

```
企业包才给 selectIdentity：传 true 时，手机号还没有个人账号但属于企业的用户
会拿到 identity_selection 而不是 token，服务端也不会替他注册个人账号。
带 identity_selection 时 token 字段全是空串，绝不能当登录成功——调用方先判前者。
```

**实现要点**：登录成功判定必须**先判 `identity_selection` 存在性**，再看 `token` 字段。只看 token 非空会把"需要选身份"误判为失败。

### 2.3 刷新策略

```js
PRIMARY_REFRESH_PATH  = "/userapi/v1/refresh"
FALLBACK_REFRESH_PATH = "/userapi/v1/agent-refresh"
REFRESH_TIMEOUT_MS = 15000
MAX_REFRESH_RESPONSE_BYTES = 256 * 1024
```

失败语义：

```js
// 主路径失败后尝试兜底
const primary = await this.request(PRIMARY_REFRESH_PATH, input);
if (primary.ok) return primary.result;
if (primary.business ... ) { /* 走兜底 */ }
```

### 2.4 token 失效判定

```js
function isProductAccountAccessCredentialRejected(result) {
  if (!result.ok) return false;
  const payload = result.data;
  if (typeof payload !== "object" || payload === null || Array.isArray(payload)) return false;
  return result.status >= 200 && result.status < 300
           && (payload.code === 4100 || payload.code === 4101)
      || result.status === 401
         && Object.keys(payload).length === 1
         && payload.error === "Invalid token";
}
```

**两种失效信号**：
- HTTP 2xx + body `{code: 4100 | 4101}`（业务码失效）
- HTTP 401 + body 恰好只有 `{"error":"Invalid token"}` 一个键

## 三、本地存储

### 3.1 目录结构

```
%APPDATA%/AutoClaw-official/          ← 生产渠道（channel: official）
├── accounts/<sha256(accountId)>/
│   ├── account-credentials.enc        835 B，magic "v10"，DPAPI 密文
│   ├── account-profile.json           明文，含 accountId / phone / displayName
│   ├── user-notifications.json
│   ├── projects/
│   └── runtime/
│       ├── runtime.sqlite             主数据库
│       ├── context-profile.enc
│       ├── model-connection-command-claims.enc
│       ├── desktop-preferences.json
│       └── traces/<traceId>/transcript-<id>.jsonl
├── device/
│   ├── active-product-account.json    {"accountKey":"<sha256>"}
│   └── device-id.json                 {"deviceId":"<uuid>"}
├── logs/
│   ├── zwork-main-YYYY-MM-DD.log      主进程日志（文本 + JSON 数组）
│   └── model-token-usage.log          token 用量（纯 JSONL）
├── diagnostics/keep-awake-main.log
└── Partitions/zwork-account-<hash>/   Electron 分区
```

渠道隔离：`AutoClaw`（旧/无渠道）与 `AutoClaw-official`（生产）是两个独立目录。

### 3.2 凭据文件

```
account-credentials.enc
0000000   v   1   0   @ 325 \b 226 : 8 W Q 2 035 001 207 ^
```

- magic `v10` 是 Electron `safeStorage` 在 Windows 上的格式（DPAPI + AES-GCM）
- 加密实现（`product-account-encryption.ts`）：

```js
function isElectronProductAccountEncryptionAvailable(storage = electron.safeStorage, platform = process.platform) {
  if (!storage.isEncryptionAvailable()) return false;
  // Linux 上 basic_text 后端视为不可用
  return platform !== "linux" || storage.getSelectedStorageBackend?.() !== "basic_text";
}
return {
  isAvailable: () => isElectronProductAccountEncryptionAvailable(),
  encrypt: (plaintext) => safeStorage.encryptString(plaintext),
  decrypt: (ciphertext) => safeStorage.decryptString(ciphertext)
};
```

**含义**：token 绑定当前 Windows 用户，**不能拷到别的机器复用**；同机同用户下理论上可解。

### 3.3 runtime.sqlite 关键表

| 表 | 内容 |
|---|---|
| `model_connections` | **模型连接配置（最有价值）** |
| `work_sessions` | 会话（title / workspace / external_provider 等） |
| `work_events` | 会话事件流（cursor / run_id / type / payload_json） |
| `work_runs` | 运行记录 |
| `memory_fts*` | 记忆全文索引（FTS5） |
| `identity_profiles` / `identity_personal_principals` | 身份 |
| `governed_*` | 受管的工具/来源/审批规则 |
| `model_connection_commands` | 模型连接命令记录 |

**`model_connections` 实测内容**：

```
provider        = zwork-managed
model           = zai_auto / zai_auto-fast
base_url        = https://autoglm-acceleration-api.zhipuai.cn/autoclaw-proxy/proxy/autoclaw
protocol        = openai-completions          ← 标准 OpenAI 协议
context_window  = 1048576
max_tokens      = 393216
supports_reasoning = 1
reasoning_levels_json = ["low","high","max"]
supports_images = 1
connection_revision = managed-catalog:v1:<sha256>
credential_version  = <uuid>
test_status     = succeeded
```

**三条结论**：
1. 上游网关说 **`openai-completions`**，协议转换层可以直接复用现有 2api 的 chat 路由
2. 模型 ID 是 `zai_auto` / `zai_auto-fast`（自动路由）与 `zaicoding_glm-5.3`（日志中出现）
3. `connection_revision` 是 `managed-catalog:v1:<hash>`，说明模型清单由服务端下发并按 hash 版本化

### 3.4 日志格式

**主日志**（`logs/zwork-main-*.log`，文本行）：

```
2026-10-19T07:18:19.443Z INFO ["zwork.update.process_started",{"pid":23388,"platform":"win32",...}]
2026-10-19T07:18:19.597Z INFO ["zwork.update.electron_ready",{"pid":23388,"version":"2.1.0","packaged":true}]
2026-10-19T07:18:19.735Z INFO ["zwork.remote_control.installed",{"endpoint":"https://autoglm-acceleration-api.zhipuai.cn"}]
2026-10-19T07:18:30.423Z WARN ["zwork.account.profile_optional_field_ignored",{"field":"email"}]
```

格式：`<ISO8601> <LEVEL> ["<event>", {json}]`

实测事件分布（今日 180 行）：`skills.preinstall.deploy.decision` ×104、`update.*` ×7、`desktop.*` ×5、`observation.config_refresh_failed` ×2。

**token 用量日志**（`model-token-usage.log`，纯 JSONL）：

```json
{"schemaVersion":1,"timestamp":"2026-09-29T02:03:43.032Z","level":"info",
 "event":"zwork.model.token_usage.record","component":"model_token_usage",
 "context":{"ownerId":"0f46f185-...","host":"desktop","runtimeVersion":"2.0.1",
  "platform":"win32","architecture":"x64","provider":"zwork-managed",
  "model":"zaicoding_glm-5.3","purpose":"model_step",
  "logicalRequestId":"...","physicalRequestId":"...","attempt":1,
  "outcome":"succeeded","responseFinished":true,"finishReason":"tool-calls",
  "thinkingLevel":"high","reasoningOption":"high","reasoningEffort":"high",
  "reasoningSource":"pi",
  "input":28793,"output":725,"total":43790,
  "cacheRead":14272,"cacheWrite":0,"reasoning":649}}
```

字段含义：
- `provider: "zwork-managed"` —— 走网关托管模型
- `model: "zaicoding_glm-5.3"` —— 实际模型
- `reasoningSource: "pi"` —— 推理来源标识
- `cacheRead: 43008 / input: 148` —— **缓存命中率极高**（连续请求时 input 只有 148 而 cacheRead 有 43008），说明上游有 prefix cache
- `thinkingLevel / reasoningOption / reasoningEffort` 三者同值，均为 `high`

## 四、可反代性评估

| 维度 | 结论 |
|---|---|
| **协议** | ✅ `openai-completions`，可直接映射到现有 chat 路由 |
| **客户端指纹** | ⚠️ 有 `X-Auth-Sign = md5(appId&ts&appKey)`，但 **appId/appKey 明文硬编码在 main.cjs**，可完整重放 |
| **账号鉴权** | ❌ `X-Authorization: Bearer <token>`，token 存 `safeStorage`（DPAPI），**跨机器不可移植** |
| **模型清单** | ⚠️ 服务端下发，`managed-catalog:v1:<hash>` 版本化 |
| **额外校验** | 未知。`X-Client-Type: pc`、`x_trace_id: autoclaw-desktop`、`X-Trace-Id` / `X-Request-Id` 等是否参与校验需实测 |

**核心卡点：token。** 与 OpenCode 的 `Bearer public`（匿名、无账号）本质不同——AutoClaw 要走真实账号登录，token 是 DPAPI 绑定的。

### 四条可选路径

| 路 | 做法 | 可行性 |
|---|---|---|
| **A. 同机解密** | 反代与 AutoClaw 同一 Windows 用户，用 DPAPI 解开 `account-credentials.enc` | 中，需复现 Electron `safeStorage` 的 `v10` 密钥派生 |
| **B. 抓包复用** | 运行一次，抓 `POST /userapi/v1/user-info` 的完整头，拿 `X-Authorization` 重放 | **低风险、可立刻验证**，但 token 会过期需定期刷新 |
| **C. 走刷新链** | 拿到 refresh token 后自己调 `/userapi/v1/refresh` 续期 | 需要先完成 A 或 B |
| **D. 不反代** | 只用它的技能/协议设计做参考，不碰 token | 无合规风险 |

## 五、待验证

1. `account-credentials.enc` 的 `v10` 格式能否在 Node/Python 里复现解密
2. 签到的 `POST /userapi/v1/user-info` 是否还有其他必填头（如 `X-Auth-Sign` 时间窗限制）
3. 模型网关 `/autoclaw-proxy/proxy/autoclaw` 是否有超出签名的客户端校验
4. `reasoning_levels_json = ["low","high","max"]` 与实际可用的 `xhigh` 是否有出入（对照 ZCode 那边的 `xhigh` 问题）
