# AGENTS.md

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
