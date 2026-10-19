# AutoClaw 逆向分析（六）网络面

2026-10-19 · 基于 **2.1.0** 实测（旧文档基于 2.0.1，本篇全部重新提取）

## 一、域名全景

全包提取 `https://` 主机名去重后 **150 个**。按归属分类：

### 1.1 智谱自有

```
autoglm-acceleration-api.zhipuai.cn   生产（模型 + 账号 + 业务网关）
autoglm-api.zhipuai.cn                 法律云 / 备用 API
autoglm-pre-api.zhipuai.cn             预发
autoglm-inner-3.zhipuai.cn             内网测试
autoglm-test-2.zhipuai.cn              local/test
autoclaw.zhipuai.cn                    官网 + Office 插件分发
autoclaw-remote-web.zhipuai.cn         远程控制 Web（生产）
autoclaw-test-remote-web.zhipuai.cn    远程控制 Web（测试）
zwork-test.zhipuai.cn                  测试环境 Office 插件
autoglm-api.autoglm.ai                 海外生产
autoglm-pre-api.autoglm.ai             海外预发
autoglm-test-api.autoglm.ai            海外测试
autoglm.aminer.cn                      更新源（CN）+ APK 分发
autoglm-public-oss.z.ai                更新源（海外 OSS）
api.z.ai / zcode.z.ai / gitlab.glm.ai  ZCode 体系
```

### 1.2 IM 平台

| 域名 | 频次 | 对应 provider |
|---|---|---|
| `open.feishu.cn` | **5431** | feishu（代码量最大） |
| `open.larksuite.com` / `www.feishu.cn` / `bytedance.feishu.cn` | 20 / 10 / 20 | 飞书系 |
| `core.telegram.org` / `t.me` / `grammy.dev` | 388 / 4 / 11 | telegram（用 grammY 框架） |
| `discord.com` / `discord.dev` | 118 / 49 | discord |
| `oapi.dingtalk.com` / `api.dingtalk.com` | 3 / 2 | dingtalk |
| `www.whatsapp.com` | 2 | whatsapp |
| `ilinkai.weixin.qq.com` | 2 | 微信客服 / 企微 |

### 1.3 模型供应商（第三方）

```
openrouter.ai                    310   聚合
ai-gateway.vercel.sh             193   Vercel AI Gateway
bedrock-runtime.us-east-1.amazonaws.com   104   AWS Bedrock
router.huggingface.co             51    HF
api.openai.com / chatgpt.com      43 / 7
gateway.ai.cloudflare.com         43
api.mistral.ai                    30
integrate.api.nvidia.com          30    NVIDIA NIM
api.individual.githubcopilot.com  29
generativelanguage.googleapis.com 24    Gemini
api.anthropic.com / platform.claude.com   19 / 3
open.bigmodel.cn                  17    智谱开放平台
api.together.ai / api.fireworks.ai 17 / 16
api.moonshot.cn / api.moonshot.ai 15 / 10
token-plan.{cn-beijing,ap-southeast-1,ams,cn,sgp}.maas.aliyuncs.com   阿里百炼
api.xiaomimimo.com / token-plan-*.xiaomimimo.com  小米 MiMo
api.x.ai / console.x.ai           7 / 2   Grok
api.groq.com                      7
api.deepseek.com                  6
api.minimaxi.com / api.minimax.io 7 / 3
api.cerebras.ai                   3
api.ant-ling.com                  3      蚂蚁 Ling
platform.kimi.com                 2
```

### 1.4 连接器 / MCP / 数据源

```
apim-gateway.pkulaw.com           9    北大法宝（法规检索）
mcp.pkulaw.com                    1
mcp.notion.com                    2
mcp.vercel.com / mcp.cloudflare.com   1 / 1
mcp.lexiang-app.com               1
ai.tianyancha.com / mcp.tianyancha.com   6 / 1   天眼查
agent.qcc.com                     6    企查查
www.jianweidata.com               2    见微数据
www.fyopen.com                    1    法院开放平台
api.cjbdi.com                     1    裁判文书网
```

### 1.5 遥测 / 基础设施

```
apmplus.volces.com                    2    火山 APM（CN）
apmplus.ap-southeast-1.volces.com    2    火山 APM（海外）
gator.volces.com / tab.volces.com    2 / 2  火山埋点
raw.githubusercontent.com            2
registry.npmmirror.com               2
```

## 二、服务网关矩阵

`RUNTIME_CONFIG_SERVICE_IDS` 七个服务，全部走同一 origin（除 `legal_cloud_gateway` 与 `device_relay`）：

| 服务 | 协议 | origin |
|---|---|---|
| `identity_gateway` | https | `HTTP_ORIGIN_BY_ENVIRONMENT` |
| `channel_hub` | https | 同上 |
| `schedule_gateway` | https | 同上 |
| `device_relay` | **wss** | `origin.replace(/^https:/, "wss:")` |
| `managed_model_gateway` | https | 同上 |
| `connected_source_gateway` | https | 同上 |
| `legal_cloud_gateway` | https | `LEGAL_CLOUD_ORIGIN_BY_ENVIRONMENT` |

## 三、更新链路

`src/main/updates/update-source.ts`：

```js
var ZWORK_UPDATE_ROOTS = {
  cn:      "https://autoglm.aminer.cn/zwork/updates/",
  oversea: "https://autoglm-public-oss.z.ai/zwork/updates/"
};
```

### 3.1 六重校验（这段写得相当严）

```js
function updateFeedUrl(identity, policy) {
  // ① 六个字段逐一校验
  if (!isZworkChannelName(identity.channel)
      || valid(policy.version) !== policy.version
      || !["test","pre","production"].includes(identity.environment)
      || !["cn","oversea"].includes(identity.region)
      || !isUpdatePlatform(identity.platform)) throw new Error("invalid-source");

  const root = new URL(policy.baseUrl ?? ZWORK_UPDATE_ROOTS[identity.region]);
  const trusted = new URL(ZWORK_UPDATE_ROOTS[identity.region]);

  // ② origin 必须与信任根完全一致（防子域劫持/重定向）
  // ③ 路径必须以 /zwork/ 开头
  // ④ 禁止 username/password
  // ⑤ 禁止 query / fragment
  // ⑥ 禁止路径里出现 % 或反斜杠（防编码绕过）
  if (root.origin !== trusted.origin
      || !root.pathname.startsWith("/zwork/")
      || root.username || root.password || root.search || root.hash
      || /%|\\/.test(root.pathname)) throw new Error("invalid-source");

  return new URL(`${identity.enterprise ? "enterprise/" : ""}${identity.environment}/${identity.channel}/${identity.platform}/`, root).href;
}
```

**这是本次审查里做得最好的安全校验之一**：`origin` 严格相等 + 路径白名单 + 禁 `%`（防 `%2e%2e` 之类）+ 禁反斜杠（Windows 路径语义）。即使 `policy.baseUrl` 被篡改也出不去。

### 3.2 更新源路径结构

```
{root}/[enterprise/]{environment}/{channel}/{platform}/
```

`channel` 形如 `latest-{semver}`（`updateChannelName`，semver 合法性同样校验）。

### 3.3 安装器

`dist/installer/`：

```
payload-manifest.json        396 KB
payload-verify.cjs           3 KB
workspace-collision-probe.cjs 555 KB
```

## 四、连接器体系

`/userapi/v1/connected-apps/` 端点：

```
oauth-url        oauth-login      oauth-refresh
status           disconnect
github/mcp       figma/api
```

对应命令域：

```
connected_source.create / delete / set_enabled / test
integrations.start_oauth / cancel_oauth / disconnect / status
domestic_connector.connect / disconnect / cancel_oauth / retry_services / set_session_selection
```

**三套并存**说明有历史分层：`integrations`（海外/OAuth）、`domestic_connector`（国内）、`connected_source`（通用受管源）。

## 五、IM 通道

7 个子目录，每个 provider 一套完整实现：

```
src/main/im/feishu/     ← 代码量最大（feishu 关键字 5787 次）
src/main/im/telegram/   ← grammY 框架
src/main/im/discord/
src/main/im/dingtalk/
src/main/im/wechat/
src/main/im/wecom/
src/main/im/whatsapp/
```

飞书模块细分（部分）：

```
zwork-feishu-capability-auditor.ts     zwork-feishu-config-store.ts
zwork-feishu-connection-service.ts     zwork-feishu-connection-set.ts
zwork-feishu-connection-view.ts        zwork-feishu-cosmetic-state.ts
zwork-feishu-delivery.ts               ...
```

钉钉有独立的扫码链路：

```
zwork-dingtalk-scan-create.ts    zwork-dingtalk-scan-ipc.ts
zwork-dingtalk-owner-pairing.ts  zwork-dingtalk-physical-ingress.ts
zwork-dingtalk-transport-protocol.ts
```

企业微信同样有扫码通道（`zwork:desktop:wecom-scan-create` / `-cancel`）。

## 六、模型供应商预设

`src/shared/model-provider-presets.ts` 内嵌全部预设，配合 `zcode-provider-templates.ts` 使用。

本地 `model_connections` 实测的 4 条记录：

```
provider = zwork-managed   model = zai_auto        protocol = openai-completions  active=1
provider = zwork-managed   model = zai_auto-fast   protocol = openai-completions  active=0
（另两条未展开）
```

## 七、出站审计（`egress.decision`）

`work_events` 里 **168 条 `egress.decision`**（占全部事件 26%），说明每次出站都有决策记录。这是分析"什么数据出去了"的关键入口——payload 结构值得进一步挖（见未决问题）。

## 八、与 2.0.1 的差异

旧文档记录「环境矩阵 + 产品后端域 + 产品自有 REST API」，未列：
- 150 个域名全景与频次
- 更新源六重校验细节
- IM 七通道各自的实现目录
- 三套连接器命令域并存

## 九、未决问题

- `egress.decision` 的 payload 字段（决定能审计到什么粒度）
- `device_relay` 的 wss 协议（配对、中继、授权）
- 飞书 5431 次引用的分布（是否含大量 schema 定义而非真实调用）
- `integrations` / `domestic_connector` / `connected_source` 三套命令的实际分工
