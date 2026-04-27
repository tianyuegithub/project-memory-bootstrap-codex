# CODEX.md

## 项目定位

`project-memory-bootstrap-codex` 是一个非官方社区开源包，为 OpenAI Codex 用户提供项目级可治理记忆机制。它不替代原生 `AGENTS.md` 或 Codex Memories，而是在仓库内建立可审计、可核验、可复用的项目记忆层。

## 模块结构

- `src/project_memory_bootstrap_codex/`: 零依赖 Python CLI 与核心逻辑。
- `templates/`: `AGENTS.md`、`CODEX.md`、`.codex/memory/*` 初始化模板。
- `skills/project-memory-bootstrap-codex/`: 可分发的 Codex skill。
- `docs/`: 原生机制对比、治理规则、采用指南。
- `tests/`: CLI 与初始化逻辑测试。

## 关键链路

1. 用户执行 `project-memory-bootstrap-codex init <path>`。
2. CLI 优先识别 Git 根目录；非 Git 项目使用传入路径。
3. CLI 创建或跳过 `CODEX.md`、`.codex/memory/MEMORY.md`、`.codex/memory/YYYY-MM-DD.md`。
4. 可选生成 `AGENTS.md`，把项目记忆协议变成 Codex 可读取的项目指令。
5. 用户后续进入项目时，Codex 依据 `AGENTS.md` 和 skill 执行 bootstrap。

## 运行入口与端口

- CLI 入口: `project-memory-bootstrap-codex`
- 模块入口: `PYTHONPATH=src python3 -m project_memory_bootstrap_codex.cli`
- 本项目不启动常驻服务，不占用端口。

## 脚本索引

当前仓库没有独立 `*.sh` 运维脚本。关键命令：

- `PYTHONPATH=src python3 -m unittest discover -s tests`: 运行单元测试。
- `PYTHONPATH=src python3 -m project_memory_bootstrap_codex.cli init .`: 初始化当前仓库项目记忆。
- `PYTHONPATH=src python3 -m project_memory_bootstrap_codex.cli doctor .`: 检查项目记忆完整性。
- `PYTHONPATH=src python3 -m project_memory_bootstrap_codex.cli scan-scripts .`: 扫描关键 shell 脚本。

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
