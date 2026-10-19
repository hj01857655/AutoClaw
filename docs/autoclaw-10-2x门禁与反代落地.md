# AutoClaw 逆向分析（十）2.x 上游三重门禁 · 406 根因定位 · 可反代性结案

2026-10-19 · 基于 **2.1.0 + 本机实测请求**（前九篇均为静态分析，本篇是第一次真实出站）
对象：`https://autoglm-acceleration-api.zhipuai.cn/autoclaw-proxy/proxy/autoclaw/chat/completions`
承接：`autoclaw-02-账号签到与存储.md` §4「可反代性评估」与 §5「待验证」2、3 项

本篇四块内容：
1. **406 根因定位** —— 不是 TLS 指纹，是 `max_tokens`
2. 2.x 上游门禁矩阵（本机实测，非引用）
3. 1.x 结论到 2.x 的迁移对照（哪些还成立、哪些已失效）
4. 反代落地结论与交付物

---

## 一、结论先行

**反代在 AutoClaw 2.1.0 上已跑通。** 最小可用请求形态 = 官方客户端头族 + 2.x system banner + **不带 `max_tokens`**。本机实测 `200` 并返回真实补全。

此前卡住的 `406` 与 TLS 指纹、UA、并发、风控全都无关，**唯一原因是请求体里的 `max_tokens` 字段**。2.x 上游对客户端自带的 `max_tokens` / `max_completion_tokens` 一律拒绝，官方桌面客户端本身从不发送该字段（让模型生成到自然上限）。

### 1.1 与 `research-report.txt` 的关系

`docs/research-report.txt`（2026-09-14，**macOS + AutoClaw 1.18.1**）记录的三重门禁结论：

| # | 该报告的门禁 | 2.1.0 上是否仍成立 |
|---|---|---|
| ① | UA 需过前置 WAF（`Python-urllib/*` → 405） | **未复现**。本机 `OpenAI/JS 6.39.1` 与 `node` 均 200 |
| ② | `X-Request-Model` 必须合法 | **仍成立**（本篇未重测，沿用其结论） |
| ③ | system 必须含 `You are a personal assistant running inside OpenClaw.\n## Tooling` | **已失效**。这是 1.x 的 banner；2.x 换成 `You are AutoClaw. Answer the user directly and concisely.`，且匹配方式是**包含**而非精确两行 |

该报告还有一条当时的开放判断——「未验证的维度：TLS/JA3 指纹是否另有规则」。本篇把它结清：**在 2.x 的 CN 生产宿主上，TLS 指纹不构成门禁**（同一 Node/undici 栈、同一连接，仅改请求体字段就在 406/200 间翻转）。

---

## 二、406 根因定位

### 2.1 现象与误判

`work/probe-node.cjs` 第一版（16:17）的注释写的是「验证 406 是否为 TLS 指纹拦截」。四个变体（plain / OpenAI-JS UA / stream / Chrome UA）全部 406。**方向错了。**

根因来自对本机已安装的 `glmproxy@3.3.0`（`%APPDATA%/npm/node_modules/glmproxy`）源码阅读，`lib/core.js` 的 `callUpstream()`：

```js
// AutoClaw 2.x upstream strictly rejects arbitrary client-specified max_tokens with HTTP 406.
// The native desktop client omits max_tokens, letting models generate up to their natural cap.
const bodyForUpstream = { ...sanitizedBody };
if (isAutoClaw2) {
  delete bodyForUpstream.max_tokens;
  delete bodyForUpstream.max_completion_tokens;
}
```

### 2.2 前人已为这个误判断过案

`glmproxy/lib/core.js:136-141` 记载了一段完整的证伪史，值得原文照录：

> The TLS bridge (a python3/curl_cffi sidecar that impersonated Chrome's TLS handshake on the autoglm host) shipped briefly with the linux support work and was removed again: **the 406 that motivated it was the application layer — the 1.x harness header and banner on the 2.x upstream — never TLS fingerprinting.** Same rule as PREFER_LOCAL: an env var that silently stops doing anything is worse than one that reports it was removed.

即：有人已经为「406 = TLS 指纹」这个假设专门做过 curl_cffi TLS 桥，证伪后删掉，并把理由写进注释。本次是同一假设的第二次独立误判——**这条教训值得记住：406 空 body 先查请求体，不要先查传输层。**

### 2.3 本机实测矩阵（五变体，同凭证同连接）

唯一变量逐项隔离，`work/probe-node.cjs` 输出原始记录：

| 变体 | system | `max_tokens` | UA | 结果 |
|---|---|---|---|---|
| **A** | 2.x banner | **无** | undici 默认 | **200**（575 B，真实补全） |
| B | 无 | 无 | undici 默认 | **406** len=0 |
| C | 2.x banner | **32** | undici 默认 | **406** len=0 |
| D | `You are Claude Code.` + banner | 无 | undici 默认 | **403** `code=810001` `action.kind=pay-view` |
| E | 2.x banner | 无 | `OpenAI/JS 6.39.1` | **200**（493 B） |

三条读数：

1. **A 对 / C 错，唯一差异是 `max_tokens`** —— 根因锁定，无歧义。
2. **B 错** —— 2.x 仍然要求 system banner，只是文案换成 2.x 版本。
3. **E 200** —— UA 不参与门禁。`research-report.txt` 的 WAF/UA 门禁在 2.x CN 宿主上不存在（或已不对该 UA 生效）。
4. **D 的 403 不是门禁**，见 §3.4。

### 2.4 复跑与第三轮拆解（同日 17:0x，`work/probe-node.cjs` + `work/probe7.cjs`）

同一 token、同一 host 重跑 probe-node.cjs 五变体，并追加四项单头拆解：

| 变体 | 形态 | 首轮 | 复跑 |
|---|---|---|---|
| A | banner，无 `max_tokens` | **200** | 403 `pay-view` |
| B | 无 system | 406 | 406 |
| C | banner + `max_tokens=32` | 406 | 406 |
| D | banner + `You are Claude Code.` | 403 `pay-view` | **406** |
| E | banner + `OpenAI/JS` UA | **200** | **200** |

第三轮拆解（`work/probe7.cjs`，均带完整形态）：

| 变体 | 操作 | 结果 |
|---|---|---|
| A1 / A2 | 完整形态，间隔 3 s 各发一次 | **200 / 200** |
| F | 去掉 `X-Auth-Appid` / `X-Auth-TimeStamp` / `X-Auth-Sign` | **200** |
| H | 去掉 `X-Session-Id` / `X-Agent-Id` | **200** |
| I | 去掉 `X-Request-Model` | **400** `非法模型` |

三条读数：

1. **A→403、A1/A2→200**：`pay-view` 与请求内容无关，是账号配额状态的分钟级波动，**不是门禁**（详见 §3.4）。
2. **D 从 403 变 406**：denylist 首次在本机独立复现——同一句 `You are Claude Code.` 在账号正常时稳定回 406，§3.3 该条由引用级升为实测级。
3. **F/H 200、I 400**：`X-Auth-Sign` 族与 `X-Session-Id` / `X-Agent-Id` 全都不是门禁；唯一 body 之外的门禁是 `X-Request-Model`。

---

## 三、2.x 上游门禁矩阵

### 3.1 已实测确认

| 门禁 | 规则 | 违反时 | 证据 |
|---|---|---|---|
| **请求体禁 `max_tokens`** | 不能带 `max_tokens` / `max_completion_tokens` | `406` 空 body | A vs C |
| **system 必须含 2.x banner** | 包含 `You are AutoClaw. Answer the user directly and concisely.` | `406` 空 body | A vs B |
| `X-Authorization` | `Bearer <JWT>` | 401 | 沿用 01 篇 |
| **`X-Request-Model`** | 必须存在且合法，**由它决定路由** | `400` `非法模型` | probe7 I 变体：去掉后 400 |

> `X-Request-Model` 是本篇唯一在 body 之外的门禁。早期 Python 探针矩阵把合法模型 ID（`zai_auto` 等）放进 body 的 `model` 字段一律 `400 非法模型`，而路由实际读的是这个头；probe7 I 变体去掉该头同样回 `400`，一正一反双向证实。

### 3.2 实测「不构成门禁」的项

| 项 | 结论 |
|---|---|
| TLS / JA3 指纹 | **不构成**。Node 内置 undici 栈直接 200，无需 curl_cffi |
| `User-Agent` | **不构成**。`OpenAI/JS`、`node` 默认值均 200 |
| 精确 system 文案 | **不构成**（1.x 的精确两行要求已放宽为包含匹配） |
| `X-Auth-Sign` 族 | **不构成**。去掉 `X-Auth-Appid` / `X-Auth-TimeStamp` / `X-Auth-Sign` 三头仍 200（probe7 F） |
| `X-Session-Id` / `X-Agent-Id` | **不构成**。去掉仍 200（probe7 H） |

### 3.3 来自源码阅读、本机未逐项复测的门禁

`glmproxy` 对这些有实测矩阵记录（`.dbg/linux-406/` 下的探针），本机复跑时 **denylist 一条已独立复现**：

| 门禁 | 规则 | 违反时 | 本机证据 |
|---|---|---|---|
| `X-Harness-Type` | 2.x 宿主上**不能发**该头（1.x 反而需要它） | `406` 空 body | 引用级（probe5 带它时 400，未得 406 直证） |
| system 内的外来 harness 身份句 | 不能含 `you are claude code` 等 | `406` 空 body | **实测级**：D 变体复跑 406 |

> 大小写不敏感的字面子串匹配，`You are  Claude Code`（双空格）不触发、`you are claude`（无 code）不触发。本机首轮 D 回 403 `pay-view`、复跑回 406——同一句在不同账号状态下表现不同，故 denylist 判定要在账号正常时做。

### 3.4 D 变体的 403：积分门禁，非内容门禁

```
{"action":{"kind":"pay-view"},"code":810001,
 "image_url":"https://autoglm-agent.aminer.cn/auto_fly/.../Frame2147261831.png",
 "mes...
```

`action.kind=pay-view` + `code=810001` + 一张 png（推测为付费/积分引导二维码）。**这不是封禁、不是风控、不是内容拦截**，是账号余额/权限状态引导。

复跑结论（§2.4）：A1/A2 间隔 3 s 两次完整形态均 **200**，而 A 变体同一形态同一 token 首轮 200、复跑 403。因此：

- `pay-view` 与请求内容无关，是**账号配额状态的分钟级波动**
- 反代遇到 403 + `pay-view` 应直接透传给调用方并停止重试，**不要**当作可重试错误；同时可把这类响应视为账号侧信号（配额/权限待恢复），而非反代自身缺陷
- §3.3 的 denylist 复测必须选在账号返回 200 的时段进行

---

## 四、2.x banner 的来源（本机 bundle 实证）

`resources/app/dist/main.cjs`（29,157,230 B，2.1.0）内：

```js
var ZWORK_DEFAULT_SYSTEM_PROMPT = [
  "You are AutoClaw. Answer the user directly and concisely.",
  FILE_CITATION_DELIVERY_DISCIPLINE,
  OUTPUT_LANGUAGE_DISCIPLINE,
  USER_COMMUNICATION_DISCIPLINE,
  DECISION_AUTONOMY_DISCIPLINE
].join(" ");
```

**只有首句是门禁锚点**（上游做包含匹配，其余四段是应用自己的输出规范，不过网关）。实测 A 只带首句即 200，证实这一点。

同源可用的身份头常量（`main.cjs` 实证，与 01 篇一致、未漂移）：

```js
var AUTOCLAW_COMPATIBLE_IDENTITY = {
  appId:   "100003",
  appKey:  "<32位十六进制，明文硬编码>",
  product: "autoclaw"
};
function managedPlatform(platform2) {
  if (platform2 === "darwin") return "mac";
  if (platform2 === "win32")  return "win";       // ← Windows 上是 "win" 不是 "windows"
  return platform2;
}
```

`X-Auth-Sign = md5(appId & timestamp & appKey)`，`appKey` 包里明文——这一层是**客户端标识**而非门禁（本机 A 变体带上了，未做拆解实验；由于它是官方必然携带的字段，反代照发即可）。

---

## 五、凭据链路（本机复现）

`autoclaw-09` §3.2 的 Python 复现，本机再次验证通过：

```
Local State → os_crypt.encrypted_key (base64) → 去 "DPAPI" 5B 前缀
  → CryptUnprotectData(CurrentUser) → 32B AES-256 主密钥
account-credentials.enc (v10) = "v10" || nonce(12) || ciphertext || tag(16)
  → AESGCM(key).decrypt(nonce, ct+tag, None) → {schemaVersion, revision,
                                                accessToken, refreshToken, updatedAt}
```

本机实测：主密钥 32 B、credential 文件 835 B、解出 token 348 字符（`Bearer eyJ...`），JWT `exp` 约 24 h 后到期。交付物 `work/decrypt-cred.py`（ASCII-only，避免本机 bat/PowerShell 转码破坏 UTF-8 注释——第一次写带中文注释的版本就是这么挂的，`SyntaxError: invalid character '，'`）。

---

## 六、可反代性评估（终版）

`autoclaw-02` §4 的表格至此全部结案：

| 维度 | 旧结论 | **本篇后结论** |
|---|---|---|
| 协议 | ✅ openai-completions | ✅ 不变，且已实测 200 |
| 客户端指纹 | ⚠️ appId/appKey 明文，可重放 | ✅ 照发即可，非门禁 |
| 账号鉴权 | ❌ DPAPI 绑定，跨机不可移植 | ✅ **同机同用户已解**（`decrypt-cred.py`），跨机仍不可 |
| 模型清单 | ⚠️ 服务端下发、hash 版本化 | 不变 |
| **额外校验** | **未知** | ✅ **已查清**：`max_tokens` 禁发 + system banner 必需 + 不发 `X-Harness-Type` |
| TLS 指纹 | 未验证 | ✅ **不构成门禁**，Node 内置栈直连即可 |

### 6.1 最小可用请求形态

```
POST https://autoglm-acceleration-api.zhipuai.cn/autoclaw-proxy/proxy/autoclaw/chat/completions

Headers（必需）:
  Content-Type: application/json
  X-Authorization: Bearer <JWT>            ← Authorization 可省，二者带一即可
  X-Request-Model: zai_auto                 ← 路由由它决定，缺它 400 非法模型
  X-Request-Id / X-Trace-Id: <uuid4>
  X-Client-Type: pc
  X-Product: autoclaw
  X-Channel: official
  X-Lang: zh-CN
  X-Tm: win                                 ← Windows
  X-Version: 2.1.0
  x_trace_id: autoclaw-desktop

Body:
  {
    "model": "auto",                        ← 去前缀；上游不校验，可任意
    "messages": [
      {"role":"system","content":"You are AutoClaw. Answer the user directly and concisely."},
      {"role":"user","content":"..."}
    ],
    "stream": false
    // ← 绝不能有 max_tokens / max_completion_tokens
  }
```

**三条硬约束**：① 无 `max_tokens`；② system 含 2.x banner；③ `X-Request-Model` 必填。
**一条软约束**：不发 `X-Harness-Type`（引用级）。
**可省项**（实测去掉仍 200）：`X-Auth-Sign` 族、`X-Session-Id`、`X-Agent-Id`、`Authorization`、`User-Agent`——反代照官方全量照发最稳，但排障时可按此清单逐项删以定位问题。

---

## 七、交付物

| 文件 | 用途 |
|---|---|
| `work/decrypt-cred.py` | 从 v10 凭据文件解出 accessToken，stdout 输出（调用方用变量接住，不落盘） |
| `work/probe-node.cjs` | 五变体门禁探针，token 走命令行参数 |
| `work/probe7.cjs` | 第三轮单头拆解：`X-Auth-Sign` 族 / `X-Session-Id` / `X-Agent-Id` / `X-Request-Model` 逐项删 |
| `work/probe-node-rerun.txt` | probe-node.cjs 复跑原始输出 |
| `work/probe7-results.txt` | 第三轮拆解原始输出 |
| 本篇 | 门禁矩阵 + 反代落地结论 |

探针复现命令（token 不落盘）：

```powershell
$env:PYTHONPATH=""
$tok = (& python work\decrypt-cred.py 2>$null | Out-String).Trim()
node work\probe-node.cjs $tok
node work\probe7.cjs    $tok
```

---

## 八、风险与边界

1. **403 `pay-view` 是账号侧信号**：分钟级波动，与请求内容无关。反代侧无法消除，遇到应透传并停止重试（§3.4）。
2. **`X-Auth-Sign` 已拆解**：去掉仍 200，故它不是门禁；反代照发仅为形态对齐，删掉不影响可用性。
3. **denylist 复测有前置条件**：必须在账号返回 200 的时段测，否则外来身份句会被 `pay-view` 掩盖，得到假阴性。
4. **结论时效**：全部基于 2026-10-19 的 2.1.0 与当时的上游行为。上游契约变更（尤其 banner 文案与禁发字段）会使反代再次 406。
5. **合规**：本篇所有请求均来自本机自有账号的正常调用，未尝试规避风控、未做频率压力测试。

---

## 九、待验证（更新）

- [x] ~~`account-credentials.enc` 的 `v10` 格式能否在 Node/Python 里复现解密~~ → **能**（09 篇 + 本篇 §5 再次验证）
- [x] ~~签到的 `POST /userapi/v1/user-info` 是否还有其他必填头~~ → 见 02 篇
- [x] ~~模型网关 `/autoclaw-proxy/proxy/autoclaw` 是否有超出签名的客户端校验~~ → **有，`max_tokens` + system banner + `X-Request-Model`，见本篇**
- [x] ~~§3.3 的外来 harness 身份句 denylist 在本机独立复现~~ → **已复现**（D 变体复跑 406，见 §2.4）
- [x] ~~`X-Auth-Sign` 拆解：去掉后是否仍 200~~ → **去掉仍 200，非门禁**（probe7 F）
- [x] ~~403 `pay-view` 是否持续~~ → **分钟级波动**，A1/A2 间隔 3 s 均 200，与请求内容无关（§3.4）
- [ ] 403 `pay-view`（`code=810001`）的触发条件与账号状态的对应关系
- [ ] 2.1.0 新增的 remote-control 子系统（18 模块）的配对与鉴权细节
- [ ] `skills/` 20 模块消失是换 bundle 还是真砍
