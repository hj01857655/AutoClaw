# AutoClaw2-03 网络面

状态：A（域名与端点实测自 main.cjs 字符串字面量）。过滤时排除了 pi-ai 通用供应商列表（anthropic/openai/gemini/bedrock 等），聚焦产品自有后端。

## 环境矩阵（bundled-runtime-config.ts，实测）

```js
HTTP_ORIGIN_BY_ENVIRONMENT = {
  local:          "https://autoglm-inner-3.zhipuai.cn",
  test:           "https://autoglm-inner-3.zhipuai.cn",   // 注释里 local/test 曾指向 autoglm-test-2
  pre:            "https://autoglm-pre-api.zhipuai.cn",
  release:        "https://autoglm-acceleration-api.zhipuai.cn",
  overseas_test:  "https://autoglm-test-api.autoglm.ai",
  overseas_pre:   "https://autoglm-pre-api.autoglm.ai",
  overseas_release:"https://autoglm-api.autoglm.ai"
}
LEGAL_CLOUD_ORIGIN_BY_ENVIRONMENT = { 同结构，release: "https://autoglm-api.zhipuai.cn" }
```

- `release` 环境 agent 通道走 **autoglm-acceleration-api.zhipuai.cn**（加速/直连线路），法律云走 autoglm-api.zhipuai.cn。
- `device_relay` 服务用 `wss:`（同域升级），其余服务 https。RUNTIME_CONFIG_SERVICE_IDS 至少含 `device_relay`、`legal_cloud_gateway`（全集待提取）。
- 配置快照 schema：`{ schema_version:1, revision:"rc_bundled_<env>_2", environment, audience:"desktop-agent", profile:"bundled", issued_at, refresh_after, minimum_client_version, connections }` —— 运行时连接配置支持服务端下发刷新（`refresh_after`），bundled 快照只是兜底。

## 产品后端域（按用途）

| 用途 | 域 |
|---|---|
| 主 API（release 国内） | autoglm-acceleration-api.zhipuai.cn / autoglm-api.zhipuai.cn |
| 更新分发 | autoglm-public-oss.z.ai/zwork/updates/ 、autoglm.aminer.cn/zwork/updates/ |
| office 插件 | autoclaw.zhipuai.cn/office-plugin-v2/{word,wps}-plugin/（test: zwork-test.zhipuai.cn） |
| 法律连接器 MCP | api.cjbdi.com:8443/354347/mcp_law_service 、open.chineselaw.com/mcp/{law,company,case}/stream 、apim-gateway.pkulaw.com/* 、www.jianweidata.com 、www.fyopen.com |
| 企业数据 MCP | agent.qcc.com/mcp/{company,executive,ipr,operation,risk}/stream 、ai.tianyancha.com/guide |
| 其他工具 MCP | mcp.lexiang-app.com/mcp 、mcp.notion.com/mcp |
| IM | ilinkai.weixin.qq.com（微信 ilink）、web.whatsapp.com / mmg.whatsapp.net、api.telegram.org、discord.com/api/v10/gateway |
| 遥测 APM | apmplus.volces.com（火山引擎 APMPlus）、gator.volces.com、tab.volces.com |
| 验证码 | g.alicdn.com/captcha-frontend/ 、o.alicdn.com/.../AliyunCaptcha.js |
| 开发态本地 | zwork.local（official-integrations 页面）、http://127.0.0.1 |
| 云端机运行时 | nodejs.org/dist 、registry.npmmirror.com/-/binary/{node,python} 、www.python.org/ftp/python 、builds.dotnet.microsoft.com（vendor 运行时下载源） |

## 产品自有 REST API（路径字面量实测，release 域）

**账号 /userapi**：agent-send-code、agent-login、agent-refresh、refresh、user-info、user-profile、user/write-off、set-config、content-resource、notifications(/read)、web-event/report、app/feedback、ali-cloud-observation-config；海外 overseasv1/{zai,google}-oauth-{url,login}、oauth-captcha-config、user-write-off

**能力服务 /agentdr/v1/assistant/**：claw-input-check、claw-output-check（输入输出安全审查）、conf-get、generate-avatar、generate-session-title、home-fetch、upload-expr、skills/{center,manage-type-list,web-search}、finance-search/mcp、sec-search/mcp、tianyancha/mcp、tianyan-proxy、jianwei-proxy、legal-proxy-yuandian、ifind/{stock,index,fund,bond,global-stock}/mcp、wind/{stock,index,fund,bond,economic,financial-docs}/mcp

**权益 /agent-assetmgr/api/v1|v2**：wallets、wallet-instances、ledgers_std、autoglm-codes/redeem（兑换码）、fission-v2/{bind,online-activity-info}（裂变活动）

**订阅 /agentpay/v1/assistant**：subscribe-info、oversea-subscribe-info

**技能**：/skill-service/invoke、/skills、/skills/{generate-image-seedream,happy-horse-create,image-recognition,ocr,search-image}、/uploads、/files

**模型供应商（pi-ai 内置目录，节选）**

open.bigmodel.cn/api/{paas,coding,anthropic}、api.z.ai、zcode.z.ai/api/v1/{zcode-plan,off-peak}/anthropic、deepseek、moonshot、minimax、kimi、dashscope、xiaomimimo、openrouter、opencode.ai/zen、cerebras、groq、x.ai、mistral、perplexity、together、fireworks、nvidia、huggingface router、volces token-plan、chatgpt.com/backend-api/codex、copilot individual、bedrock multi-region、vertex aiplatform。

## 鉴权线索

- JWT 解析逻辑 `parseJwtExpiryMs`（main.cjs ~536577）：接受 `Bearer ` 前缀，取 payload.exp —— **主进程持有并自解析 JWT**。
- 失败分类重试：`failureClass ∈ {soft, hard_candidate, network}` + 各自退避表（SOFT_BACKOFF_MS / NETWORK_BACKOFF_MS / HARD_CANDIDATE_RETRY_MS，值待抄）。
- 渲染层 auth 面：`zwork:auth:invoke` 判别 union（含 `oauth.start`），会话通道 `zwork:auth:session`。
- Sentry DSN 出现在字符串里但被 redaction 处理（`https://[REDACTED]@`）。

## 未决问题

- RUNTIME_CONFIG_SERVICE_IDS 全集已确认（7 个，见 docs/04 运行时拓扑）：identity_gateway、channel_hub、schedule_gateway、device_relay、managed_model_gateway、connected_source_gateway、legal_cloud_gateway。
- 遥测事件结构（APMPlus 上报内容）。
- `zwork:auth:session` 的推送内容与刷新流程。
