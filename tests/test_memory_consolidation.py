from __future__ import annotations

import datetime as dt
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from project_memory_bootstrap_codex.cli import main
from project_memory_bootstrap_codex.memory_consolidation import (
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


if __name__ == "__main__":
    unittest.main()
