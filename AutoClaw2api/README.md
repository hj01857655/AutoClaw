# AutoClaw2api

AutoClaw（澳龙）桌面端托管模型的本地 **OpenAI / Anthropic / Responses** 三协议网关。

把 AutoClaw 桌面客户端消费的云端模型包装成标准协议，任何支持自定义 Base URL 的客户端
指向本服务即可调用，无需改客户端代码。

分析依据见 `../docs/autoclaw-10-2x门禁与反代落地.md`。

---

## 快速开始

```powershell
cd E:\VSCodeSpace\Agent\AutoClaw\AutoClaw2api
pip install -r requirements.txt
python server.py
```

启动即打印凭据状态与模型目录。默认监听 **http://127.0.0.1:8378/v1**。

**前置条件**：AutoClaw 桌面端已登录，且以**同一个 Windows 用户身份**运行本服务。
凭据文件由 DPAPI 加密，换用户或换机器都解不开——这是设计使然，不是缺陷。

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

**`model` 填什么都行。** 上游真实 id（`GET /v1/models` 查看目录）直接命中；
填 `claude-sonnet-5` / `gpt-5` 这类客户端固化的名字，或者干脆拼错了，都会落到
`zai_auto`——那是上游的自动路由档，也正是"我要个通用模型"这个意图的正确答案。

想让某个名字固定到特定档位（比如把 `my-fast` 钉在 flash 上），用
`AUTOCLAW_MODEL_ALIASES` 显式声明。日志会记录每次未命中的路由结果，
`zai-glm-5.3-flash` 这类只差分隔符的笔误还会额外提示一次疑似对象。

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
- ③ → `_open_upstream()` 的 `resolve_model()` + `build_request()`
- 额外：`system` 里的外来 harness 身份句（`you are claude code` 等）会被中和，
  否则上游同样回 406

客户端**照常填 `max_tokens`** 即可，删不删由本服务负责。Anthropic 协议的
`max_tokens`、Responses 的 `max_output_tokens` 都是必填字段，在 `adapters/` 出口
和 `lanes/` 各删一次，双保险。

模型名同理：`_open_upstream()` 的 `resolve_model()` 把它换成上游 id，
因为路由读的是 `X-Request-Model` 而不是 `body.model`。未知名落到默认档，
不报错——`zai_auto` 就是上游的自动路由档。

---

## 目录结构

```
server.py            调度层：装配 Flask、鉴权、上游重试、日志/落盘、SSE 产出
lanes/autoclaw.py    通道层：凭据解密、请求整形（三条硬约束在此）、SSE 解析
routes/              协议路由：chat.py / anthropic.py / responses.py
adapters/            协议翻译：anthropic.py / responses.py ⇄ Chat
panel.html           管理面板（门禁自检 / 可调性探测）
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
- **刷新状态 / 重新解密凭据** —— 后者仅在桌面端刚刷新登录态、而 mtime 检查
  恰好漏掉时用；正常情况下凭据按 mtime 热重载，自动跟随。

| 接口 | 用途 |
|---|---|
| `GET /health` | 凭据状态、token 剩余有效期、当前模型目录 |
| `GET /admin/api/overview` | 凭据 + 模型 + probe 缓存 + 当前生效的约束 |
| `POST /admin/api/self-check` | 门禁自检（不出网） |
| `POST /admin/api/probe-models` | 逐模型实测可调性 |
| `POST /admin/api/refresh-token` | 强制重新解密凭据 |

正常情况下**不需要**手动刷新：凭据按 mtime 热重载，桌面端刷新登录态后本服务
自动跟随（TTL 5 分钟 + mtime 检查，成本是一次 stat）。

---

## 环境变量

复制 `.env.example` 为 `.env` 修改。常用项：

| 变量 | 默认 | 说明 |
|---|---|---|
| `HOST` / `PORT` | `127.0.0.1` / `8378` | 监听地址。**别改 0.0.0.0**——凭据绑死本机身份 |
| `API_KEY` | 空 | 本地 API Key。留空 = 不校验（配合回环监听即「本机随便调」） |
| `AUTOCLAW_BASE` | `%APPDATA%/AutoClaw-official` | 用户数据根 |
| `AUTOCLAW_ACCOUNT` | 自动 | 多账号时指定账号目录 |
| `AUTOCLAW_MODEL_ALIASES` | 空 | 模型名映射（JSON） |
| `DUMP` | `1` | 是否落盘请求/响应原文到 `dumps/` |

**凭据不落在任何配置文件里**——一律从桌面端密文现场解密，`dumps/` 也绝不记录
header（那里有明文 JWT）。

---

## 排障

| 现象 | 原因 | 处理 |
|---|---|---|
| `406` 空响应 | 三条硬约束之一被触发 | 看 `dumps/` 里的 `*.resp.txt`；通常是 `max_tokens` 又回来了 |
| `400` `非法模型` | 上游拒绝该 id | 看 `request.log` 里的路由记录；通常是目录刚刷新、拼写偏差 |
| `401` | token 过期或桌面端已退出登录 | 重新登录桌面端；本服务会自动重新解密重试一次 |
| `403` `pay-view` (`code=810001`) | **账号配额状态**，与请求内容无关，分钟级波动 | 本服务透传且不重试；等待或充值，不是反代的问题 |
| `SSLError: UNEXPECTED_EOF_WHILE_READING` | 该 host 间歇性 TLS 断连 | `_open_upstream()` 已带退避重试（2s/4s/8s，共 4 次） |

`request.log`（滚动 2 MB × 4）记录每个请求的 TTFT、字节数、身份句中和与 banner 注入情况。

---

## 与其余六个 2api 的关系

同目录下的 WorkBuddy2api 等六个服务用同一套 `routes/` + `lanes/` + `adapters/`
分层与 `register(app, core)` 挂载约定，端口 8378 避开其余六个（8389/8787/8381/8390/8788/8377）。

差别只在凭据来源：其余几家靠 OAuth / 密码换 token，AutoClaw 是 Chromium
os_crypt 密文（DPAPI 护主密钥 + AES-256-GCM 护业务文件），只读不写。
