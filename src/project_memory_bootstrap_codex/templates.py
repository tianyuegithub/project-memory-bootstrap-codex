"""Text templates used by the project memory bootstrap CLI."""

from __future__ import annotations

CODEX_TEMPLATE = """# CODEX.md

## 项目定位

`{project_name}` 的项目定位待补充。请基于实时源码、配置、脚本、测试和用户确认进行更新。

## 模块结构

- 待补充：核心模块、目录职责、边界。

## 关键链路

- 待补充：主要业务链路、入口、数据流、关键依赖。

## 运行入口与端口

- 待补充：启动命令、服务端口、健康检查、依赖服务。

## 脚本索引

{script_index}

## 上下文来源优先级

1. 用户明确确认
2. 实时源码、配置、脚本、测试
3. 项目 `CODEX.md`
4. 项目 `.codex/memory/MEMORY.md`
5. 项目 `.codex/memory/YYYY-MM-DD.md`
6. 仓库内其它 `README*`、`docs/*`、agent 文件
7. legacy memory
8. 全局 `~/.codex/memories/*`
9. 推断

## 记忆治理规则

- `CODEX.md` 只保留项目第一眼事实与索引。
- `.codex/memory/MEMORY.md` 只保留已核验、可跨任务复用、3 天后仍大概率成立的长期事实。
- `.codex/memory/YYYY-MM-DD.md` 记录当天事件、验证、决策、迁移、冲突和待回验。
- 长期条目必须包含类型、范围、来源、最近核验、稳定性、失效条件、替代关系。
- 证据冲突但未核实时，只写当天文件，类别标记为 `conflict`。
- 当前任务命中已有长期条目时，刷新 `最近核验`。
"""

AGENTS_TEMPLATE = """# AGENTS.md

请使用中文进行交互，保持专业、简洁、准确、完整。

## 项目记忆初始化协议

每次进入本项目或切换到不熟悉的仓库时，先执行项目记忆初始化：

1. 定位项目根目录。若存在 Git 仓库，优先使用 Git 根目录，否则使用当前工作目录。
2. 优先读取根目录 `CODEX.md`。若不存在，读取 `AGENTS.md`、`CLAUDE.md`、`CURSOR.md`、`README*`、`docs/*` 和构建文件，并创建 `CODEX.md`。
3. 通用发现已有记忆和 agent 遗留文件，重点关注 `.codex/memory/*`、`**/MEMORY.md`、`**/memory/*.md`、`.workbuddy/*`、`.claude/*`、`.cursor/*`。
4. 长期稳定事实写入 `.codex/memory/MEMORY.md`，当天事项写入 `.codex/memory/YYYY-MM-DD.md`，保留原始文件。
5. 识别关键 `*.sh` 脚本，覆盖根目录以及 `scripts/`、`bin/`、`tools/`、`deploy/`。
6. 完成任务后，如新增稳定项目事实、约束、脚本认知或运行规则，同步更新 `CODEX.md` 与 `.codex/memory/*`。

## 记忆治理规则

- 来源优先级：用户明确确认 > 实时源码/配置/脚本/测试 > 项目 `CODEX.md` > 项目 `.codex/memory/MEMORY.md` > 项目 `.codex/memory/YYYY-MM-DD.md` > 仓库内其它文档和 agent 文件 > legacy memory > 全局 `~/.codex/memories/*` > 推断。
- 写入流程：先写当天文件，再判断是否满足长期晋升条件，只有影响“项目第一眼认知”时才同步改 `CODEX.md`。
- 长期晋升条件：本轮已核验、可跨任务复用、3 天后仍大概率成立。
- 冲突规则：证据冲突但未核实时，只写入当天文件，类别标记为 `conflict`。
- 去重规则：`CODEX.md` 只放摘要与索引，细节型长期知识只常驻 `MEMORY.md`。
"""

MEMORY_TEMPLATE = """# Project Long-Term Memory

本文件只记录已核验、可跨任务复用、3 天后仍大概率成立的项目级长期事实。

## 条目模板

### 示例：项目记忆治理已启用
- 类型: durable_rule
- 范围: repo
- 来源: `CODEX.md`; `.codex/memory/MEMORY.md`
- 最近核验: {today}
- 稳定性: high
- 失效条件: 项目放弃仓库内项目记忆治理，或改用其它记忆结构
- 替代关系: none
- 内容:
  - 本项目使用 `CODEX.md`、`.codex/memory/MEMORY.md`、`.codex/memory/YYYY-MM-DD.md` 分层维护项目记忆。
"""

DAY_TEMPLATE = """# Project Memory Events - {today}

## 事件 1: 初始化项目记忆
- 类别: verification
- 来源: `project-memory-bootstrap-codex init`
- 验证命令/证据: 初始化命令成功创建或检查项目记忆文件
- 影响范围: repo
- 是否可晋升: later
- 记录:
  - 已建立 `CODEX.md`、`.codex/memory/MEMORY.md`、当天事件文件的项目记忆结构。
"""

SCRIPT_EMPTY = "- 未发现关键 `*.sh` 脚本。后续发现后按真实脚本正文补充用途、依赖、端口和用法。"
