# 原生 Codex 记忆与项目级记忆治理

## 结论

`project-memory-bootstrap-codex` 是对原生 Codex 记忆机制的补全，不是替代。

更准确的关系是：

```text
AGENTS.md = 原生项目规则入口
Codex Memories = 原生跨会话记忆
CODEX.md + .codex/memory/* = 项目级可治理记忆层
```

## 原生机制包含什么

### AGENTS.md

`AGENTS.md` 是原生 Codex 的项目或目录级 instruction 入口。它适合表达：

- 在这个仓库中如何工作。
- 需要遵守哪些工程规则。
- 哪些命令、目录、约定优先。

它是项目级 instruction，不是自动沉淀和治理项目事实的长期记忆系统。

### Codex Memories

Codex Memories 是可选的长期记忆能力，偏向跨会话复用用户偏好、历史经验、稳定工作流和常见项目线索。

它通常不是随仓库维护的审计型项目知识库，也不天然要求来源、核验时间、失效条件、冲突记录和晋升规则。

## 本项目补齐什么

本项目提供仓库内的项目级记忆治理：

- `CODEX.md`: 第一眼项目说明书。
- `.codex/memory/MEMORY.md`: 长期项目事实。
- `.codex/memory/YYYY-MM-DD.md`: 当天事件日志。
- 固定来源优先级。
- 长期记忆晋升规则。
- 冲突处理规则。
- 脚本治理规则。
- 回写与去重规则。

## 推荐边界

- 团队强约束写入 `AGENTS.md`。
- 项目第一眼认知写入 `CODEX.md`。
- 稳定项目事实写入 `.codex/memory/MEMORY.md`。
- 当天发现、决策、迁移、冲突写入 `.codex/memory/YYYY-MM-DD.md`。
- 个人偏好和跨项目经验交给 Codex Memories。
