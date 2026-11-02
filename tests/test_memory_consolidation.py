from __future__ import annotations

import datetime as dt
import json
import shutil
import stat
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

from project_memory_bootstrap_codex.cli import main
from project_memory_bootstrap_codex.memory_consolidation import (
    agent_apply_consolidation,
    apply_consolidation_report,
    generate_consolidation_report,
    latest_report,
    list_reports,
)
from project_memory_bootstrap_codex.memory_index import index_status


class MemoryConsolidationTests(unittest.TestCase):
    def test_dry_run_writes_cache_report_and_preserves_memory_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            memory_dir = root / ".codex" / "memory"
            memory_dir.mkdir(parents=True)
            memory = memory_dir / "MEMORY.md"
            memory.write_text(
                "# Project Long-Term Memory\n\n"
                "### 运行入口\n"
                "- 类型: durable_fact\n"
                "- 最近核验: 2026-05-02\n"
                "- 内容:\n"
                "  - CLI only.\n",
                encoding="utf-8",
            )
            original_memory = memory.read_text(encoding="utf-8")

            report = generate_consolidation_report(
                root,
                now=dt.datetime(2026, 5, 2, 3, 0, 0),
                write=True,
            )

            self.assertEqual(memory.read_text(encoding="utf-8"), original_memory)
            self.assertTrue(report.markdown_path.exists())
            self.assertTrue(report.json_path.exists())
            self.assertEqual(stat.S_IMODE(report.markdown_path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(report.json_path.stat().st_mode), 0o600)
            self.assertIn(".codex/cache/memory-reports", report.markdown_path.as_posix())
            self.assertIn("I001", report.markdown_path.read_text(encoding="utf-8"))
            data = json.loads(report.json_path.read_text(encoding="utf-8"))
            self.assertEqual(data["items"][0]["id"], "I001")
            self.assertTrue(data["items"][0]["safe_apply"])

    def test_report_detects_duplicate_stale_promote_and_conflict_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            memory_dir = root / ".codex" / "memory"
            memory_dir.mkdir(parents=True)
            (memory_dir / "MEMORY.md").write_text(
                "# Project Long-Term Memory\n\n"
                "### 服务端口\n"
                "- 类型: durable_fact\n"
                "- 最近核验: 2026-05-01\n"
                "- 内容:\n"
                "  - 服务端口为 8080。\n\n"
                "### 服务端口副本\n"
                "- 类型: durable_fact\n"
                "- 最近核验: 2026-05-01\n"
                "- 内容:\n"
                "  - 服务端口为 8080。\n\n"
                "### 旧部署入口\n"
                "- 类型: durable_fact\n"
                "- 最近核验: 2026-03-20\n"
                "- 内容:\n"
                "  - 部署环境为 staging。\n",
                encoding="utf-8",
            )
            (memory_dir / "2026-05-01.md").write_text(
                "# Project Memory Events - 2026-05-01\n\n"
                "## 事件 1: 新入口已核验\n"
                "- 类别: verification\n"
                "- 来源: `CODEX.md`\n"
                "- 验证命令/证据: `doctor`\n"
                "- 影响范围: repo\n"
                "- 是否可晋升: yes\n"
                "- 记录:\n"
                "  - 新入口已核验。\n\n"
                "## 事件 2: README 与 CODEX 冲突\n"
                "- 类别: conflict\n"
                "- 来源: `README.md`; `CODEX.md`\n"
                "- 验证命令/证据: 人工对比\n"
                "- 影响范围: docs\n"
                "- 是否可晋升: no\n",
                encoding="utf-8",
            )

            report = generate_consolidation_report(
                root,
                now=dt.datetime(2026, 5, 2, 3, 0, 0),
                write=True,
            )
            kinds = {item.kind for item in report.items}

            self.assertIn("index", kinds)
            self.assertIn("duplicate", kinds)
            self.assertIn("stale", kinds)
            self.assertIn("promote", kinds)
            self.assertIn("conflict", kinds)

    def test_apply_safe_index_item_rebuilds_index_without_editing_memory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            memory_dir = root / ".codex" / "memory"
            memory_dir.mkdir(parents=True)
            memory = memory_dir / "MEMORY.md"
            memory.write_text(
                "# Project Long-Term Memory\n\n"
                "### 索引入口\n"
                "- 类型: durable_fact\n"
                "- 最近核验: 2026-05-02\n"
                "- 内容:\n"
                "  - 需要索引。\n",
                encoding="utf-8",
            )
            original_memory = memory.read_text(encoding="utf-8")
            generate_consolidation_report(
                root,
                now=dt.datetime(2026, 5, 2, 3, 0, 0),
                write=True,
            )

            result = apply_consolidation_report(root, from_report="latest", only=["I001"], safe=True)

            self.assertEqual(memory.read_text(encoding="utf-8"), original_memory)
            self.assertTrue(index_status(root).is_current)
            self.assertEqual(result.applied, ("I001",))
            self.assertEqual(result.skipped, ())

    def test_agent_apply_archives_duplicate_worktree_legacy_memory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            memory_dir = root / ".codex" / "memory"
            memory_dir.mkdir(parents=True)
            (memory_dir / "MEMORY.md").write_text("# Memory\n", encoding="utf-8")
            legacy = root / ".workbuddy" / "memory" / "MEMORY.md"
            worktree_copy = root / ".worktrees" / "feature-a" / ".workbuddy" / "memory" / "MEMORY.md"
            duplicate_text = (
                "# Legacy Memory\n\n"
                "### 数据账号授权模块\n"
                "- 类型: durable_fact\n"
                "- 最近核验: 2026-05-01\n"
                "- 内容:\n"
                "  - 授权模块使用 db_privilege 位掩码。\n"
            )
            legacy.parent.mkdir(parents=True)
            worktree_copy.parent.mkdir(parents=True)
            legacy.write_text(duplicate_text, encoding="utf-8")
            worktree_copy.write_text(duplicate_text, encoding="utf-8")

            result = agent_apply_consolidation(
                root,
                now=dt.datetime(2026, 5, 3, 3, 30, 0),
            )

            archived_rel = ".worktrees/feature-a/.workbuddy/memory/MEMORY.md"
            archived_path = root / ".codex" / "cache" / "memory-archive" / "20260503-033000" / archived_rel
            self.assertIn(archived_rel, result.archived)
            self.assertTrue(legacy.exists())
            self.assertFalse(worktree_copy.exists())
            self.assertTrue(archived_path.exists())
            self.assertTrue(index_status(root).is_current)

    def test_agent_apply_removes_duplicate_sections_from_mixed_worktree_memory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            memory_dir = root / ".codex" / "memory"
            memory_dir.mkdir(parents=True)
            (memory_dir / "MEMORY.md").write_text("# Memory\n", encoding="utf-8")
            legacy = root / ".workbuddy" / "memory" / "2026-04-11.md"
            worktree_copy = root / ".worktrees" / "feature-a" / ".workbuddy" / "memory" / "2026-04-11.md"
            duplicate_section = (
                "## 数据账号授权模块\n"
                "- 类型: durable_fact\n"
                "- 最近核验: 2026-05-01\n"
                "- 内容:\n"
                "  - 授权模块使用 db_privilege 位掩码。\n"
            )
            unique_section = (
                "## Worktree 专属观察\n"
                "- 类型: observation\n"
                "- 内容:\n"
                "  - 这个 worktree 仍有独有内容。\n"
            )
            legacy.parent.mkdir(parents=True)
            worktree_copy.parent.mkdir(parents=True)
            legacy.write_text("# 2026-04-11\n\n" + duplicate_section, encoding="utf-8")
            worktree_copy.write_text("# 2026-04-11\n\n" + duplicate_section + "\n" + unique_section, encoding="utf-8")

            result = agent_apply_consolidation(
                root,
                now=dt.datetime(2026, 5, 3, 3, 45, 0),
            )

            cleaned_section = ".worktrees/feature-a/.workbuddy/memory/2026-04-11.md#数据账号授权模块"
            archive_path = root / ".codex" / "cache" / "memory-archive" / "20260503-034500" / ".worktrees" / "feature-a" / ".workbuddy" / "memory" / "2026-04-11.md"
            cleaned_text = worktree_copy.read_text(encoding="utf-8")
            self.assertIn(cleaned_section, result.cleaned)
            self.assertNotIn("数据账号授权模块", cleaned_text)
            self.assertIn("Worktree 专属观察", cleaned_text)
            self.assertTrue(archive_path.exists())
            self.assertTrue(index_status(root).is_current)

    def test_agent_apply_archives_old_non_actionable_day_memory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            memory_dir = root / ".codex" / "memory"
            memory_dir.mkdir(parents=True)
            (memory_dir / "MEMORY.md").write_text("# Memory\n", encoding="utf-8")
            old_day = memory_dir / "2026-01-01.md"
            old_day.write_text(
                "# Project Memory Events - 2026-01-01\n\n"
                "## 事件 1: 临时观察\n"
                "- 类别: observation\n"
                "- 来源: local\n"
                "- 验证命令/证据: none\n"
                "- 影响范围: repo\n"
                "- 是否可晋升: no\n"
                "- 记录:\n"
                "  - 已过期的临时观察。\n",
                encoding="utf-8",
            )

            result = agent_apply_consolidation(
                root,
                now=dt.datetime(2026, 7, 1, 9, 0, 0),
                archive_day_memory_older_than_days=30,
            )

            archived_rel = ".codex/memory/2026-01-01.md"
            archived_path = root / ".codex" / "cache" / "memory-archive" / "20260701-090000" / archived_rel
            self.assertIn(archived_rel, result.archived)
            self.assertFalse(old_day.exists())
            self.assertTrue(archived_path.exists())
            self.assertTrue(index_status(root).is_current)

    def test_report_listing_and_latest_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            memory_dir = root / ".codex" / "memory"
            memory_dir.mkdir(parents=True)
            (memory_dir / "MEMORY.md").write_text("# Memory\n", encoding="utf-8")

            first = generate_consolidation_report(root, now=dt.datetime(2026, 5, 2, 3, 0, 0), write=True)
            second = generate_consolidation_report(root, now=dt.datetime(2026, 5, 2, 4, 0, 0), write=True)

            reports = list_reports(root)

            self.assertEqual(reports[-1].json_path, second.json_path)
            self.assertEqual(latest_report(root).json_path, second.json_path)
            self.assertIn(first.json_path, [report.json_path for report in reports])

    def test_report_falls_back_to_temp_when_project_cache_is_not_writable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            memory_dir = root / ".codex" / "memory"
            memory_dir.mkdir(parents=True)
            (memory_dir / "MEMORY.md").write_text("# Memory\n", encoding="utf-8")
            fallback_base = root / "tmp-fallback"
            original_write_text = Path.write_text

            def blocked_project_report_write(path: Path, data: str, *args: object, **kwargs: object) -> int:
                if ".codex/cache/memory-reports" in path.as_posix():
                    raise PermissionError("blocked by sandbox")
                return original_write_text(path, data, *args, **kwargs)

            with mock.patch.object(Path, "write_text", blocked_project_report_write), mock.patch(
                "project_memory_bootstrap_codex.memory_consolidation.tempfile.gettempdir",
                return_value=str(fallback_base),
            ):
                report = generate_consolidation_report(
                    root,
                    now=dt.datetime(2026, 5, 2, 5, 0, 0),
                    write=True,
                )
                latest_json_path = latest_report(root).json_path

            self.assertTrue(report.markdown_path.exists())
            self.assertTrue(report.json_path.exists())
            self.assertIn("project-memory-bootstrap-codex", report.markdown_path.as_posix())
            self.assertNotIn(".codex/cache/memory-reports", report.markdown_path.as_posix())
            self.assertEqual(latest_json_path, report.json_path)
            shutil.rmtree(fallback_base)

    def test_cli_dry_run_respects_report_dir_override(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            memory_dir = root / ".codex" / "memory"
            memory_dir.mkdir(parents=True)
            (memory_dir / "MEMORY.md").write_text("# Memory\n", encoding="utf-8")
            report_dir = root / "writable-reports"

            output = StringIO()
            with redirect_stdout(output):
                code = main(
                    [
                        "consolidate-memory",
                        str(root),
                        "--dry-run",
                        "--report-dir",
                        str(report_dir),
                    ]
                )

            self.assertEqual(code, 0)
            self.assertIn(str(report_dir), output.getvalue())
            self.assertTrue(any(report_dir.glob("*-consolidation.json")))

    def test_report_dir_override_fails_fast_when_not_writable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            memory_dir = root / ".codex" / "memory"
            memory_dir.mkdir(parents=True)
            (memory_dir / "MEMORY.md").write_text("# Memory\n", encoding="utf-8")
            report_dir = root / "blocked-reports"
            fallback_base = root / "tmp-fallback"
            original_write_text = Path.write_text

            def blocked_override_write(path: Path, data: str, *args: object, **kwargs: object) -> int:
                if report_dir.as_posix() in path.as_posix():
                    raise PermissionError("blocked explicit report dir")
                return original_write_text(path, data, *args, **kwargs)

            with mock.patch.object(Path, "write_text", blocked_override_write), mock.patch(
                "project_memory_bootstrap_codex.memory_consolidation.tempfile.gettempdir",
                return_value=str(fallback_base),
            ):
                with self.assertRaises(PermissionError):
                    generate_consolidation_report(
                        root,
                        now=dt.datetime(2026, 5, 2, 5, 30, 0),
                        write=True,
                        report_dir=report_dir,
                    )

            self.assertFalse(fallback_base.exists())

    def test_cli_dry_run_and_latest_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            memory_dir = root / ".codex" / "memory"
            memory_dir.mkdir(parents=True)
            (memory_dir / "MEMORY.md").write_text("# Memory\n", encoding="utf-8")

            dry_run_output = StringIO()
            with redirect_stdout(dry_run_output):
                code = main(["consolidate-memory", str(root), "--dry-run"])
            self.assertEqual(code, 0)
            self.assertIn("markdown:", dry_run_output.getvalue())

            report_output = StringIO()
            with redirect_stdout(report_output):
                code = main(["consolidation-report", str(root), "--latest"])
            self.assertEqual(code, 0)
            self.assertIn("# Memory Consolidation Report", report_output.getvalue())

    def test_cli_agent_apply_generates_report_and_archives(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            memory_dir = root / ".codex" / "memory"
            memory_dir.mkdir(parents=True)
            (memory_dir / "MEMORY.md").write_text("# Memory\n", encoding="utf-8")
            legacy = root / ".workbuddy" / "memory" / "MEMORY.md"
            worktree_copy = root / ".worktrees" / "feature-a" / ".workbuddy" / "memory" / "MEMORY.md"
            duplicate_text = (
                "# Legacy Memory\n\n"
                "### 运行入口\n"
                "- 类型: durable_fact\n"
                "- 最近核验: 2026-05-01\n"
                "- 内容:\n"
                "  - CLI 入口是 project-memory-bootstrap-codex。\n"
            )
            legacy.parent.mkdir(parents=True)
            worktree_copy.parent.mkdir(parents=True)
            legacy.write_text(duplicate_text, encoding="utf-8")
            worktree_copy.write_text(duplicate_text, encoding="utf-8")

            output = StringIO()
            with redirect_stdout(output):
                code = main(["consolidate-memory", str(root), "--agent-apply"])

            self.assertEqual(code, 0)
            self.assertIn("agent report:", output.getvalue())
            self.assertIn("archived .worktrees/feature-a/.workbuddy/memory/MEMORY.md", output.getvalue())
            self.assertTrue(legacy.exists())
            self.assertFalse(worktree_copy.exists())


if __name__ == "__main__":
    unittest.main()
