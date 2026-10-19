# AutoClaw 逆向分析（九）egress 审计 · 2.1.0 版本 diff · 凭据解密落地

2026-10-19 · 承接 `autoclaw-02-账号签到与存储.md`（其「四、可反代性评估」与「五、待验证」的 1、2 项在本篇结案）

对象：AutoClaw 2.1.0（`AutoClaw2/resources/app/dist/main.cjs`）+ 本机实测运行期数据
（`%APPDATA%/AutoClaw-official/accounts/<hash>/`）。

本篇三块内容：
1. `egress.decision` 审计事件（168 条）的结构、指纹口径与判定逻辑
2. 2.0.1 → 2.1.0 模块增删（+84 / −36 个应用模块）
3. **凭据解密落地**：`v10` 格式 = Chromium os_crypt，密钥在 `Local State`，已在 Python 侧完整复现

---

## 一、egress.decision 出站审计

### 1.1 事件结构

`work_events` 表 `type='egress.decision'`，共 **168 条**（全部为 `subject=model`）。
payload 七个字段，**每条都齐全**（无缺字段事件）：

```json
{
  "subject": "model",
  "decision": "allowed",
  "route": "zwork-managed:managed-catalog:4382c61e4ed3f9470900bbe331ffee...",
  "provider": "zwork-managed",
  "purpose": "model_prompt",
  "classification": "workspace_conversation",
  "fingerprint": "6d8d87e9b421568dec26d0c1d85edc4b35514e5c8f3077af534b2d5b589c..."
}
```

### 1.2 字段取值分布

| 字段 | 取值 | 条数 |
|---|---|---|
| `subject` | `model` | 168 |
| `decision` | `allowed` | 168 |
| `provider` | `zwork-managed` | 168 |
| `purpose` | `model_prompt` | 168 |
| `classification` | `workspace_conversation` | 168 |
| `route` | `zwork-managed:managed-catalog:4382c61e…` | 139 |
| `route` | `zwork-managed:managed-catalog:fb57f1e5…` | 29 |

两个观察：

1. **168 条全是 `allowed`，没有一条 `blocked`/`approval_required`**。这不是"策略宽松"，而是审计事件的语义——`work_events` 记的是**实际发生了的出站**，被拦的请求走 `MODEL_EGRESS_FAILURE_CODES` 的异常路径（见 1.4），不进 `work_events`。
2. **同一 `provider` 出现两个 `route`**（139 : 29）。`route` = `<provider>:managed-catalog:<hash 前 16>`，hash 对应 `model_connections.connection_revision` 的 `managed-catalog:v1:<sha256>`。**两个 hash 说明本机用过的模型连接分属两版服务端目录**——2.1.0 升级后模型清单刷新过一次，旧目录的连接记录仍留旧 hash。（`autoclaw-02` §3.3 记的 `connection_revision` 即此字段。）

### 1.3 常量（main.cjs 字面量）

```js
var MODEL_EGRESS_POLICY_ACTION  = "model_egress";
var MODEL_EGRESS_PURPOSE        = "model_prompt";
var MODEL_EGRESS_CLASSIFICATION = "workspace_conversation";
var IM_GUEST_MODEL_EGRESS_CLASSIFICATION = "im_guest_conversation";
var MODEL_EGRESS_FAILURE_CODES = {
  blocked:           "model_egress_blocked",
  approval_required: "model_egress_approval_required",
  stale:             "model_egress_stale"
};
```

**`classification` 只有两个值**：桌面端 `workspace_conversation`，IM 访客 `im_guest_conversation`。实测只出现前者。

### 1.4 判定逻辑（`DefaultModelEgressDecider.decide`）

```js
decide(request) {
  const base = {
    route:          modelEgressRouteLabel(request.connection),
    provider:       request.connection.provider,
    purpose:        request.purpose,
    classification: request.classification,
    fingerprint:    modelEgressBindingFingerprint(request)
  };
  if (!request.ownerId || !request.payloadFingerprint
      || request.purpose !== MODEL_EGRESS_PURPOSE) {
    return { ...base, decision: "blocked" };              // ①
  }
  const guestScope = request.guestForm === true
    && request.workspaceId === null
    && request.classification === IM_GUEST_MODEL_EGRESS_CLASSIFICATION;
  if (request.workspaceId === null && !guestScope) return { … };   // ② 无工作区非访客 → 拦
  …
}
```

**fail-closed 是这套机制的主基调**，共三层：

| 触发 | 结果 |
|---|---|
| 缺 `ownerId` / 缺 `payloadFingerprint` / `purpose` 不是 `model_prompt` | `blocked` |
| `workspaceId === null` 且不满足 IM 访客三条件 | `blocked`（桌面端没工作区就别出网） |
| 判定器本身不可用（`failClosedModelEgressDecision`） | `blocked`，`route = "model_egress_decider_unavailable"`，`fingerprint = sha256("model_egress_decider_unavailable")` |

第三层值得单独说：**判定器自己挂了也返回一条结构完整的 blocked 决策**（不是抛异常、不是放行），并把 `route` 打成固定哨兵值。所以"策略组件故障"和"策略拒绝"在审计上是可区分的。

### 1.5 指纹口径（`modelEgressPayloadFingerprint`）

```js
function modelEgressPayloadFingerprint(input) {
  return sha2562(canonicalJson({
    prompt:              sha2562(input.prompt),
    systemPromptBase:    sha2562(input.systemPromptBase ?? ""),
    systemPromptAdditions: (input.systemPromptAdditions ?? []).map(sha2562),
    context: input.context.map(item => ({
      contextItemId: item.contextItemId,
      contextSeq:   item.contextSeq,
      role:         item.role,
      text:         sha2562(item.text)
    })),
    ...(input.imageContentDigests?.length > 0 ? { imageContentDigests: [...input.imageContentDigests] } : {}),
    ...(input.requestHeaders !== undefined ? { requestHeaders: { ...input.requestHeaders } } : {})
  }));
}
```

要点：

- **双层 sha256**：叶子先各自哈希，再对 `canonicalJson` 序列化整体哈希。原文明文从不落库。
- `context[]` 保留 **`contextItemId` 与 `contextSeq` 明文**（不哈希）——同一段文本换个上下文位置，指纹就变。这是有意的：指纹要能反映"这次出站内容"的**结构性差异**，而不只是内容摘要。
- `imageContentDigests` **为空则整个键不出现**（不是空数组）——避免把"没有图片"和"有空图片列表"混成同一种指纹。
- `requestHeaders` 原样展开（浅拷贝，不再哈希）——如果启用，头字段直接进指纹输入。
- `systemPromptAdditions` 逐条哈希后成数组，顺序敏感。

**实测指纹 168 条全不相同**（`fingerprint` 字段 168 个唯一值），与"每次请求 prompt 不同"一致。指纹不是去重键，是**绑定键**：把"哪一次出站、发了什么"锚定到审计记录上。

### 1.6 与旧认知的差异

`autoclaw-03-IPC面.md` 系列把出站面按网络栈梳理，但 `egress.decision` 这层**应用策略审计**当时没有覆盖。补上后的完整出站链路是：

```
Renderer/Main 请求模型
  → DefaultModelEgressDecider.decide()      ← 应用层策略（本篇）
      ├─ blocked → MODEL_EGRESS_FAILURE_CODES 异常路径，不进 work_events
      └─ allowed → 写 work_events(egress.decision)  ← 168 条即此
  → 签名头（X-Auth-Sign = md5(appId&ts&appKey)）
  → 网关 /autoclaw-proxy/proxy/autoclaw
```

即：**`work_events` 里的 egress 记录是"已放行"的快照，不是"全部尝试"的日志**。分析访问控制时不能拿它当分母。

---

## 二、2.0.1 → 2.1.0 模块 diff

方法：从 `main.cjs` 的 `// src/...` / `// ../packages/...` 注释里提取模块路径，与 2.0.1 提取结果（`work/main-src-paths.txt`，3691 条）对比，本次提取 2782 条，交集 2260。

> **口径说明**：两次提取的正则宽窄不同（旧 3691 / 新 2782），且 bundler 会把**无注释的模块**吞掉，所以绝对值不可比，只能看 `src/` 与 `packages/` 层的**净增减**。下表只统计应用自有代码，不含 node_modules。

### 2.1 净增：84 个 `src/main/**` 模块

| 子系统 | 新增 | 规模 |
|---|---|---|
| **remote-control/** | `bridge-device-api`、`ws-device-socket`、`device-command-dispatcher`、`pair-link`、`pending-controllers`、`remote-artifact-read`、`remote-attachments`、`remote-control-gate`、`remote-control-analytics`、`remote-control-device-client`、`remote-control-device-lifecycle`、`remote-helpers`、`remote-history`、`remote-model-list`、`request-id`、`subscription-forwarder`、`two-way-command`、`workspace-dir-browse` | **18** |
| **im/** | dingtalk ×10（`dingtalk-connection-service`、`dingtalk-delivery`、`dingtalk-media`、`dingtalk-owner-pairing`、`dingtalk-physical-ingress`、`dingtalk-scan-create`、`dingtalk-scan-ipc`、`dingtalk-transport-protocol`、`dingtalk-transport`、`dingtalk-config-store`）；wecom ×5；feishu ×2；whatsapp ×1 | **18** |
| **zwork-autoclaw-migration-*** | `preview`、`preview-summary`、`apply-report`、`artifact-batch`、`directory-expand`、`failure-summary`、`im-phase`、`im-port`、`im-summary`、`user-summary`、`adopt-landing` | **11** |
| **data-location/** | `zwork-data-location`、`-ports`、`-path-rewrite`、`-migration-ui` | **4** |
| **feedback/** | `zwork-feedback-scheduling`、`zwork-feedback-attachments`、`zwork-diagnostic-log-file`、`zwork-general-feedback-submitter` | **4** |
| **agent-config/context-profile-*** | `-api`、`-cache`、`-lifecycle` | **3** |
| **desktop/** | `app-focus/zwork-desktop-app-foreground`、`attachments/zwork-workspace-attachment-faces`、`html-artifact/committed-html-preview-source`、`instrumentation/model-queue-analytics`、`visible-browser/visible-browser-ua`、`zoom/zwork-interface-zoom` | **6** |
| **model-connections/catalog/** | `built-in-model-catalog-assembly` | **1** |
| **i18n/** | `zwork-migration-*` 翻译 ×6 | **6** |
| **zwork-agent/** | `model-queue-analytics-sink`、`scheduled-task-analytics`、`zwork-ledger-connection`、`zwork-third-party-reasoning-rules` | **4** |
| **app/** | `zwork-remote-control-integration`、`office-plugin/office-pane-attachment-ops`、`legal/legal-imported-session-documents` | **3** |
| **shared/** | `model-catalog-selection`、`model-gating`、`reasoning-experiment`、`remote-control-experiment`、`scheduled-task-analytics-properties`、`science-scene-experiment` | **6** |

**2.1.0 的三个真正主题**：

1. **remote-control 从零到 18 个模块** —— 这是最大变化，意味着 2.1.0 把「手机/第二设备接管桌面端」做成了完整子系统（配对、双工命令、订阅转发、模型列表远程读取、产物远程读、工作区目录浏览）。`shared/remote-control-experiment.ts` + `app/zwork-remote-control-integration.ts` 说明它有实验开关。
2. **IM 接入面铺开（18 个）** —— 钉钉从 2.0.1 的雏形长成 10 模块（含独立 `transport-protocol`/`transport` 分层、扫码建连、媒体、Owner 配对），企微 5、飞书 2、WhatsApp 1。
3. **`zwork-autoclaw-migration-*`（11 个）** —— 2.1.0 内置了**从旧版 AutoClaw 迁移**的成套逻辑（会话/工件/IM/身份分阶段导入 + 摘要报告 + 失败汇总）。

### 2.2 净减：36 个 `src/main/**` 模块

| 子系统 | 消失 | 规模 |
|---|---|---|
| **skills/** | `preinstall-ledger`、`preinstall-skill-policy`、`preinstall-state`、`preinstalled-skill-content`、`preinstalled-skill-deployer`、`preinstalled-skill-manifest`、`installed-expert-skills`、`skill-capability-relay`、`skill-identity`、`skill-junk-files`、`skill-service-composition`、`skill-service-contract`、`skill-service-upstream`、`skill-tombstone-store`、`skill-upstream-docx`、`skill-upstream-engines`、`skill-upstream-file`、`skill-upstream-http`、`skill-upstream-jianwei`、`skill-upstream-tianyancha` | **20** |
| **legal/** | `legal-reference-resolver`、`legal-vendor-analytics`、`yuandian-legal-data-adapter` | **3** |
| **finance-data/** | `autoglm-finance-data-adapter`、`finance-data-authorization`、`finance-data-policies` | **3** |
| **web-search/ media-tools/** | `autoglm-web-search-adapter`、`response-normalizer`、`autoglm-media-tools-adapter`、`media-response` | **4** |
| 其他 | `pi-package-identity`、`fs/cache-directory-prune`、`guard/disable-native-clipboard-watcher`、`net/public-url-policy`、`shims/abort-controller.cjs`、`shims/node-fetch.cjs` | **6** |

**解读**：

- **skills 全家桶消失（20 个）是最大的减项**，尤其 `skill-upstream-*` 六个外部源适配器（docx / 文件 / HTTP / 简维 / 天眼查）整体不见。2.0.1 日志里 `skills.preinstall.deploy.decision` 一天 104 条的那套预安装机制，2.1.0 已不在 `src/`。要么整体挪去了别的 bundle，要么被服务端目录化（`built-in-model-catalog-assembly` 这类"内置清单装配"模块是同一思路）。
- **`web-search`/`media-tools`/`finance-data`/`legal` 四个 `autoglm-*` 前缀适配器消失**——这批是 AutoGLM 旧品牌的数据源适配层，2.1.0 清掉了。注意 `legal` 只减了 3 个却新增了 `legal-imported-session-documents`，是**换实现不是砍功能**。
- **两个 `shims/*.cjs`（node-fetch、abort-controller）消失** = 运行时已原生支持，不再打补丁。

---

## 三、凭据解密落地（结案「待验证 #1」）

### 3.1 结论先行

`autoclaw-02` §3.2 判断"magic `v10` 是 Electron `safeStorage` 在 Windows 上的格式（DPAPI + AES-GCM）"，方向对，但**漏了关键一环：密钥不在文件里，在 `Local State`**。这也是当时"裸 DPAPI blob 解不开"的原因。

**v10 完整格式**（Chromium os_crypt，Electron `safeStorage` 在 Windows 上的底层实现）：

```
文件:   "v10" || nonce(12) || ciphertext(N-16) || tag(16)
密钥:   Local State → os_crypt.encrypted_key (base64)
          → 去掉 "DPAPI" 5 字节前缀
          → CryptUnprotectData(CurrentUser)   ← 唯一的 DPAPI 环节在这里
          → 32 字节 AES-256 主密钥
```

即：**DPAPI 只保护那 32 字节主密钥，业务文件本身是 AES-256-GCM**。三个 `.enc` 全部按此解开。

### 3.2 Python 侧完整复现

```python
import io, json, base64, win32crypt, os
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

base = os.environ.get('AUTOCLAW_USER_DATA_ROOT', r'%APPDATA%/AutoClaw-official')

# ① 取主密钥
ls  = json.load(io.open(os.path.join(base, 'Local State'), encoding='utf-8'))
raw = base64.b64decode(ls['os_crypt']['encrypted_key'])
assert raw[:5] == b'DPAPI'
key = win32crypt.CryptUnprotectData(raw[5:], None, None, None, 0)[1]   # 32B

# ② 解业务文件
def dec(rel):
    b = io.open(os.path.join(base, rel), 'rb').read()
    assert b[:3] == b'v10'
    return AESGCM(key).decrypt(b[3:15], b[15:-16] + b[-16:], None)
```

本机实测三个文件全部成功：

| 文件 | 长度 | 明文长度 |
|---|---|---|
| `accounts/<hash>/account-credentials.enc` | 835 | 804 |
| `accounts/<hash>/runtime/context-profile.enc` | 622 | 591 |
| `accounts/<hash>/runtime/model-connection-command-claims.enc` | 171 | 140 |

### 3.3 凭据明文结构

```json
{
  "schemaVersion": 1,
  "revision": 2,
  "accessToken":  "Bearer eyJhbGciOiJIUzI1NiIs...",
  "refreshToken": "Bearer eyJhbGciOiJIUzI1NiIs...",
  "updatedAt": "2026-10-19T07:18:27.951Z"
}
```

JWT（HS256）本地解析：

| 声明 | accessToken | refreshToken |
|---|---|---|
| `user_id` | 141710 | 141710 |
| `device_id` | `fb1387bf-c351-4d3d-a3ca-fe5246dad23c` | 同 |
| `source_id` | `autoclawaccess_token` | `autoclawrefresh_token` |
| `jti` | `130****3007`（**明文手机号**） | 同 |
| `is_guest` / `power` | false / 0 | 同 |
| `iat` | 2026-10-19T07:18:57Z | 同 |
| **`exp`** | **2026-10-20T07:18:57Z（1 天）** | **2026-11-18T07:18:57Z（30 天）** |

三条要点：

1. **access 1 天、refresh 30 天**，与 `autoclaw-02` §2.3 的主/兜底双刷新路径配套——短 access + 长 refresh 是标准设计，意味着**抓到 refresh 就能长期续**。
2. **`jti` 是明文手机号**（形如 `130****3007`，与 `account-profile.json` 的掩码形式对应）。JWT 载荷本身零加密，任何拿到 token 的人都能读出完整手机号。
3. `device_id` 与 `device/device-id.json` 是同一个值，token 与设备绑定。

### 3.4 另两个文件

**`context-profile.enc`**（591 B）——`zwork-context-profile-v1`：

```json
{
  "format": "zwork-context-profile-v1",
  "revision": "2026.10.15-2",
  "issuedAt": "2026-10-15T12:00:00+08:00",
  "defaults": { "tools": {
      "mcp_science_chemistry_zinc_get_3d": "off",
      "mcp_science_chemistry_zinc_random_sample": "off",
      ...
  }}
}
```

外层键是 `["desktop-context-profile","server-context-profile","2"]`（三元素数组序列化成 JSON 字符串当 key）。这是**服务端下发的上下文配置**，带 `revision`/`issuedAt`，控制 MCP 工具默认开关。

**`model-connection-command-claims.enc`**（140 B）：

```json
{ "[\"0f46f185-65d4-49cd-8031-f7ddfb941ff5\",\"__zwork_model_connection_claim_key__\",\"v1\"]":
  "cQ-i7yebBhDX43oQF0O5mcvjyAhHVBc61xRBnPO8Rmw" }
```

键是 `<ownerId>` + 哨兵 + 版本，值是一个 claim 串。与 `model_connection_commands` 表配套，属于**模型连接命令的持有凭证**。

---

## 四、对「可反代性评估」的修订

`autoclaw-02` §4 的表格需要更新：

| 维度 | 旧结论 | **本篇后结论** |
|---|---|---|
| 协议 | ✅ openai-completions | 不变 |
| 客户端指纹 | ⚠️ appId/appKey 明文硬编码，可重放 | 不变 |
| 账号鉴权 | ❌ token 存 safeStorage（DPAPI），**跨机器不可移植** | ❌ **跨机器仍不可移植**（密钥绑 Windows 用户）；但**同机同用户已实测可解**（§3.2） |
| 模型清单 | ⚠️ 服务端下发、hash 版本化 | 不变；补充：本机已出现两版目录 hash（§1.2） |
| 额外校验 | 未知 | 补充 egress 三层 fail-closed 策略（§1.4）+ 指纹绑定（§1.5） |

**路径 A（同机解密）从"中，需复现密钥派生"变为"已复现"**。四选项表更新为：

| 路 | 做法 | 可行性（更新后） |
|---|---|---|
| A. 同机解密 | 同一 Windows 用户跑反代，按 §3.2 三行代码解出 accessToken；过期则用 refreshToken 走 `/userapi/v1/refresh` | **已验证**，无技术卡点 |
| B. 抓包复用 | 抓 `X-Authorization` 重放 | 仍可行，但不如 A 干净（A 不依赖代理/证书配置） |
| C. 走刷新链 | 拿 refresh token 续期 | A 的后续步骤，access 1 天 / refresh 30 天，续期窗口充裕 |
| D. 不反代 | 只参考协议设计 | 无合规风险 |

**安全含义**：任何以该 Windows 用户身份运行的进程都能解开这三个文件、拿到 Bearer token 和手机号。这不是漏洞，是 Electron `safeStorage` 的设计边界——**它防的是"文件被拷走"，不防"同用户代码读取"**。若 AutoClaw 的 Renderer 有 XSS/任意代码执行面，凭据即失守。

---

## 五、待验证（更新）

- [x] ~~`account-credentials.enc` 的 `v10` 格式能否在 Node/Python 里复现解密~~ → **能，见 §3.2**
- [ ] 签到的 `POST /userapi/v1/user-info` 是否还有其他必填头（如 `X-Auth-Sign` 时间窗限制）
- [ ] 模型网关 `/autoclaw-proxy/proxy/autoclaw` 是否有超出签名的客户端校验
- [ ] `reasoning_levels_json = ["low","high","max"]` 与实际可用的 `xhigh` 是否有出入
- [ ] 2.1.0 新增的 remote-control 子系统（18 模块）的配对与鉴权细节——**下一轮重点**
- [ ] `skills/` 20 模块消失是换 bundle 还是真砍（影响技能面结论）
