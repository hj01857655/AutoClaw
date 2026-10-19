# AutoClaw2-06 Renderer 与窗口安全模型

状态：A（实测）。

## Renderer 构建

- 入口 `dist/renderer/zwork.html`：`<html lang="zh-CN" data-theme="light">`，`<body data-skin="ember">`，title "AutoClaw"，meta description "AutoClaw native agent workbench"。
- 主 bundle `assets/zwork-DEhamXD4.js`，预载 `session-history-D3RznYo3.js`（7.1MB，最大 chunk——会话历史是前端重心）。
- assets 共 786 文件 / 36.2MB：含 highlight.js 语言分块（双份，疑似两套主题/按需 chunk）、pdf.worker、pptx-preview、cynefin（图可视化）、wasm chunk 等。
- Vite 构建产物（hash 命名 + modulepreload）。

## 主窗口（createZworkMainWindow）

```
默认 1400×900（设计稿），minWidth 664 兜底 workArea，minHeight 600
mac: titleBarStyle hiddenInset + vibrancy under-window
win: frame true + autoHideMenuBar
webPreferences:
  preload: zwork-bootstrap-preload.cjs
  contextIsolation: true
  nodeIntegration: false
  sandbox: false          ← preload 需要 require 权限（bootstrap API 面大）
  webviewTag: true        ← 内置浏览器 tab 以 <webview> guest 挂载
  partition: 按账号分区（rendererSessionPartition）
```

导航/弹窗治理（实测代码）：
- `setWindowOpenHandler` → 一律 deny，URL 转 `openExternalUrl`（系统浏览器）。
- `will-navigate` → 只放行 `isRendererUrl()` 与 `isZworkArtifactUrl()`，其余 preventDefault + 外开。
- `page-title-updated` preventDefault（标题由产品控制）。
- webview guest 防线：`installGuestAttachGuard`（will-attach-webview）——partition 白名单、剥除 guest preload、强制 sandbox preferences（代码注释原文承认这是防 attack surface 扩大的关键）。
- 窗口角色标记：`main` / `account_candidate` / `auth_gate_candidate`（登录/账号窗口的加载超时与慢加载告警分别配置）。
- 关闭行为：`hideMainWindowOnClose` 可配置（托盘驻留）。

## 自定义协议 `zwork-artifact://`

- 权限：standard + secure + supportFetchAPI + stream + corsEnabled，`bypassCSP:false`，无 SW。
- URL 形态：`zwork-artifact://<documentId>/html?phase=streaming|final`、`.../video`、`.../pdf`、资产路径任意（扩展名白名单见下）；documentId ≥32 字符。
- **双阶段 CSP**：
  - streaming：`default-src 'none'; script-src 'none'; style-src 'unsafe-inline'; img-src data:; connect-src 'none'; form-action 'none'; base-uri 'none'`
  - final：放开 `script-src 'unsafe-inline'` 与 `img-src data: https: zwork-artifact:`（流式阶段保持 script 全禁 + img 仅 data:）。
- 资产 Content-Type 白名单：png/jpg/jpeg/gif/webp/avif/bmp/ico/svg，其余 404（注释 BUG-0043）；资产上限 16MB，chunk 8MB。
- 响应头固定 nosniff + no-store。
- 注释内嵌决策记录：`ADR-0110 (#674-B, owner 2026-09-02)`——正式 artifact 允许外链 https 图片，流式不允许。内部 issue 号 #674-B/BUG-0043 可作为代码考古锚点。

## 未决问题

- `isRendererUrl` 的精确白名单（file:// 路径 vs vite dev URL vs zwork.local）。
- BrowserTool 内置浏览器的 webview partition 命名与 cookie 隔离（815737 行第二处 webPreferences，疑为 browser 窗口/ Artifact 窗口）。
- renderer 登录页背景 SVG 2.2MB（zwork-login-z-bg）——纯视觉资产，无安全意义但说明登录页走本地资源。
