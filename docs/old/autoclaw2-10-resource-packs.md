# AutoClaw2-10 资源包体系：vertical-packs、skills、skill-runtime

状态：A（实测文件与 manifest）。

## Vertical Packs（垂直能力包，`dist/resources/vertical-packs/`）

| packId | 标题 | 版本 | domainRules capabilityId |
|---|---|---|---|
| code | 代码项目 | 2 | autocoder.core-rules |
| design | 设计项目 | 5 | autodesign.core-rules |
| financial-analysis | 金融分析 | 3 | autofinance.financial-analysis-core-rules |
| financial-reporting | 财务报告 | 3 | autofinance.financial-reporting-core-rules |
| legal | 法律垂直包 | 19 | autolegal.core-rules |

**包格式** `zwork-vertical-pack-v1`：`{format, packId, packVersion, requires:{runtimeApi:1}, title, description, projectInitializationKind, domainRules[{source:"file", capabilityId, revision, name, description, path}], mcpServers[], files[{path, sha256, bytes}], governedTools[]}`。
- 规则文件带 sha256 完整性清单 —— 主进程可校验后才注入 agent 上下文。
- code 包声明「初始化不写入任何文件」，对应 docs/07 的 work_project_initialization_journal。
- **闭环验证**：本工作区系统提示里的「项目域规则——代码开发工作规则」与 `code/rules/code-core-rules.md` 同源（autocoder.core-rules rev1），即本会话运行在 AutoClaw2 的 agent 内核上、被注入该包规则。

## 预装技能（`dist/resources/skills-preinstall/`，~90 个）

金融/法律/办公/设计全谱（3-statement-model、dcf-model、tianyancha、jianwei、legal-*、contract-*、xlsx/pptx/docx、deep-research、delivery-artifact…）+ manifest.json（32KB）+ PROVENANCE.json（550B）。与 agent 会话内可见的 available_skills 完全对应——桌面端把这里的内容暴露给内核。

## 技能核心包

- `agent-skills/`：lark-doc / lark-markdown / lark-shared / lark-wiki（连接器技能）
- `agent-skills-core/`：autoclaw-migration（19KB SKILL.md + references/migration-options.md 12.7KB）、zwork-orchestration（14.6KB）
- `agent-skills-craft/`：diagram-design（40KB SKILL.md + assets 示例 + scripts，manifest 40KB）

## 技能运行时与本地工具

- `zwork-skill-runtime/zwork_skill_runtime.py`（16.6KB）：Python 技能执行运行时（配合 vendor/python）。
- `bin/windows-helper/`：Windows 辅助进程目录。
- `lark-cli/lark-cli.exe`（44.5MB 单体）：飞书 CLI（lark 技能族后端）。
- `office-plugin-templates/`：Word/WPS 插件模板（对端 autoclaw.zhipuai.cn/office-plugin-v2/）。
- `apmplus/`：parfait.node 原生 APM addon（docs/04）。
- `autonomous/execution-worker.cjs`（9.3KB）：自治任务执行 worker。

## 模型供应商预设（src/shared/model-provider-presets.ts）

- 服务端下发（`model_provider_presets.load`），schema：providers[] { id, name, vendor, icon?, tag?(运营角标，原样展示不翻译), accessType: `direct|codingPlan`, enabled, endpoints[{protocol: openai|anthropic|google…, baseUrl}], supportedModels[{id, name, tag?, maxInputTokens, maxOutputTokens}] }。
- 快速配置文案 `quick_model_setup_copy.load`（badge + ruleTip）。
- 促销配置 `promotion_config.load`（badge type: new/discount/beta）。
- 失败枚举：access_rejected / network / service_unavailable / invalid_response。
- 即模型目录是**运营可热更的**，客户端只持有 schema 与内置兜底。

## 技能装载链（三棵根目录树，实测 main.cjs + 本机文件）

1. **产品技能根** `<runtimeData>/zwork-agent/skills/`（本机 147 个目录）——skill_store 商店安装、skills-preinstall 预装部署（PreinstalledSkillDeployer，ledger `skills-preinstall.json` + 墓碑 `skills-tombstones.json`）、BrowserAgentSkillUpdater 同步都落这里；禁用移至 `skills-disabled/`。
2. **共享技能根** `~\.agents\skills`（env `ZWORK_HARNESS_SHARED_SKILLS_DIRECTORY` 可覆盖）——`npx skills add -g` 的落点，BrowserAgentSkillUpdater 会同步它。
3. **运行时快照** `pi-resources/revisions/<sha>/skills/`（238 目录，只读）——官方 bundle（.zwork-bundles: official-agent-craft-suite@1 / official-core-orchestration@40 / official-lark-governed-docs@5）按修订冻结，会话实际加载的 available_skills 来自这里。

实测时间线：2026-09-29 10:38 左右 `npx skills add zhaoxuya520/reverse-skill -g -y` 装入 `~\.agents\skills` 后，BrowserAgentSkillUpdater 把它同步进产品技能根（reverse-engineering / js-reverse / ida-reverse / dotnet-reverse / protocol-reverse / reverse-skill-router / ghidra-reverse / binary-diff / patch-diff-exploit / pwn-chain 等全部 Test-Path=True）。**即我此前只验证了第 2 棵树、误报了“我没装上”；实际同步链路已把它送进第 1 棵树，新会话即可见。**

## 未决问题

- zwork_skill_runtime.py 的执行边界（socket? stdin? 文件协议?）。
- windows-helper 的具体功能（提权？托盘？）。
- autonomous/execution-worker.cjs 与 scheduled_* 表的执行链。
