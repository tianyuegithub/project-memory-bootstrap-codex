from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from project_memory_bootstrap_codex.memory_index import (
    context_from_ids,
    context_from_results,
    discover_memory_files,
    get_memory_by_id,
    index_status,
    rebuild_index,
    search_memory,
)


class MemoryIndexTests(unittest.TestCase):
    def test_index_discovers_managed_memory_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            (root / "CODEX.md").write_text("# CODEX\n\n## 运行入口与端口\n\nCLI only.\n", encoding="utf-8")
            memory_dir = root / ".codex" / "memory"
            memory_dir.mkdir(parents=True)
            (memory_dir / "MEMORY.md").write_text(
                "# Long Memory\n\n### 数据治理入口\n- 类型: durable_fact\n- 内容:\n  - 使用 SQLite 索引。\n",
                encoding="utf-8",
            )
            (memory_dir / "2026-04-30.md").write_text(
                "# Day\n\n## 事件 1: 索引验证\n- 类别: verification\n",
                encoding="utf-8",
            )
            docs = root / "docs"
            docs.mkdir()
            (docs / "random.md").write_text("not indexed by default\n", encoding="utf-8")

            files = [path.relative_to(root).as_posix() for path in discover_memory_files(root)]

            self.assertIn("CODEX.md", files)
            self.assertIn(".codex/memory/MEMORY.md", files)
            self.assertIn(".codex/memory/2026-04-30.md", files)
            self.assertNotIn("docs/random.md", files)

    def test_search_memory_returns_lightweight_matches(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            (root / "CODEX.md").write_text("# CODEX\n\n## 项目定位\n\n记忆索引工具。\n", encoding="utf-8")
            memory_dir = root / ".codex" / "memory"
            memory_dir.mkdir(parents=True)
            (memory_dir / "MEMORY.md").write_text(
                "# Long Memory\n\n### SQLite 索引层\n- 类型: durable_fact\n- 内容:\n  - 搜索先返回 ID 和标题，不读取全文。\n",
                encoding="utf-8",
            )

            index = rebuild_index(root)
            results = search_memory(root, "SQLite 索引", limit=5)

            self.assertGreaterEqual(index.chunks, 2)
            self.assertTrue(results)
            self.assertEqual(results[0].kind, "long_memory")
            self.assertIn("SQLite", results[0].title)

    def test_context_respects_token_budget(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            memory_dir = root / ".codex" / "memory"
            memory_dir.mkdir(parents=True)
            (memory_dir / "MEMORY.md").write_text(
                "# Long Memory\n\n### 慢读取问题\n"
                + "\n".join(f"- 内容: 需要减少 token 消耗 {i}" for i in range(200)),
                encoding="utf-8",
            )

            rebuild_index(root)
            results = search_memory(root, "token 消耗", limit=5)
            context = context_from_results(results, max_tokens=160)

            self.assertIn("# Memory Context", context)
            self.assertIn("Approx read tokens", context)
            self.assertLess(len(context), 1200)

    def test_get_memory_by_id_returns_exact_chunk(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            memory_dir = root / ".codex" / "memory"
            memory_dir.mkdir(parents=True)
            (memory_dir / "MEMORY.md").write_text(
                "# Long Memory\n\n"
                "### SQLite 索引层\n索引命中后可以按 ID 精确取回。\n\n"
                "### 其它条目\n这个条目不应被取回。\n",
                encoding="utf-8",
            )

            rebuild_index(root)
            result = search_memory(root, "精确取回", limit=1)[0]
            fetched = get_memory_by_id(root, result.id)

            self.assertIsNotNone(fetched)
            assert fetched is not None
            self.assertEqual(fetched.id, result.id)
            self.assertEqual(fetched.title, "SQLite 索引层")
            self.assertIn("按 ID 精确取回", fetched.text)
            self.assertNotIn("不应被取回", fetched.text)

    def test_index_status_reports_stale_when_source_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            memory_dir = root / ".codex" / "memory"
            memory_dir.mkdir(parents=True)
            source = memory_dir / "MEMORY.md"
            source.write_text("# Long Memory\n\n### 索引状态\n初始内容。\n", encoding="utf-8")

            rebuild_index(root)
            fresh = index_status(root)
            self.assertFalse(fresh.missing_db)
            self.assertTrue(fresh.is_current)
            self.assertEqual(fresh.stale_files, ())

            source.write_text("# Long Memory\n\n### 索引状态\n内容已经变更。\n", encoding="utf-8")
            stale = index_status(root)

            self.assertFalse(stale.is_current)
            self.assertIn(".codex/memory/MEMORY.md", stale.stale_files)

    def test_context_from_ids_respects_token_budget(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            memory_dir = root / ".codex" / "memory"
            memory_dir.mkdir(parents=True)
            (memory_dir / "MEMORY.md").write_text(
                "# Long Memory\n\n### 按 ID 上下文\n"
                + "\n".join(f"- 内容: 按 ID 生成上下文并控制预算 {i}" for i in range(200)),
                encoding="utf-8",
            )

            rebuild_index(root)
            result = search_memory(root, "按 ID 上下文", limit=1)[0]
            context = context_from_ids(root, [result.id], max_tokens=140)

            self.assertIn(result.id, context)
            self.assertIn("Approx read tokens", context)
            self.assertLess(len(context), 1100)


if __name__ == "__main__":
    unittest.main()
