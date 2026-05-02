# Project Memory Governance v2

## 来源优先级

1. 用户明确确认
2. 实时源码、配置、脚本、测试
3. 项目 `CODEX.md`
4. 项目 `.codex/memory/MEMORY.md`
5. 项目 `.codex/memory/YYYY-MM-DD.md`
6. 仓库内其它 `README*`、`docs/*`、agent 文件
7. legacy memory
8. 全局 `~/.codex/memories/*`
9. 推断

## 三层文件职责

### CODEX.md

项目第一眼说明书，只保留首屏级事实与索引。固定 7 个章节：

1. 项目定位
2. 模块结构
3. 关键链路
4. 运行入口与端口
5. 脚本索引
6. 上下文来源优先级
7. 记忆治理规则

### .codex/memory/MEMORY.md

长期记忆，只收录满足以下条件的内容：

- 本轮已核验。
- 可跨任务复用。
- 3 天后仍大概率成立。

每个长期条目必须带轻量元数据头：

- 类型
- 范围
- 来源
- 最近核验
- 稳定性
- 失效条件
- 替代关系

### .codex/memory/YYYY-MM-DD.md

当天事件日志，记录事件而不是聊天流水账。每个事件包含：

- 类别
- 来源
- 验证命令/证据
- 影响范围
- 是否可晋升
- 记录

## 写入流程

1. 先写当天文件。
2. 判断是否满足长期晋升条件。
3. 只有影响项目第一眼认知时，才同步更新 `CODEX.md`。

## 运行时读取流程

治理文件是事实来源，但运行时不应默认全文展开。推荐流程：

1. 执行 `project-memory-bootstrap-codex index-memory .` 构建或刷新 `.codex/cache/memory-index.sqlite`。
2. 用 `project-memory-bootstrap-codex memory-status .` 检查索引是否存在、是否相对源文件过期。
3. 用 `project-memory-bootstrap-codex search-memory . "<query>"` 获取轻量结果表，只查看 ID、来源、标题和估算 token。
4. 对明确要展开的结果，用 `project-memory-bootstrap-codex get-memory . <id>` 精确取回单个片段。
5. 用 `project-memory-bootstrap-codex context-memory . "<query>" --max-tokens 800` 按查询生成预算受控的小上下文；也可以用 `project-memory-bootstrap-codex context-memory . --id <id> --max-tokens 800` 按 ID 生成上下文。
6. 对端口、路径、命令、部署形态等易漂移事实，命中后仍要回读源文件或实时源码/脚本做回验。

`.codex/cache/memory-index.sqlite` 是本地运行缓存，不属于项目记忆治理文件，不应提交或推送。

## 记忆整合流程

项目记忆变大后，推荐定期做保守整合巡检：

```bash
project-memory-bootstrap-codex memory-status .
project-memory-bootstrap-codex index-memory .
project-memory-bootstrap-codex consolidate-memory . --dry-run
```

`consolidate-memory --dry-run` 默认把 Markdown 与 JSON 报告写入 `.codex/cache/memory-reports/`，只报告重复、过期、冲突、可晋升当天事件和索引状态，不修改 `.codex/memory/*`。

查看报告：

```bash
project-memory-bootstrap-codex consolidation-reports .
project-memory-bootstrap-codex consolidation-report . --latest
```

执行报告项必须保守处理。初版 `--apply --safe` 只适合索引重建这类本地缓存动作；冲突裁决、删除长期记忆、覆盖用户确认口径或改写 durable memory，都必须人工审查后由 Codex 单独执行。

## 冲突规则

证据冲突但未核实时，只写入当天文件，类别标记为 `conflict`，不得直接改写长期记忆。

当用户明确口径与仓库实时事实冲突时，可以临时按用户口径推进，但必须写入当天文件并标注 `待回验`。

## 脚本治理规则

只有核实到脚本正文或真实执行证据的用途、依赖、I/O、常见用法，才允许晋升为长期脚本知识。仅凭文件名或目录扫描结果不得晋升。
