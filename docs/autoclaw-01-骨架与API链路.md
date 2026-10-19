# AutoClaw 逆向分析（一）骨架与 API 链路

2026-10-19 · 目标：智谱 AutoClaw（澳龙）桌面客户端 v2.1.0
安装位置：`%LOCALAPPDATA%/Programs/AutoClaw2`

## 一、产品与安装形态

| 项 | 值 |
|---|---|
| 产品名 | AutoClaw（澳龙），智谱 AI 出品 |
| 官网 | https://autoclaw.zhipuai.cn（海外 autoclaw.z.ai） |
| 实质 | 智谱 ZCode 体系下的产品，包名内部叫 **`zwork`** |
| 版本 | package.json `2.1.0`；Electron `42.6.1` |
| 安装体积 | 721 MB |
| 框架 | **Electron**（chrome_*.pak / icudtl.dat / resources.pak / LICENSES.chromium.html） |

**关键：不是 asar 打包。** `resources/app/` 是**明文目录**：

```
resources/app/
├── package.json          313 B，入口 dist/zwork-main-bootstrap.cjs
├── dist/
│   ├── main.cjs          29 MB（主进程，全部逻辑）
│   ├── zwork-main-bootstrap.cjs   553 KB
│   ├── zwork-bootstrap-preload.cjs 1.35 MB
│   ├── channel.json      {"channel":"official","stage":"production"}
│   ├── renderer/         前端（zwork.html + assets）
│   ├── resources/        技能/插件/工具链
│   └── installer/        payload-manifest.json 等
└── node_modules/         仅 1 个包
```

**附带四套运行时**（`resources/vendor/`）：

| 运行时 | 大小 | 用途推测 |
|---|---|---|
| bun | 83 MB（bun.exe 86MB） | agent 主运行时 / autonomous worker |
| node | 106 MB（node.exe 93MB） | 技能脚本 / lark-cli |
| dotnet | 72 MB | Office 插件 |
| python | 22 MB | 数据处理技能 |

`resources/app/dist/resources/` 下还有：`agent-skills`、`agent-skills-core`、`agent-skills-craft`、`autonomous`、`bin`、`lark-cli`、`office-plugin-templates`、`skills-preinstall`、`vertical-packs`、`apmplus`（埋点）。对应官网宣传的「50+ 内置技能」。

## 二、服务基址映射

`packages/zwork-agent/src/host/bundled-runtime-config.ts`：

```js
HTTP_ORIGIN_BY_ENVIRONMENT = {
  local:            "https://autoglm-test-2.zhipuai.cn",
  test:             "https://autoglm-test-2.zhipuai.cn",
  pre:              "https://autoglm-pre-api.zhipuai.cn",
  release:          "https://autoglm-acceleration-api.zhipuai.cn",   // ← 生产
  overseas_test:    "https://autoglm-test-api.autoglm.ai",
  overseas_pre:     "https://autoglm-pre-api.autoglm.ai",
  overseas_release: "https://autoglm-api.autoglm.ai"
};

LEGAL_CLOUD_ORIGIN_BY_ENVIRONMENT = {   // 法律云网关单独一套
  release: "https://autoglm-api.zhipuai.cn",
  ...
};
```

七个服务 ID（`RUNTIME_CONFIG_SERVICE_IDS`）：

```
identity_gateway · channel_hub · schedule_gateway · device_relay
managed_model_gateway · connected_source_gateway · legal_cloud_gateway
```

- 全部走 `https` + 同一个 `origin`
- `device_relay` 走 **wss**
- `legal_cloud_gateway` 走 `LEGAL_CLOUD_ORIGIN`

## 三、模型调用链路（核心）

`src/main/model-connections/zwork-managed-model-request.ts`：

```js
var MODEL_SERVICE_ID = "managed_model_gateway";
var MODEL_PROVIDER_API_KEY = "zwork-managed-model";   // 占位 key，不是真凭据
var MANAGED_MODEL_TRACE_SOURCE = "autoclaw-desktop";
var MANAGED_MODEL_QUEUE_SSE_EVENT = "zwork_queue";

// 模型基址
modelBaseUrl = resolveManagedEndpoint({
  serviceId: "managed_model_gateway",
  path: "/autoclaw-proxy/proxy/autoclaw"
});
// → https://autoglm-acceleration-api.zhipuai.cn/autoclaw-proxy/proxy/autoclaw

// 模型目录
MODEL_CONFIG_PATH = "/autoclaw-proxy/proxy/autoclaw-model-config";
```

其余同族端点（都在 `/autoclaw-proxy/proxy/` 下）：

```
autoclaw-model-config        模型清单
autoclaw-promotion-config    推广配置
```

### 请求头构造（指纹层）

`src/main/network/zwork-managed-request-headers.ts`：

```js
var AUTOCLAW_COMPATIBLE_IDENTITY = {
  appId: "100003",
  appKey: "<32位十六进制，明文硬编码于 main.cjs>",
  product: "autoclaw"
};
var ZWORK_DEFAULT_DISTRIBUTION_CHANNEL = "official";
var ZWORK_ENTERPRISE_HEADER = "X-Enterprise";

// 每次请求现算
timestamp = floor(Date.now() / 1000)
headers = {
  "X-Version":        app.getVersion(),
  "X-Tm":             managedPlatform(process.platform),   // 平台标识
  "X-Product":        "autoclaw",
  "X-Auth-Appid":     "100003",
  "X-Auth-TimeStamp": timestamp,
  "X-Auth-Sign":      md5(`${appId}&${timestamp}&${appKey}`),
  "X-Lang":           "zh-CN",
  "X-Channel":        "official",
  // 企业包才加：
  "X-Enterprise":     "1"
}
```

**注意**：`X-Auth-Sign` 只是 `md5(appId & ts & appKey)`，**appKey 在包里是明文**——这一层不需要任何逆向就能重放，本质是防君子不防小人的 app 标识，不是真正的门禁。

### 模型请求的额外头

`createZworkManagedModelRequestResolvers`：

```js
// 先拿到基础签名头，然后：
headers.delete("authorization");        // ← 显式删除标准 Authorization
headers.delete("x-authorization");
headers.set("X-Trace-Id",    physicalRequestId);
headers.set("X-Request-Id",  physicalRequestId);
headers.set("X-Request-Model", connection.model);
headers.set("X-Client-Type", "pc");
headers.set("x_trace_id",    "autoclaw-desktop");   // 下划线，非连字符，与其它头不一致
if (scope.kind !== "connection_probe") headers.set("X-Session-Id", scope.sessionId);
if (scope.kind === "run")              headers.set("X-Query-Id", scope.runId);

// 鉴权走自定义头
providerApiKey: "zwork-managed-model"
headers: {
  "Authorization":  null,
  "X-Authorization": `Bearer ${accessToken}`
}
```

**这是关键设计点**：标准 `Authorization` 被删掉，token 走 `X-Authorization`。这说明上游网关在鉴权前会先做一层**客户端识别**（`X-Auth-Sign` + `X-Client-Type` + `X-Product`），然后用 `X-Authorization` 做账号鉴权。两层分离。

### 流式与队列

- `MANAGED_MODEL_QUEUE_SSE_EVENT = "zwork_queue"` —— SSE 流里除了内容还有**排队事件**，说明网关有并发排队机制
- 响应观察器 `observeZworkManagedModelResponse` 会识别 `accessCredentialRejected`（token 失效）与 `failureSignal`（provider 失败）
- token 被拒时 `resolveRecovery` 会调 `recoverRejectedAccessCredential` 刷新后重试一次

## 四、账号鉴权链路

`src/main/product-account/`：

| 端点 | 用途 |
|---|---|
| `POST /userapi/v1/agent-send-code` | 发短信验证码（body: `{phone}`） |
| `POST /userapi/v1/agent-login` | 验证码登录（body: `{phone, code:Number}`，企业包加 `select_identity:true`） |
| `POST /userapi/v1/refresh` | 刷新 token（主） |
| `POST /userapi/v1/agent-refresh` | 刷新 token（兜底） |
| `GET  /userapi/v1/user-info` | 用户信息 |
| `GET  /userapi/v1/user-profile` | 用户档案 |
| `POST /userapi/v1/agent-identity-switch` | 切换身份（个人/企业） |
| `POST /userapi/v1/agent-identity-enter` | 进入身份 |
| `GET  /userapi/v1/agent-identities` | 身份列表 |
| `POST /userapi/v1/agent/context-profile` | 上下文配置 |

刷新策略：

```js
PRIMARY_REFRESH_PATH  = "/userapi/v1/refresh"
FALLBACK_REFRESH_PATH = "/userapi/v1/agent-refresh"
REFRESH_TIMEOUT_MS = 15000
MAX_REFRESH_RESPONSE_BYTES = 256 * 1024
```

登录返回的 `token` 字段在 `identity_selection` 场景下是**空串**（企业用户需要先选身份），代码里有明确注释「绝不能当登录成功」。

### Token 存储

用 Electron `safeStorage`（DPAPI on Windows）加密：

```js
// src/main/product-account/product-account-encryption.ts
createElectronProductAccountEncryption() → {
  isAvailable: () => safeStorage.isEncryptionAvailable(),
  encrypt: (plaintext) => safeStorage.encryptString(plaintext),
  decrypt: (ciphertext) => safeStorage.decryptString(ciphertext)
}
// Linux 上若后端是 basic_text 则视为不可用
```

**含义**：token 是 DPAPI 绑定当前 Windows 用户的密文，不能直接拷走复用。

## 五、与 ZCode 的关系

包名 `zwork`，代码里大量 `zwork-agent`、`zwork-managed-model`、`zwork-bootstrap`。同时存在：

```
packages/zwork-agent/src/capabilities/model-connections/zcode-provider-templates.ts
```

说明 **AutoClaw 与 ZCode 共用同一套 agent 框架**（zwork-agent），ZCode 是它的 provider 模板来源之一。这也解释了为什么它同时内置 GLM / DeepSeek / OpenRouter / Bedrock 等多厂商端点。

## 六、下一步

1. **renderer 层**：`dist/renderer/` + `assets`，看前端 API 调用是否还有未在主进程出现的端点
2. **autonomous 目录**：`dist/resources/autonomous/` 有 `execution-worker.cjs` 与 `release-manifest.json`，是沙箱执行层
3. **技能协议**：`skill-service-contract.ts` 定义 `/skill-service/invoke` + 三个 `x-zwork-skill-*` 头
4. **运行验证**：抓 `AutoClaw-official` 用户数据目录里的日志，确认实际请求形状
5. **判断可反代性**：关键是 `X-Authorization` 的 token 能否在体外复用（受 DPAPI 限制），以及网关是否还有未发现的客户端校验
