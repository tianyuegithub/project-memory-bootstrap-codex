# AGENTS.md

本仓库维护 Codex 项目级可治理记忆机制。交互默认使用中文，输出保持专业、简洁、准确、完整。

## 工作规则

1. 进入本仓库后先读取 `CODEX.md`，再读取 `.codex/memory/MEMORY.md` 和当天记忆。
2. 修改 CLI、模板、skill 或治理规则后，同步更新相关文档和测试。
3. 不要把聊天流水账写入长期记忆。长期记忆只收录已核验、可跨任务复用、3 天后仍大概率成立的事实。
4. 回写记忆前先重读目标文件，按主题小节更新，不做整文件盲改。
5. 完成开发前至少运行：

```bash
PYTHONPATH=src python3 -m unittest discover -s tests
PYTHONPATH=src python3 -m project_memory_bootstrap_codex.cli doctor .
```

## 项目边界

本项目是非官方社区项目，不修改 Codex 原生运行时，不替代 OpenAI 官方 Memories。它提供仓库内模板、CLI 和 skill，用于建立可审计的项目级记忆治理层。
