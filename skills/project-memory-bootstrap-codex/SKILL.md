---
name: project-memory-bootstrap-codex
description: Initialize or refresh project-level auditable memory for Codex repositories. Use when entering an unfamiliar repository, switching projects, a repo lacks CODEX.md, project context is scattered across README/docs/agent files, or Codex should maintain CODEX.md plus .codex/memory/MEMORY.md and .codex/memory/YYYY-MM-DD.md.
---

# Project Memory Bootstrap Codex

## Goal

Create or refresh a repository-local memory layer that complements native Codex `AGENTS.md` and Memories:

- `CODEX.md`: first-screen project briefing.
- `.codex/memory/MEMORY.md`: durable project facts.
- `.codex/memory/YYYY-MM-DD.md`: day-level event log.

## Workflow

1. Locate the project root.
   - Prefer `git rev-parse --show-toplevel`.
   - If the path is not in Git, use the current working directory.

2. Read high-signal context in this order.
   - `CODEX.md`
   - `AGENTS.md`, `CLAUDE.md`, `CURSOR.md`
   - `README*`, `docs/*`
   - Build files such as `package.json`, `pyproject.toml`, `pom.xml`, `Makefile`

3. Discover legacy or agent memory files.
   - `.codex/memory/*`
   - `**/MEMORY.md`
   - `**/memory/*.md`
   - `.workbuddy/*`, `.claude/*`, `.cursor/*`

4. Use this source priority.
   - User-confirmed facts
   - Live source/config/scripts/tests
   - Project `CODEX.md`
   - Project `.codex/memory/MEMORY.md`
   - Project `.codex/memory/YYYY-MM-DD.md`
   - Repo docs and agent files
   - Legacy memory
   - Global `~/.codex/memories/*`
   - Inference

5. Create or update `CODEX.md` with exactly seven sections.
   - 项目定位
   - 模块结构
   - 关键链路
   - 运行入口与端口
   - 脚本索引
   - 上下文来源优先级
   - 记忆治理规则

6. Maintain `.codex/memory/MEMORY.md`.
   - Only promote facts that are verified in this turn, reusable across tasks, and likely still true after 3 days.
   - Each durable entry must include: 类型, 范围, 来源, 最近核验, 稳定性, 失效条件, 替代关系.

7. Maintain `.codex/memory/YYYY-MM-DD.md`.
   - Write today first, then decide whether to promote to long-term memory.
   - Each event must include: 类别, 来源, 验证命令/证据, 影响范围, 是否可晋升.

8. Index shell scripts.
   - Scan root `*.sh`, `scripts/**/*.sh`, `bin/**/*.sh`, `tools/**/*.sh`, `deploy/**/*.sh`.
   - Only promote script knowledge after reading the script body or seeing real execution evidence.

9. For runtime reads, prefer indexed lookup before opening full memory files.
   - Build or refresh the index with `project-memory-bootstrap-codex index-memory .`.
   - Check freshness with `project-memory-bootstrap-codex memory-status .`.
   - Search with `project-memory-bootstrap-codex search-memory . "<query>"`.
   - Fetch one selected chunk with `project-memory-bootstrap-codex get-memory . <id>`.
   - Generate a bounded context with `project-memory-bootstrap-codex context-memory . "<query>" --max-tokens 800`.
   - If a search result ID is already known, generate context with `project-memory-bootstrap-codex context-memory . --id <id> --max-tokens 800`.
   - Treat index results as leads; re-open source files before relying on drift-prone facts.

10. For scheduled memory consolidation, generate a dry-run report first.
   - Run `project-memory-bootstrap-codex memory-status .`.
   - Rebuild the local index with `project-memory-bootstrap-codex index-memory .` when missing or stale.
   - Run `project-memory-bootstrap-codex consolidate-memory . --dry-run`.
   - Read reports with `project-memory-bootstrap-codex consolidation-report . --latest`.
   - Only apply explicitly selected safe items with `project-memory-bootstrap-codex consolidate-memory . --apply --from-report latest --only <id> --safe`.
   - Never let automation delete, overwrite, or resolve durable-memory conflicts without human review.

## CLI Shortcut

If the repository has installed `project-memory-bootstrap-codex`, prefer:

```bash
project-memory-bootstrap-codex init . --with-agents
project-memory-bootstrap-codex doctor .
project-memory-bootstrap-codex index-memory .
project-memory-bootstrap-codex memory-status .
project-memory-bootstrap-codex consolidate-memory . --dry-run
```

## Guardrails

- Do not overwrite existing project memory blindly.
- Do not copy raw chat transcripts into long-term memory.
- Do not store secrets, credentials, tokens, or private customer data.
- Do not commit `.codex/cache/memory-index.sqlite` or `.codex/cache/memory-reports/`; they are local runtime caches.
- If evidence conflicts, write a `conflict` event to the day file and do not change durable memory until verified.
