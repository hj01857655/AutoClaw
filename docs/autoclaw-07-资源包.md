# AutoClaw 逆向分析（七）资源包体系

2026-10-19 · 基于 **2.1.0** 实测文件系统（旧文档基于 2.0.1，本篇全部重新盘点）

## 一、资源根目录

`resources/app/dist/resources/`：

```
vertical-packs/          6 个垂直能力包
skills-preinstall/       104 个预装技能（manifest 声明）+ 105 个目录
agent-skills/            6 项：lark-doc / lark-markdown / lark-shared / lark-wiki
agent-skills-core/       4 项：autoclaw-migration / beida-fabao / zwork-orchestration
agent-skills-craft/      2 项：diagram-design
autonomous/              沙箱执行层（见 04）
office-plugin-templates/ Office 加载项模板
bin/
lark-cli/
apmplus/                 原生 APM addon
im-pdf-worker.cjs
trayTemplate.png
```

## 二、Vertical Packs（垂直能力包）

### 2.1 清单

| packId | title | version | files | mcpServers | governedTools | domainRules |
|---|---|---|---|---|---|---|
| `code` | 代码项目 | v3 | 1 | 0 | 0 | 1 |
| `design` | 设计项目 | v6 | 29 | 0 | 0 | 1 |
| `financial-analysis` | 金融分析 | v4 | 10 | 0 | 2 | 1 |
| `financial-reporting` | 财务报告 | v4 | 4 | 0 | 2 | 1 |
| `legal` | 法律垂直包 | **v24** | 3 | 0 | **3** | 1 |
| `science` | 科研项目 | v2 | 26 | **9** | 0 | 1 |

`legal` 版本号 v24 远高于其他（v2–v6），说明法律包迭代最频繁。

### 2.2 清单格式（`zwork-pack.json`）

```json
{
  "format": "zwork-vertical-pack-v1",
  "packId": "code",
  "packVersion": 3,
  "requires": { "runtimeApi": 1 },
  "title": "代码项目",
  "description": "软件工程场景官方代码垂直包：领域工作规则（autocoder.core-rules）…",
  "indexSummary": "software engineering rules for working in real codebases",
  "projectInitializationKind": "code",
  "domainRules": [{
    "source": "file",
    "capabilityId": "autocoder.core-rules",
    "revision": 2,
    "name": "代码项目工作规则",
    "path": "skills/code-core-rules/SKILL.md"
  }],
  "files": [{
    "path": "skills/code-core-rules/SKILL.md",
    "sha256": "b711da8fe97fd2dc…",
    "bytes": 5715
  }],
  "mcpServers": [],
  "governedTools": []
}
```

**关键字段**：
- `requires.runtimeApi: 1` —— 运行时 API 版本协商，防旧内核跑新包
- `files[].sha256` + `bytes` —— 每个文件独立校验
- `domainRules` —— 领域工作规则，挂在 system prompt
- `governedTools` —— 声明该包可用的受管工具及版本（如 legal 包：`legal.case@5`、`legal.statute@5`、`legal.web_search_law@6`）

### 2.3 science 包的 MCP 定义

`science` 是唯一带 MCP 服务的包，9 个，全部 `transport: stdio` + `deferred: true`：

```json
{
  "transport": "stdio",
  "name": "science_literature",
  "command": "mcp/science-tools.cjs",
  "args": ["--group", "science_literature"],
  "deferred": true,
  "tools": [{
    "name": "arxiv_get_papers",
    "description": "Fetch metadata (abstracts included) for a batch of arXiv papers by ID — at most 100 papers served by one politely paced request…",
    "inputSchema": { "type": "object", "$schema": "https://json-schema.org/draft/2020-12/schema", … }
  }]
}
```

组名（从 args 推断）：`science_literature`、`science_genetics`、`science_clinical`、`science_chemistry` 等 9 组。

**`deferred: true` 值得注意**：工具 schema **延迟加载**，不预先塞进上下文——正是社区长期吐槽的"大型 MCP server 撑爆上下文"的解法。

## 三、预装技能（104 个）

### 3.1 manifest 结构

`skills-preinstall/manifest.json`：

```json
{
  "schemaVersion": 1,
  "appVersion": "2.1.0",
  "channel": { "environment": "release", "oversea": false },
  "targetPlatform": "win",
  "fetchedAt": 1791729086210,
  "skills": [{
    "skillId": "10862d6a-18c8-4446-9822-2e0956921aa2",
    "name": "xlsx-author",
    "dirName": "xlsx-author",
    "version": "1.0.0",
    "contentSha256": "sha256:022895527957c767c51140a227b7bed64cdfe701c26a632656e6b1aa0c071938",
    "byteLength": 34100,
    "fileCount": 2
  }]
}
```

**规模**：104 项，合计 **11.7 MB**，`contentSha256` 逐项校验。

### 3.2 技能分类（按名推断）

| 类别 | 代表技能 |
|---|---|
| **金融分析** | `dcf-model` `lbo-model` `comps-analysis` `three-statement-model` `accretion-dilution` `sources-uses` `scenario-analysis` `rolling-forecast` |
| **财报** | `earnings-analysis` `earnings-preview` `earnings-flash` `month-end-close-review` `budget-variance` `close-recap` |
| **法律** | `legal-research-drafting` `legal-qa` `legal-docx-generator` `contract-review` `legal-norm-validity-check` `legal-element-extraction` `evidence-evaluation` `case-retrieval` |
| **研究** | `deep-research` `research-report` `sector-overview` `competitive-analysis` `peer-benchmark` `policy-tracker` |
| **文档产出** | `xlsx` `xlsx-author` `pptx` `pptx-author` `docx` `pdf` `pdf-text-extractor` `report-render` |
| **数据源** | `tianyancha` `jianwei` `fatianshi` `feishu-doc` `feishu-chat-history` `wecom-unified` |
| **平台能力** | `autoclaw-billing` `autoclaw-im-cron` `autoglm-asr-translate` `autoglm-file-upload` `autoglm-remove-bg` |
| **元技能** | `skill-creator` `skill-vetter` `find-skills` `memory` `model-update` |

### 3.3 技能内部结构

以 `xlsx-author` 为例：

```
xlsx-author/
├── SKILL.md              主指令（~34 KB）
├── scripts/recalc.py     附带脚本（LibreOffice 公式重算）
└── _store_meta.json      技能元数据
```

`_store_meta.json`：

```json
{
  "skillId": "10862d6a-…",
  "skillKey": "xlsx-author",
  "version": "1.0.0",
  "installedAt": 0,
  "installedAtSource": "bundled",
  "source": "store",
  "origin": "preinstalled",
  "contentSha256": "sha256:02289…",
  "displayName": "财务与研究表格制作",
  "description": "Produce a professional .xlsx workbook…",
  "buildCorrections": {
    "tableSha256": "50c20933…",
    "files": ["SKILL.md"]
  }
}
```

**三层校验**：`manifest.contentSha256`（发布清单）→ `_store_meta.contentSha256`（安装记录）→ `buildCorrections.tableSha256`（构建期修正表）。三者任一不一致即视为被篡改。

### 3.4 复杂技能：report-render（22 文件 / 534 KB）

```
report-render/
├── SKILL.md
├── references/
│   ├── canary.png          ← 视觉回归基准图
│   ├── docx.md
│   ├── pitfalls.md
│   └── visual-review.md
└── scripts/
    ├── check_env.py
    └── fin_report/
        ├── charts.py    cjk.py      doc.py
        ├── fonts.py     inline.py   libreoffice.py
        └── ooxml.py
```

**`cjk.py` + `fonts.py`** 说明中文字体排版有专门处理——这是财务报告类的刚需（CJK 字宽、字形回退），也是很多同类产品做不好的地方。

### 3.5 SKILL.md 的风格

`xlsx-author/SKILL.md` 开头的约束很具体：

```markdown
---
name: xlsx-author
description: Produce a professional .xlsx workbook on disk with openpyxl — …
---

> 先读取 `financial-analysis-core`，按当前目录调用受管数据；文件在本机处理，缺依赖时明确说明。

## Output language
Sheet names, headers, labels, cell comments, the `来源` worksheet and the delivery
message all follow **the user's language of address**, not the template's…
**One language per workbook**: do not mix Chinese sheet names with English column headers.

Provenance tags in cell comments follow the workbook, not the market:
`[披露]` `[测算]` `[预期]` `[推断]` `[媒体]` in a Chinese workbook, the English
aliases `[Reported]` `[Est.]` `[Consensus]` `[Inferred]` `[Media]` in an English one.
Those literal strings only — a paraphrase such as `[已披露]` or `[一致预期]`…
```

**三个设计特征**：
1. **显式的前置技能依赖**（`先读取 financial-analysis-core`）
2. **语言一致性约束**（一个工作簿只用一种语言，含来源标注的字面量白名单）
3. **来源标签体系**（披露/测算/预期/推断/媒体）——这是给金融交付物做可信度分级的做法

## 四、Agent Skills（三套）

| 目录 | 内容 | 用途 |
|---|---|---|
| `agent-skills/` | `lark-doc` `lark-markdown` `lark-shared` `lark-wiki` + manifest | **飞书文档生态**（Lark = 飞书海外名） |
| `agent-skills-core/` | `autoclaw-migration` `beida-fabao` `zwork-orchestration` + manifest | 迁移助手、北大法宝（法规检索）、编排 |
| `agent-skills-craft/` | `diagram-design` + manifest | 制图 |

`autoclaw-migration` 与 IPC 里的 `zwork:autoclaw:import-applied`、`migration_assistant` starter 对应——是对旧版数据的迁移通道。

## 五、模型供应商预设

`src/shared/model-provider-presets.ts` + `zcode-provider-templates.ts`。

本地 `model_connections` 实测的 4 条：

```
zai_auto        openai-completions  active=1   ctx=1048576  max=393216  reasoning=["low","high","max"]
zai_auto-fast   openai-completions  active=0
（另两条未展开）
```

`connection_revision` 形如 `managed-catalog:v1:<sha256>`，说明模型清单由服务端下发并按 hash 版本化，本地只缓存。

## 六、与 2.0.1 的差异

旧文档记录「vertical-packs、预装技能 ~90 个、skill-runtime、模型预设」。2.1.0 实测：

| 项 | 2.0.1（旧文档） | 2.1.0（实测） |
|---|---|---|
| 预装技能 | ~90 个 | **104 个**（manifest 声明），11.7 MB |
| vertical packs | 未列数量 | **6 个**，legal 已到 v24 |
| MCP 声明 | 未提 | science 包 9 个 stdio MCP，全部 deferred |
| agent-skills | 未提三套 | 三套共 12 项 |
| 校验层级 | 未提 | manifest → _store_meta → buildCorrections 三层 |

## 七、观察

1. **`deferred: true` 的 MCP**：工具 schema 延迟加载，是应对上下文膨胀的正确做法，值得记录（对照社区长期报的「MCP schema 撑爆上下文」）
2. **三层 sha256 校验**：技能从发布到安装到构建都有 hash 比对，防篡改做得完整
3. **`legal` 包 v24**：迭代最频繁，配合 `governedTools` 三个受管工具（`legal.case` / `legal.statute` / `legal.web_search_law`），法律是重点垂直
4. **技能内容质量高**：不是空壳提示词，带脚本（`recalc.py`、`fin_report/*.py`）、参考文档、视觉基准图（`canary.png`）
5. **中文本地化到位**：`cjk.py` / `fonts.py` 专门处理中文字体，`displayName` 有中文名

## 八、未决问题

- `vertical-packs` 的 `governedTools.revision` 与 `governed_tool_revisions` 表的对应关系
- `skill-vetter` / `skill-creator` 两个元技能的具体规则（技能审核机制）
- `skills-preinstall` 目录数 105 vs manifest 声明 104 的差异（多出哪个？）
- `agent-skills-*` 与 `skills-preinstall` 的加载优先级与作用域差异
