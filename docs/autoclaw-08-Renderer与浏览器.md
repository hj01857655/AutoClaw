# AutoClaw 逆向分析（八）Renderer 与浏览器隔离

2026-10-19 · 基于 **2.1.0** 实测（旧文档基于 2.0.1，本篇全部重新提取）

## 一、Renderer 构建

```
dist/renderer/
├── zwork.html      700 B
└── assets/         789 个文件
```

`zwork.html`（实测全文）：

```html
<!doctype html>
<html lang="zh-CN" data-theme="light">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <meta name="description" content="AutoClaw native agent workbench" />
    <title>AutoClaw</title>
    <script type="module" crossorigin src="./assets/zwork-CfldF7G5.js"></script>
    <link rel="modulepreload" crossorigin href="./assets/session-history-Dp6AmpRk.js">
    <link rel="stylesheet" crossorigin href="./assets/session-history-CPpXCcsE.css">
    <link rel="stylesheet" crossorigin href="./assets/zwork-Dm5oddhe.css">
  </head>
  <body data-skin="ember" data-theme="light">
    <div id="root"></div>
  </body>
</html>
```

**特征**：
- Vite 构建（`type="module"` + `modulepreload` + hash 文件名）
- React（`<div id="root">`）
- 主题体系：`data-theme` + `data-skin="ember"`
- 主入口仅 **36 KB**，靠 789 个 chunk 按需加载

## 二、窗口安全配置

实测四处 `webPreferences`，全部满足三项硬要求：

| 窗口 | contextIsolation | nodeIntegration | sandbox | 其他 |
|---|---|---|---|---|
| 主窗口 | `true` | `false` | `true` | `backgroundThrottling: false` |
| 导出管线窗口 | `true` | `false` | `true` | 注释：settle 屏障依赖 rAF，隐藏窗口默认节流会烧完 60s 预算 |
| 第三方 SDK 页 | `true` | `false` | `true` | 注释：只与腾讯 SDK 通信，不需要 node、不访问 zwork 桥 |
| 隔离会话页 | `true` | `false` | `true` | `webviewTag: false` + `session: isolatedSession` |

**四项安全开关（contextIsolation / nodeIntegration / sandbox / webviewTag）在全部窗口上一致**，没有为图方便开口子的地方。

## 三、自定义协议（三个）

```js
ZWORK_ARTIFACT_SCHEME                      = "zwork-artifact"
ZWORK_WORKSPACE_FILE_SCHEME                = "zwork-workspace-file"
ZWORK_WORKSPACE_FILE_SCHEME_CHROMIUM_ALIAS = "zwork--workspace-file"   // 双横线，避开 Chromium 对 scheme 名的规范化
ZWORK_DEEP_LINK_SCHEME                     = "zwork"
```

### 3.1 特权声明（`register-privileged-schemes.ts`）

```js
protocol.registerSchemesAsPrivileged([
  ZWORK_ARTIFACT_SCHEME_PRIVILEGES,
  ZWORK_WORKSPACE_FILE_SCHEME_PRIVILEGES,
  ZWORK_WORKSPACE_FILE_SCHEME_ALIAS_PRIVILEGES
]);
```

两个协议的 privileges 完全相同：

```js
{
  standard: true,
  secure: true,
  supportFetchAPI: true,
  stream: true,
  corsEnabled: true,
  bypassCSP: false,          // ← 关键：不绕过 CSP
  allowServiceWorkers: false // ← 关键：禁 ServiceWorker
}
```

`bypassCSP: false` 与 `allowServiceWorkers: false` 是正确取舍——很多 Electron 应用为了加载本地资源把 `bypassCSP` 打开，这里没有。

### 3.2 协议处理器

```js
function installZworkArtifactProtocol(electronSession, store, videoStreams) {
  electronSession.protocol.handle(ZWORK_ARTIFACT_SCHEME, async (request) => {
    if (videoStreams) {
      const served = await videoStreams.handleRequest(request);
      if (served !== null) return served;
    }
    return store.handleRequest(request);
  });
  // 可卸载：返回 unhandler
}
```

按 session 注册（`createZworkArtifactProtocolRegistration(defaultSession, store, videoStreams)` 用 Map 管理），支持多 session 各自隔离。

### 3.3 三套 CSP

**HTML_ARTIFACT_STREAMING_CSP**（流式写入阶段，最严）：
```
default-src 'none'; script-src 'none'; style-src 'unsafe-inline';
img-src data:; font-src data:; connect-src 'none';
form-action 'none'; base-uri 'none'
```

**HTML_ARTIFACT_FINAL_CSP**（提交后）：
```
default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline';
img-src data: https: zwork-artifact:; font-src data:;
connect-src 'none'; form-action 'none'; base-uri 'none'
```
代码注释：
> ADR-0110 (#674-B, owner 2026-09-02): committed first-party artifacts may load external https images (autoclaw alignment — picsum/brand/gallery URLs render). **Streaming stays `img-src data:`; http: and every non-image channel stay closed.**

**WORKSPACE_FILE_CSP**：
```
default-src 'self'; script-src 'self' 'unsafe-inline';
style-src 'self' 'unsafe-inline';
img-src 'self' data: blob:; font-src 'self' data:;
media-src 'self' data: blob:; frame-src 'self';
connect-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none'
```

**评价**：三套 CSP 分开是正确设计——流式阶段的产物是**模型正在生成的 HTML**，必须最严（`script-src 'none'`）；提交后才放宽到 `'unsafe-inline'`。**流式期禁止脚本执行**这一条尤其关键，否则模型生成的内容（可能含注入）会在写入过程中就执行。

## 四、内置浏览器（visible-browser）

### 4.1 模块（20 个）

```
create-zwork-visible-browser.ts        zwork-visible-browser.ts
zwork-visible-browser-composition.ts   zwork-visible-browser-ipc.ts
zwork-browser-actions.ts               zwork-browser-cdp.ts
browser-adoption-diagnostics.ts        browser-dialog-dispatch.ts
browser-element-actions.ts             browser-locator.ts
browser-page-operations.ts             guest-attach-guard.ts
guest-navigation.ts                    download-approval.ts
visible-browser-context-menu.ts        install-visible-browser-context-menu.ts
visible-browser-contract.ts            visible-browser-ua.ts
zwork-workspace-file-authority.ts      browser-agent-skill-updater.ts
```

### 4.2 是"接管"而非"新建"

IPC 命令 `browser.adopt` / `browser.release`（另有 `adopt` 关键字 460 次）说明它**接管用户已有的浏览器实例**，用完释放。`browser-adoption-diagnostics.ts` 专门做接管诊断。

### 4.3 UA 伪装

```js
function chromeLikeUserAgent(chromiumVersion = process.versions.chromium ?? "130.0.0.0") {
  const platform = process.platform === "darwin"  ? "Macintosh; Intel Mac OS X 10_15_7"
                 : process.platform === "win32"   ? "Windows NT 10.0; Win64; x64"
                 :                                  "X11; Linux x86_64";
  return `Mozilla/5.0 (${platform}) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/${chromiumVersion} Safari/537.36`;
}
```

**用 `process.versions.chromium` 而非硬编码**，UA 与真实内核版本一致——这是防"UA 与 TLS/JS 指纹不一致"的正确做法（很多自动化工具硬编码版本号，反而暴露）。

### 4.4 CDP 快照

`zwork-browser-cdp.ts`：

```js
var AX_NODE_LIMIT = 1e5;          // 无障碍树节点上限 10 万
var ELEMENT_PAGE_SIZE = 200;      // 元素分页 200/页
var INTERACTIVE = new Set([
  "button","link","textbox","searchbox","combobox","checkbox","radio",
  "tab","menuitem","switch","slider","spinbutton","listbox","option","region"
]);
```

`snapshotFromAxTree` 把 CDP 的 AX 树压成 ref + text 两部分，`name`/`value` 各截 512 字符，节点超限时 `textCapped` 置位。

### 4.5 敏感字段识别

```js
var BROWSER_SENSITIVE_PATTERN =
  "(?:password|passcode|credential|authorization|(?:api[ _-]?key)|" +
  "(?:^|[^a-z])(?:token|secret|payment|card|cvv|ssn|pin|otp)(?:$|[^a-z])|" +
  "cc-number|cc-csc|one-time-code)";
```

元素探针里对 `[e.type, e.name, e.id, name, e.autocomplete].join(' ')` 做正则匹配，命中即标 `sensitive`。

**这是浏览器自动化的关键防护**——识别到敏感字段就不让模型自动填写。覆盖 password / OTP / CVV / 卡号 / SSN 等，且用 `(?:^|[^a-z])…(?:$|[^a-z])` 边界避免 `tokenizer` 这类误伤。

### 4.6 可见性探针

```js
const visible = attached && st.display !== 'none' && st.visibility !== 'hidden'
             && st.opacity !== '0' && r.width > 0 && r.height > 0;
```

五项都查，避免对隐藏元素做无效操作。

## 五、深链与产品标识

```js
var ZWORK_WINDOWS_APP_USER_MODEL_ID = "com.zhipuai.autoclaw2";
var ZWORK_DEEP_LINK_SCHEME = "zwork";
```

- 深链 scheme：`zwork://`
- Windows AppUserModelID：`com.zhipuai.autoclaw2`（注意是 `autoclaw2`，带 2）

## 六、IM 与浏览器之外的隔离

`Partitions/zwork-account-<hash>/` —— 每个账号一个独立 Electron partition，会话/存储/缓存互不串。这解释了数据目录里为什么有 `Partitions/` 子目录。

## 七、与 2.0.1 的差异

旧文档记录「zwork-artifact 协议 CSP」与「内置浏览器隔离」。2.1.0 实测补齐：

| 项 | 2.1.0 实测 |
|---|---|
| 自定义协议 | **三个**（artifact / workspace-file / workspace-file 的 Chromium 别名）+ 深链 `zwork://` |
| CSP | **三套**（流式 / 最终 / 工作区文件），流式期 `script-src 'none'` |
| 内置浏览器模块 | 20 个，含 CDP 快照与敏感字段识别 |
| UA | 用 `process.versions.chromium` 动态生成 |
| 窗口安全 | 四处 webPreferences 全部 contextIsolation+sandbox |

## 八、安全评价

**做得好的**：
1. 流式 HTML 的 CSP `script-src 'none'` —— 模型生成内容在写入过程中不执行
2. `bypassCSP: false` + `allowServiceWorkers: false` —— 没有为本地资源开口子
3. UA 取 `process.versions.chromium`，避免版本指纹不一致
4. 敏感字段识别覆盖银行卡/OTP/CVV/SSN，且带词边界防误伤
5. 所有窗口 `contextIsolation: true` + `sandbox: true` + `nodeIntegration: false`

**需要留意**：
1. `HTML_ARTIFACT_FINAL_CSP` 放开到 `script-src 'unsafe-inline'` —— 提交后的产物可执行内联脚本，若产物来源不可信则是 XSS 面
2. `img-src https:` 允许任意 https 图片 —— 可做外链追踪（虽非本应用独有）
3. `browser.adopt` 接管真实浏览器 —— 需确认接管的是独立实例还是用户日常浏览器的 profile

## 九、未决问题

- `browser.adopt` 的实际接管对象（独立 profile 还是用户主 profile）
- `zwork-workspace-file-authority.ts` 的授权模型
- `download-approval.ts` 的审批策略
- `guest-attach-guard.ts` 防的是什么（webview 附着？）
