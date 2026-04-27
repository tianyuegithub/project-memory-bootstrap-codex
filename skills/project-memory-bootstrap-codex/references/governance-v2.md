# Governance v2 Reference

## Long-term promotion rule

Promote to `.codex/memory/MEMORY.md` only when all are true:

- Verified in the current turn.
- Reusable across future tasks.
- Likely to remain valid after 3 days.

## Durable metadata

Each durable entry must include:

- 类型: durable_fact | durable_rule | script_knowledge | diagnostic_rule
- 范围: repo | module:<name> | env:<name>
- 来源
- 最近核验
- 稳定性: high | medium
- 失效条件
- 替代关系

## Event metadata

Each day-memory event must include:

- 类别: observation | verification | decision | migration | conflict
- 来源
- 验证命令/证据
- 影响范围
- 是否可晋升: yes | no | later
