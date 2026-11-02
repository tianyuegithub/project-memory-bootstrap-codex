# 项目协作入口

本仓库维护项目共享记忆程序，交互使用中文。README拥有项目地图，`src/project_memory_bootstrap_codex/resources/shared-memory-protocol.md`拥有共享协议；CLI `protocol`读取同一资源。

- 普通开发按需读取相关源文件，不创建第二份CODEX手册或补造日期日记。
- 修改CLI、格式、模板或生命周期行为时同步相关文档及有意义的验证。模板正文唯一拥有者是templates.py，磁盘templates为分发副本。
- 保留已有工作树改动。受管知识经候选/提案/提交修改，不绕过版本和抑制，不用原生记忆或缓存替代当前证据。
- 秘密、运行状态、来源原文与缓存不进入Git；公开样例使用合成数据。
- 验证入口：`PYTHONPATH=src python3 -m unittest discover -s tests` 与 `PYTHONPATH=src python3 -m project_memory_bootstrap_codex.cli doctor .`。程序检查不证明任何宿主已接入或任何业务已验收。
