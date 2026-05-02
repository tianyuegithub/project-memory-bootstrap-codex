# 采用指南

## 最小接入

在项目根目录执行：

```bash
project-memory-bootstrap-codex init . --with-agents
```

然后提交这些文件：

```text
AGENTS.md
CODEX.md
.codex/memory/MEMORY.md
.codex/memory/YYYY-MM-DD.md
```

## 团队推荐接入

1. 在团队模板仓库中加入 `AGENTS.md` 和 `CODEX.md`。
2. 要求每次进入项目先读取 `CODEX.md` 和 `.codex/memory/*`。
3. 把“今天发生了什么”先写入当天文件。
4. 只有核验过且可复用的事实才晋升到长期记忆。
5. 在 PR review 中检查项目记忆是否需要同步更新。

## 低 token 运行方式

如果项目记忆变大，不要继续默认读取全部 `.codex/memory/*`。推荐改成：

```bash
project-memory-bootstrap-codex index-memory .
project-memory-bootstrap-codex memory-status .
project-memory-bootstrap-codex search-memory . "你当前任务的关键词"
project-memory-bootstrap-codex get-memory . <id>
project-memory-bootstrap-codex context-memory . "你当前任务的关键词" --max-tokens 800
project-memory-bootstrap-codex context-memory . --id <id> --max-tokens 800
```

把 `search-memory` 的结果当成索引，只在需要时用 `get-memory` 按 ID 展开单条，或按来源文件回读。`context-memory` 的输出适合作为本轮任务提示的一小段背景，但不能替代实时源码、配置、脚本和测试证据。

## 自动记忆整合

长期使用后，可以把记忆整合接入 Codex 定时自动化。推荐自动化只做 dry-run：

```bash
project-memory-bootstrap-codex memory-status .
project-memory-bootstrap-codex index-memory .
project-memory-bootstrap-codex consolidate-memory . --dry-run
```

报告默认写入 `.codex/cache/memory-reports/`，不会修改项目记忆。人工审查报告后，再选择性应用明确安全的项：

```bash
project-memory-bootstrap-codex consolidation-report . --latest
project-memory-bootstrap-codex consolidate-memory . --apply --from-report latest --only I001 --safe
```

## 不建议的用法

- 不要把 `CODEX.md` 写成详细设计文档。
- 不要把聊天记录原样复制进长期记忆。
- 不要在运行时默认把所有记忆文件塞进上下文。
- 不要用项目记忆替代真实测试。
- 不要把密钥、token、账号密码写入记忆文件。
- 不要提交 `.codex/cache/memory-index.sqlite` 或 `.codex/cache/memory-reports/`。
- 不要让定时自动化直接删除、覆盖或裁决长期记忆。
- 不要在证据冲突时直接覆盖长期条目。

## 开源分发建议

如果你要把机制推广给其它 Codex 用户，建议按以下层级介绍：

1. 原生 Codex 有 `AGENTS.md` 和 Memories。
2. 原生缺少严格的项目级记忆治理。
3. 本项目提供仓库内、可审计、可核验的项目记忆层。
4. 这套机制适合多仓库、长期维护、多人协作、客户交付和生产排障场景。
