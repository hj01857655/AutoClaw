# AutoClaw2 技术语料索引

- 目标：`%LOCALAPPDATA%\Programs\AutoClaw2`（Electron 桌面应用，未 asar 打包）
- 开始时间：2026-09-29
- 工作目录：`E:\VSCodeSpace\ZCode\AutoClaw`（本文档与 `work/` 中间产物所在）

## 文档列表

| 编号 | 文件 | 主题 | 状态 |
|---|---|---|---|
| 01 | [autoclaw2-01-target-overview.md](autoclaw2-01-target-overview.md) | 目标概况：产品身份、安装布局、入口链、构建形态 | A（实测） |
| 02 | [autoclaw2-02-ipc-surface.md](autoclaw2-02-ipc-surface.md) | IPC 面：preload 暴露 API、主进程通道、desktop invoke 分发器 | A（持续补充） |
| 03 | [autoclaw2-03-network-surface.md](autoclaw2-03-network-surface.md) | 网络面：环境矩阵、后端域名、连接器 MCP、IM、遥测、更新源 | A（持续补充） |
| 04 | [autoclaw2-04-agent-runtime.md](autoclaw2-04-agent-runtime.md) | Agent 运行时：pi-coding-agent 集成、扩展权限模型、Parfait、Office 桥 | A |
| 05 | [autoclaw2-05-auth-crypto.md](autoclaw2-05-auth-crypto.md) | 鉴权与密钥：safeStorage、智谱 client-sign 协议全貌 | A |
| 06 | [autoclaw2-06-renderer.md](autoclaw2-06-renderer.md) | Renderer：zwork.html、窗口安全、zwork-artifact 协议 CSP | A |
| 07 | [autoclaw2-07-persistence.md](autoclaw2-07-persistence.md) | runtime.sqlite 120 表 schema、账本运维 | A |
| 08 | [autoclaw2-08-im-browser.md](autoclaw2-08-im-browser.md) | IM 通道（feishu/wechat/telegram/…）+ 内置浏览器隔离 | A |
| 09 | [autoclaw2-09-ipc-vocab-updates.md](autoclaw2-09-ipc-vocab-updates.md) | IPC 请求词汇 317 条归类 + 更新链路 | A |
| 10 | [autoclaw2-10-resource-packs.md](autoclaw2-10-resource-packs.md) | vertical-packs、预装技能、skill-runtime、模型预设 | A |

## 通用纪律（沿用 zcode 语料约定）

1. 区分「实测（A）」与「推断（B）」；推断必须写明依据。
2. 搜索命中不等于结论：bundle 内同名常量可能出现在不同模块，先读上下文再定性。
3. 先查本文档索引，避免重复劳动；每篇文档末尾留「未决问题」。
4. `work/main-src-paths.txt` 是 main.cjs 的 3691 个源码路径图谱，查模块先查它。
