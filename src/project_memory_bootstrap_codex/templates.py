"""Executable owner of initialization templates; templates/ holds distribution copies."""

from __future__ import annotations

README_TEMPLATE = """# {project_name}

本文件是项目地图，以下导航均待基于当前源码、配置和正式文档核验。

- 项目定位与模块职责：待核验。
- 构建、测试和运行入口：待核验。
- 正式文档与领域合同：待核验。
- 项目协作指令入口：`AGENTS.md`。
- 共享记忆协议：`project-memory-bootstrap-codex protocol`（随程序提供的唯一协议正文）。
"""

CODEX_TEMPLATE = """# CODEX.md

此文件仅为旧版分发占位，不是项目指令入口，初始化器不再创建此文件。
项目指令由 `AGENTS.md` 拥有，项目地图由 `README.md` 拥有。
共享记忆协议见 `project-memory-bootstrap-codex protocol`。
"""

AGENTS_TEMPLATE = """# AGENTS.md

请使用中文进行交互，保持专业、简洁、准确、完整。

## 项目入口与按需检索

- 本文件是唯一项目指令入口；`README.md` 拥有项目地图与导航。
- 启动时按任务需要读取项目地图和对应正式文档，不全量初始化或加载记忆库，不因缺少日期文件补造日记。
- 需要历史线索时按需使用 `search-memory` / `get-memory` / `context-memory`，只展开相关事实及其适用条件。
- 用户当前要求、实时源码与有效正式合同优先；读取记忆或摘要不等于重新核验，来源变化时回看对应 owner。

## 共享记忆边界

- 进入项目先执行 `project-memory-bootstrap-codex project-context .` 和 `protocol --brief`；已有注入身份可复用。未登记是否自动启用由用户的全局默认授权决定，软件不替所有安装者默认扩大范围。
- 任务取得重要新证据后经 `capture-memory → propose-memory → commit-memory` 发布；结束前判断有无值得记忆的变化，没有就不造日记。原生笔记只保存共享根、fact_id与读取入口，不复制事实正文。
- 共享记忆协议：`project-memory-bootstrap-codex protocol`。按该协议核对项目身份、来源、事实版本、核验范围和状态。
- `.codex/memory/MEMORY.md` 是受管事实正文；通过受控提案与提交维护，不直接编辑受管块，不自动晋升未核验材料。
- 人工修订受管正文前先停止自动写入，再由协议程序采纳修订。旧日期文件保留作历史线索，不按年龄自动判无效或自动清理。
- 候选捕获、语义核验与提交分别记录；初始化不代表业务事实已核验，也不代表已启用持续维护。
- `.codex/cache/`、`.codex/memory-state/` 与 `.state/` 为本机状态，不提交 Git；凭证不得进入记忆正文或日志。
- 记忆只提供任务线索，不扩大业务修改、发布、跨项目读取或数据删除授权。
"""

MEMORY_TEMPLATE = """# 项目共享记忆

当前没有受管事实。初始化只建立文件，不证明业务事实、项目身份注册或持续维护已经完成。

事实块的格式、来源核验、更正与遗忘合同见 `project-memory-bootstrap-codex protocol`。
通过受控提案与提交维护本文件；读取历史材料不等于重新核验。
"""

DAY_TEMPLATE = """# 项目记忆初始化事件 - {today}

- 事件：调用 `project-memory-bootstrap-codex init` 时显式指定本日期，建立或检查初始化文件。
- 范围：仅记录初始化操作，不代表已核验业务事实、注册项目或启用持续维护。
"""

SCRIPT_EMPTY = "- 未发现关键 `*.sh` 脚本。后续发现后按真实脚本正文补充用途、依赖、端口和用法。"
