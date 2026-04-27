# Project Long-Term Memory

本文件只记录已核验、可跨任务复用、3 天后仍大概率成立的项目级长期事实。

## 长期条目

### 项目记忆治理已启用
- 类型: durable_rule
- 范围: repo
- 来源: `CODEX.md`; `.codex/memory/MEMORY.md`
- 最近核验: 2026-04-26
- 稳定性: high
- 失效条件: 项目放弃仓库内项目记忆治理，或改用其它记忆结构
- 替代关系: none
- 内容:
  - 本项目使用 `CODEX.md`、`.codex/memory/MEMORY.md`、`.codex/memory/YYYY-MM-DD.md` 分层维护项目记忆。

### 开源包定位与分发形态
- 类型: durable_fact
- 范围: repo
- 来源: `README.md`; `pyproject.toml`; `skills/project-memory-bootstrap-codex/SKILL.md`
- 最近核验: 2026-04-27
- 稳定性: high
- 失效条件: 包名、CLI 入口、skill 目录或项目边界发生变更
- 替代关系: none
- 内容:
  - 本仓库以 `project-memory-bootstrap-codex` 为包名、CLI 命令名和 Codex skill 名。
  - 本仓库提供零依赖 Python CLI、项目记忆模板、文档和可分发 Codex skill。
  - 本仓库是非官方社区项目，不替代 OpenAI 官方 Codex Memories。

### README 展示层包含中英文介绍视频
- 类型: durable_fact
- 范围: repo
- 来源: `README.md`; `docs/videos/project-memory-bootstrap-codex-intro-zh.mp4`; `docs/videos/project-memory-bootstrap-codex-intro-en.mp4`; `docs/videos/project-memory-bootstrap-codex-intro-zh.png`; `docs/videos/project-memory-bootstrap-codex-intro-en.png`
- 最近核验: 2026-04-27
- 稳定性: medium
- 失效条件: README 媒体区、视频文件路径、默认分支名或 GitHub README 渲染策略变更
- 替代关系: none
- 内容:
  - README 使用两个视频封面图链接到 MP4：中文版 `docs/videos/project-memory-bootstrap-codex-intro-zh.mp4` 和英文版 `docs/videos/project-memory-bootstrap-codex-intro-en.mp4`。
  - 中文版结尾突出 GitHub 账号 `@tianyuegithub`。
  - 视频资产用于 GitHub README 展示，不参与 Python CLI 运行链路。

### 当前验证门禁
- 类型: durable_rule
- 范围: repo
- 来源: `AGENTS.md`; `.github/workflows/test.yml`; 本轮命令输出
- 最近核验: 2026-04-27
- 稳定性: high
- 失效条件: 测试框架、CLI 模块入口、GitHub Actions 工作流或 skill 校验方式变化
- 替代关系: none
- 内容:
  - 本仓库基础验证命令为 `PYTHONPATH=src python3 -m unittest discover -s tests`。
  - CLI 完整性验证命令为 `PYTHONPATH=src python3 -m project_memory_bootstrap_codex.cli doctor .`。
  - skill 结构验证使用 skill-creator 的 `quick_validate.py skills/project-memory-bootstrap-codex`。

### 托管文件写入必须限制在项目根目录内
- 类型: durable_rule
- 范围: module:cli
- 来源: `src/project_memory_bootstrap_codex/bootstrap.py`; `tests/test_bootstrap.py`
- 最近核验: 2026-04-27
- 稳定性: high
- 失效条件: 写入函数、托管文件路径或 root 解析策略变化
- 替代关系: none
- 内容:
  - CLI 在写入 `CODEX.md`、`.codex/memory/*`、`AGENTS.md` 前，必须解析写入目标和父目录，拒绝任何指向项目根目录外的 symlink 逃逸路径。
