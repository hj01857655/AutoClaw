# AutoClaw2-01 目标概况

状态：A（全部实测于 2026-09-29，安装目录 `%LOCALAPPDATA%\Programs\AutoClaw2`）

## 产品身份

| 项 | 值 |
|---|---|
| 产品名 | AutoClaw（内部代号 zwork） |
| package.json name/version | `zwork` / `2.0.1` |
| description | "AutoClaw desktop AI product" |
| author | Zhipu AI <support@zhipuai.cn> |
| license | Apache-2.0 |
| 发行渠道 | `dist/channel.json` → `{"channel":"zai","stage":"production"}` |
| Electron | 42.6.1（`version` 文件） |
| 主依赖 | `@earendil-works/pi-coding-agent` 0.83.0（完整 npm 包随附） |
| git 仓库线索 | `https://gitlab.glm.ai/autoclaw/zwork` |

## 安装布局

```
AutoClaw2\                     Electron 壳（AutoClaw2.exe 233MB 单体）
├─ resources\
│  ├─ default_app.asar         Electron 默认（未使用）
│  ├─ elevate.exe              squirrel 提权
│  └─ app\                     ← 应用本体（未 asar，直接目录）
│     ├─ package.json
│     ├─ node_modules\         完整依赖（pi-coding-agent、@anthropic-ai、@aws-sdk、openai 等）
│     └─ dist\
│        ├─ channel.json
│        ├─ main.cjs                     36,804,213 B  esbuild bundle，保留标识符与 // src/ 路径注释
│        ├─ zwork-main-bootstrap.cjs     553,400 B     真实 main 入口（package.json main 字段指向它）
│        ├─ zwork-bootstrap-preload.cjs  1,290,872 B   preload bundle
│        ├─ installer\                   payload-manifest.json / payload-verify.cjs / workspace-collision-probe.cjs
│        ├─ renderer\                    zwork.html + assets\（代码分块）
│        └─ resources\                   agent-skills / agent-skills-core / agent-skills-craft / lark-cli /
│                                        apmplus / autonomous / bin / office-plugin-templates /
│                                        skills-preinstall / vertical-packs / zwork-skill-runtime
└─ vendor\                     内嵌完整运行时：bun / dotnet / node / python / ripgrep
```

要点：
- **未 asar**，`resources/app` 是裸目录，无需解包步骤。
- `main.cjs` 是 esbuild 产物但**未压缩混淆**：模块级 `// src/...` 与 `// ../../node_modules/...` 路径注释完整保留，函数名/局部变量名大体保留（esbuild 压缩级别低或有意保留）。
- 主进程侧源码树在 bundle 注释里可见：`src/main/**`（498 文件）、`src/shared/**`（66 文件）+ `packages/zwork-agent/src/**`（863 文件，来自 monorepo `packages/`）。
- 全部源码路径清单已提取至 `work/main-src-paths.txt`（3691 条）。

## 入口链

1. `package.json` → `main: dist/zwork-main-bootstrap.cjs`
2. bootstrap 负责开发版版本注入、profile 解析、日志，然后 require main.cjs
3. preload 侧独立 bundle `zwork-bootstrap-preload.cjs`

bootstrap 已见的模块（`// src/` 注释）：
- `src/main/app/zwork-main-bootstrap.ts`
- `src/main/app/zwork-development-version.ts`（`--zwork-performance` 调试旗标，5–300 秒窗口）
- `src/main/app/zwork-profile-resolution.ts`
- `src/main/log/zwork-main-log.ts`
- `src/shared/local-performance-contract.ts`
- `src/shared/zwork-build-environment.ts`

## 未决问题

- zwork-main-bootstrap.cjs 553KB 的其余内容（除了头部 80 行外还没细读）——它 bundle 了 zod 等依赖，可能有独立子模块。
- `AutoClaw2.exe` 233MB 远大于常规 Electron 壳，疑似嵌入了额外 payload（installer/payload-manifest.json 883KB 值得对读）。
