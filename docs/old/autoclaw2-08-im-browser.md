# AutoClaw2-08 IM 通道与内置浏览器

状态：A（实测）。IM = 桌面产品的"IM 接入"能力（channel_hub 服务面 + 本地 provider host）；内置浏览器 = workspace 内嵌 webview 标签页。

## IM Provider 体系（zwork-im-provider-host.ts）

**Provider 顺序**：`feishu, telegram, whatsapp, discord, wecom, wechat`

**渠道按发行变体分流**：
- 国内（domestic）：`feishu, wechat`
- 海外（oversea）：`feishu, telegram, whatsapp, discord`
- wecom 在 IM_PROVIDER_ORDER 里但不在两个 channel 列表 → 预留。

**WeChat 通道**（@wechatbot/wechatbot@2.2.0，官方 ilink AI Bot 协议）：
- API base `https://ilinkai.weixin.qq.com`，媒体 CDN `https://novac2c.cdn.weixin.qq.com/c2c`
- 登录：扫码流 `POST /ilink/bot/get_bot_qrcode?bot_type=3`（带 local_token_list）→ 轮询 `get_qrcode_status`（处理 `scaned_but_redirect` IDC 重定向，最多刷 3 次，间隔 2s）→ 得 `{bot_token, ilink_bot_id, ilink_user_id}`
- 收发：`/ilink/bot/getupdates`（长轮询 40s，游标 get_updates_buf）→ `/ilink/bot/sendmessage`；`/getconfig`、`/sendtyping`（typing_ticket）、`/msg/notifystart|notifystop`、`/getuploadurl`
- AuthorizationType: `ilink_bot_token`；credential authority host 白名单 = ilinkai.weixin.qq.com
- 杀开关：env `ZWORK_WECHAT_ENABLED=0`
- 凭据存储键域：`owner:{ownerId}:wechat:ilink[:namespace]`
- 消息模型：TEXT/IMAGE/VOICE/FILE/VIDEO，NEW/GENERATING/FINISH 状态

**Provider Host 架构**：
- `IM_PROVIDER_DESCRIPTORS` 每通道声明 configFileName（如 wecom-connection.json / whatsapp-connection.json / wechat-connection.json）、attachments 支持位、view sanitize 函数、construct 工厂
- 每通道 connection 服务从 `HostSecretStore(<runtimeData>/owner-secrets.enc)`（safeStorage 加密）取 token
- 飞书单独有 InstallerService（registerApp + 能力审计 ZworkFeishuCapabilityAuditor，domain=feishu/lark）+ 多 bot 目录（ADR-0121：listConnections → 内核 im_bots）
- 出站投递统一走 imDeliveryPump（outbox 表见 docs/07），应答流经 imAnswerStreamRelay
- 入站：imIngress.submitNormalizedIngress 归一化后进 agent（发送方审计埋点同源）

## 内置浏览器（visible-browser）

**Guest webview 准入**（installGuestAttachGuard，will-attach-webview）：
- partition 必须匹配 `^persist:zwork-native-browser-[0-9a-f]{40}$`（每会话一个持久分区，40 hex = 大概率 sha1/session id）
- src 必须为空或 about:blank（真正的导航由主进程 gate 放行后进行）
- guest webPreferences 强制：剥 preload、nodeIntegration=false（含 subFrames/Worker）、contextIsolation=true、sandbox=true、webSecurity=true、禁不安全内容、webviewTag=false、plugins=false

**预采纳请求门**（pre-adoption request gate）：
- session.onBeforeRequest 拦 `http/https/ws/wss://*/*`
- 采纳前（10s ADOPTION_TIMEOUT）所有请求经 `validateBrowserTarget` 校验（协议白名单 + resolveHost 检查，allowTestLoopback 仅测试），失败一律 cancel
- 采纳后（browser.adopt）正常放行 —— 即 gate 只约束"浏览器被 agent 接管前"的窗口期

**Browser IPC 词汇**（zwork:desktop:browser:invoke）：`browser.open / adopt / navigate / refresh / back / forward / control / release / close`
- `browser.control` 是 agent 驱动浏览器操作的面（配合 per-session 分区隔离与 cookie 隔离）
- identityKey = `workspaceId\0sessionId` —— 浏览器会话与工作区绑定

## 安全观察

- 每会话独立 persist 分区 → cookie/storage 天然隔离，防跨会话追踪；但持久化分区意味着登录态落盘。
- gate 的意义：防止 agent 在未采纳状态下诱导 webview 加载任意 URL（SSRF 式探测内网）；采纳后信任建立。
- IM token 全部经 DPAPI 加密（owner-secrets.enc），唯独 Office 桥 pairings.json 的 32B 共享密钥是明文 JSON（见 docs/04）。

## 未决问题

- `browser.control` 的具体动作集（点击/输入/截图？）在 preload zwork-desktop-browser.ts 侧，未逐条抄。
- 飞书事件订阅模式（websocket 长连接 or webhook 回调）未确认。
- validateBrowserTarget 对内网地址的具体判定（私网 IP？DNS rebinding 防护?）。
