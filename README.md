# AutoClaw2api

[![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/)
[![Flask](https://img.shields.io/badge/Flask-3.x-black.svg)](https://flask.palletsprojects.com/)
[![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20macOS-lightgrey.svg)](#快速开始)
[![Protocols](https://img.shields.io/badge/protocols-Chat%20%7C%20Anthropic%20%7C%20Responses-teal.svg)](#快速开始)

AutoClaw（澳龙）桌面端托管模型的本地 **OpenAI / Anthropic / Responses** 三协议网关。

把 AutoClaw 桌面客户端消费的云端模型包装成标准协议，任何支持自定义 Base URL 的客户端
指向本服务即可调用，无需改客户端代码。

分析依据见 [`docs/autoclaw-10-2x门禁与反代落地.md`](docs/autoclaw-10-2x门禁与反代落地.md)。

---

## 快速开始

```powershell
cd E:\VSCodeSpace\Agent\AutoClaw\AutoClaw2api
pip install -r requirements.txt
python server.py
```

启动即打印凭据状态与模型目录。默认监听 **http://127.0.0.1:8378/v1**。

**前置条件**：AutoClaw 桌面端已登录，且以**同一个 Windows 用户身份**运行本服务。
凭据文件由 Windows DPAPI 绑定到当前用户与机器，换用户或换机器都取不到明文——
这是设计使然，不是缺陷。本服务**只读**桌面端的凭据密文，不写入、不改写，
桌面端刷新登录态后按 mtime 自动跟随，无需手动干预。

客户端接入：

| 客户端 | Base URL | 协议 |
|---|---|---|
| 通用 OpenAI SDK | `http://127.0.0.1:8378/v1` | Chat Completions |
| Claude Code | `http://127.0.0.1:8378` | Anthropic Messages |
| Codex CLI / agents SDK | `http://127.0.0.1:8378/v1` | Responses |

```bash
# OpenAI Chat
curl http://127.0.0.1:8378/v1/chat/completions -H "Content-Type: application/json" -d '{
  "model": "zai_auto",
  "messages": [{"role":"user","content":"你好"}]
}'

# Anthropic Messages
curl http://127.0.0.1:8378/v1/messages -H "Content-Type: application/json" -d '{
  "model": "zai_auto", "max_tokens": 4096,
  "messages": [{"role":"user","content":"你好"}]
}'

# OpenAI Responses
curl http://127.0.0.1:8378/v1/responses -H "Content-Type: application/json" -d '{
  "model": "zai_auto", "max_output_tokens": 2048,
  "input": "你好"
}'
```

**`model` 填 `/v1/models` 里的 id，反代原样透传、不做任何改写。** 客户端本来就
都是从那里拉的，所以不需要解析也不需要映射。上游是唯一裁决者：它认得就 200，
不认得就 `400 非法模型`——那个来自上游的错误比本地猜"你大概想要哪个档"更有用。

唯一一条兜底在 `lanes/autoclaw.py` 的 `build_request()`：`model` 留空时补
`AUTOCLAW_DEFAULT_MODEL`，因为上游的 `X-Request-Model` 必填，而 OpenAI 协议里
`model` 是可选的。

---

## 三条硬约束（为什么必须这么做）

AutoClaw 2.x 上游有三条实测门禁，违反即失败，**且都在请求发出前就必须满足**——
返回的 406 是空 body，无法从响应区分是哪一条触发，不能靠响应码事后补救。

| # | 约束 | 违反时 |
|---|---|---|
| ① | 请求体**绝不能带** `max_tokens` / `max_completion_tokens` / `max_output_tokens` | `406` 空 body |
| ② | `system` 必须含 banner `You are AutoClaw. Answer the user directly and concisely.` | `406` 空 body |
| ③ | 头 `X-Request-Model` 必填，且必须是上游模型 id | `400` `非法模型` |

这三条在本服务里的落实位置：

- ①② → `lanes/autoclaw.py` 的 `sanitize_body()` + `ensure_banner()`，所有协议共用
- ③ → `lanes/autoclaw.py` 的 `build_request()`（`body.model` → `X-Request-Model`，
  留空补 `AUTOCLAW_DEFAULT_MODEL`）
- 额外：`system` 里的外来 harness 身份句（`you are claude code` 等）会被中和，
  否则上游同样回 406

客户端**照常填 `max_tokens`** 即可，删不删由本服务负责。Anthropic 协议的
`max_tokens`、Responses 的 `max_output_tokens` 都是必填字段，在 `adapters/` 出口
和 `lanes/` 各删一次，双保险。

模型名同理：反代不碰它，`build_request()` 直接拿 `body.model` 填 `X-Request-Model`。
只有留空时才补默认档。

---

## 目录结构

```
AutoClaw2api/
├── server.py            调度层：装配 Flask、鉴权、上游重试、日志/落盘、SSE 产出
├── lanes/autoclaw.py    通道层：凭据解密、请求整形（三条硬约束在此）、SSE 解析
├── routes/              协议路由：chat.py / anthropic.py / responses.py
├── adapters/            协议翻译：anthropic.py / responses.py ⇄ Chat
├── panel.html           管理面板（门禁自检 / 可调性探测）
└── regress.py           三协议 × {真实id / 空名 / 未知名} 回归脚本
```

分层原则：**routes/ 只管协议形状，lanes/ 只管怎么跟上游说话，server/ 只管调度**。
新增一个协议 = 加一个 `routes/<name>.py` + 一个 `adapters/<name>.py`，
`server.py` 不改逻辑，只多一行 `register()`。

---

## 管理面板

`http://127.0.0.1:8378/admin`

- **门禁自检** —— 把一个「全字段污染的请求」喂给 `build_request`，断言出口满足
  三条硬约束（无 token 上限字段、含 banner、身份句已中和、`X-Request-Model` 已填
  且在目录内、未发 `X-Harness-Type`、sign 用非空 appKey）。**不出网**，随时可跑。
  它证明的是「反代会怎么做」，不等于「上游会接受」。
- **探测可调性** —— 逐模型发极小请求实测。`pay-view` 是账号配额的分钟级波动，
  所以「目录里有」不等于「现在能调」，只有实调能区分。结果会缓存并进 overview。
- **积分任务与流水** —— 列任务、逐项领取、翻页看流水。见下面「账号面：积分」。
- **账号授权登录**（OAuth）—— 仅在 `AUTOCLAW_CRED_SOURCE=oauth` 时出现。
- **刷新状态 / 重新解密凭据** —— 后者仅在桌面端刚刷新登录态、而 mtime 检查
  恰好漏掉时用；正常情况下凭据按 mtime 热重载，自动跟随。

| 接口 | 用途 |
|---|---|
| `GET /health` | 凭据状态、token 剩余有效期、当前模型目录 |
| `GET /admin/api/overview` | 凭据 + 模型 + probe 缓存 + 当前生效的约束 + 积分 |
| `POST /admin/api/self-check` | 门禁自检（不出网） |
| `POST /admin/api/probe-models` | 逐模型实测可调性 |
| `POST /admin/api/refresh-token` | 强制重新解密凭据 |
| `GET /admin/api/credits` | 积分余额（含即将过期的积分） |
| `GET /admin/api/tasks` | 任务列表，每项带 `claimable` |
| `POST /admin/api/claim` | 领指定任务的积分，body `{"task_id":"..."}` |
| `GET /admin/api/ledgers` | 积分流水，`?page=1&page_size=20` |
| `POST /admin/api/oauth/begin` | OAuth 模式：取授权链接 |
| `POST /admin/api/oauth/status` | OAuth 模式：轮询授权结果 |
| `POST /admin/api/oauth/cancel` | OAuth 模式：取消本次授权 |

> **OAuth 模式当前未打通**：`oauth-url` 在账号面返回业务码 `631000`
> （「抱歉, 登录失败,请重新尝试」），拿不到授权链接，浏览器那一步无从开始。
> 请求形状已与桌面端 2.1.0 `main.cjs` 逐字段对齐——body（`navigate_uri` +
> `source_id` + `device_id`）、managed 头全集、`X-Auth-Sign = md5(appId & ts & appKey)`、
> 秒级时间戳、loopback 端口与路径、UA——并用 Node 原生 fetch（与 Electron 客户端同栈）
> 复刻以排除 TLS 指纹；同一套签名头的 `oauth-captcha-config` 返回 `code:0`，
> 说明身份与签名无误，拒绝只发生在两个 `*-oauth-*` 业务端点本身。
> `captcha-config` 返回 `region: "ga"` 是唯一异常信号：服务端可能把出口 IP 判成了
> 非国内区域。已排除的假设见 `docs/autoclaw-10`。**桌面端凭据解密（默认 `desktop`
> 模式）不受影响，是当前可用的凭据来源。**

正常情况下**不需要**手动刷新：凭据按 mtime 热重载，桌面端刷新登录态后本服务
自动跟随（TTL 5 分钟 + mtime 检查，成本是一次 stat）。

---

## 账号面：积分

账号面的端点挂在**裸域名** `AUTOCLAW_ACCOUNT_BASE`
（默认 `https://autoglm-acceleration-api.zhipuai.cn`），**不是** `UPSTREAM_BASE`——
后者自带 `/autoclaw-proxy/proxy/autoclaw` 路径，而任务端点是该路径的**兄弟**：
`/autoclaw-proxy/proxy/autoclaw-task-list`，中间没有 `autoclaw` 这一段。
把 `UPSTREAM_BASE` 当 base 传进去会 404（实测）。

### 两个签到接口，只有一个真的给分

这一点反直觉，也是本模块唯一真正踩过的坑：

| 接口 | 实测结果 |
|---|---|
| `POST /userapi/v1/user-info` | `code:0` 但 `completed:false`，**一分没给**。这是桌面端 `main.cjs` 的 `DAILY_CREDITS_CLAIM_PATH`，名正言顺却不生效 |
| `POST /autoclaw-proxy/proxy/autoclaw-task-complete` | `{task_id:"daily_signin"}` → **200 分**，余额 22931 → 23131 |

前者**没有代码错误**——`code:0`、HTTP 200、不抛异常，静悄悄地什么也不给。
所以不要因为"文档说这是签到路径"就用它；本服务只实现后者。

`/admin/api/claim` 的四种结果必须分开看，提示语完全不同：

| 字段 | 含义 |
|---|---|
| `completed=true` | 真领到了 |
| `already=true` | 今天已经领过了（服务端按天去重，幂等，**正常**） |
| `rejected=true` | 服务端不认这个客户端触发（见下） |
| 三者皆 false | 服务端没认这次请求（这才是真失败） |

`rejected` 的响应形状也和成功时不一样：成功/已签过是 `{"data":{...}}`，
而 `rejected` 是 **HTTP 400 + 顶层 `{"message":"任务不支持客户端触发"}`，没有 `data`**。
所以解析时不能看到没有 `data` 就报"结构不对"——那会把已知的正常情况当故障。

### 哪些任务客户端领不到

任务列表每项带 `client_triggered`（本服务映射为 `claimable`）。当前为 `false` 的：

| 任务 | 分数 | 为什么领不到 |
|---|---|---|
| `invite_friends` | 200 | 得真的去拉人注册 |
| `upgrade_pc_app` | 300 | 得真的安装 PC 客户端 |

这两个**不是 bug**，服务端就是这么设计的。面板据此不渲染「领取」按钮，
而是显示「需另行完成」——不让用户点了再看报错。

可领的两个是 `daily_signin`（200）和 `daily_inspiration_center`（200）。

### 余额与流水

- `GET /admin/api/credits` —— 查不到时返回 `{"available": false, "error": ...}`，
  **绝不返回 0**。「余额查不到」和「余额是 0」对用户是两件事，混成 0 会让人以为号废了。
  面板同样显示「积分查不到」而不是 0。
- `GET /admin/api/ledgers` —— 每条带 `metadata.actual_model` 与
  `metadata.autoclaw_model_mode`，所以它不只是账本，还能回答
  「刚才那次对话花了多少分、走的哪个模型」。时间字段是 **`occurred_at`**
  （不是 `created_at`——响应里根本没有后者的键，取错了时间列会全空）。

---

## 环境变量

复制 `AutoClaw2api/.env.example` 为 `AutoClaw2api/.env` 修改。常用项：

| 变量 | 默认 | 说明 |
|---|---|---|
| `HOST` / `PORT` | `127.0.0.1` / `8378` | 监听地址。**别改 0.0.0.0**——凭据绑死本机身份 |
| `API_KEY` | 空 | 本地 API Key。留空 = 不校验（配合回环监听即「本机随便调」） |
| `AUTOCLAW_BASE` | `%APPDATA%/AutoClaw-official` | 用户数据根 |
| `AUTOCLAW_ACCOUNT` | 自动 | 多账号时指定账号目录 |
| `AUTOCLAW_DEFAULT_MODEL` | `zai_auto` | 客户端没填 `model` 时补的档位 |
| `AUTOCLAW_CRED_SOURCE` | `desktop` | `desktop` = 解密桌面端密文；`oauth` = 自己走 OAuth 换 token（存 `.cred/`，DPAPI 加密）。选 `oauth` 时桌面端那份仍只读、不碰 |
| `AUTOCLAW_ACCOUNT_BASE` | `https://autoglm-acceleration-api.zhipuai.cn` | 账号面积分/任务端点的 **base（裸域名）**。与 `AUTOCLAW_UPSTREAM_BASE` 不是一回事，见下面「账号面」 |
| `DUMP` | `1` | 是否落盘请求/响应原文到 `dumps/` |

**凭据不落在任何配置文件里**——一律从桌面端密文现场解密，`dumps/` 也绝不记录
header（那里有明文 JWT）。

---

## 排障

| 现象 | 原因 | 处理 |
|---|---|---|
| `406` 空响应 | 三条硬约束之一被触发 | 看 `dumps/` 里的 `*.resp.txt`；通常是 `max_tokens` 又回来了 |
| `400` `非法模型` | 上游不认这个 id | 从 `GET /v1/models` 里挑；该错误来自上游，反代不改写模型名 |
| `401` | token 过期或桌面端已退出登录 | 重新登录桌面端；本服务会自动重新解密重试一次 |
| `403` `pay-view` (`code=810001`) | **账号配额状态**，与请求内容无关，分钟级波动 | 本服务透传且不重试；等待或充值，不是反代的问题 |
| `SSLError: UNEXPECTED_EOF_WHILE_READING` | 该 host 间歇性 TLS 断连 | `_open_upstream()` 已带退避重试（2s/4s/8s，共 4 次） |

`request.log`（滚动 2 MB × 4）记录每个请求的 TTFT、字节数、身份句中和与 banner 注入情况。

---

## 与其余六个 2api 的关系

`E:\VSCodeSpace\Agent\` 下的 WorkBuddy2api 等六个服务用同一套 `routes/` + `lanes/` +
`adapters/` 分层与 `register(app, core)` 挂载约定，端口 8378 避开其余六个
（8389/8787/8381/8390/8788/8377）。

差别只在凭据来源：其余几家靠 OAuth / 密码换 token，AutoClaw 直接复用桌面端
已登录的本机凭据，只读不写。
