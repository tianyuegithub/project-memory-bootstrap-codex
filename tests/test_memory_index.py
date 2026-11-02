from __future__ import annotations

import json
import os
from dataclasses import asdict
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import sqlite3

from project_memory_bootstrap_codex.cli import build_parser
from project_memory_bootstrap_codex.bootstrap import init_project
from project_memory_bootstrap_codex import memory_state as state
from project_memory_bootstrap_codex.project import register_project

from project_memory_bootstrap_codex.memory_index import (
    context_from_ids,
    context_from_results,
    discover_memory_files,
    get_memory_by_id,
    index_status,
    load_chunks,
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

    def test_index_ignores_codex_cache_archives(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            memory_dir = root / ".codex" / "memory"
            memory_dir.mkdir(parents=True)
            (memory_dir / "MEMORY.md").write_text("# Long Memory\n\n### Active\nactive.\n", encoding="utf-8")
            archive_memory = (
                root
                / ".codex"
                / "cache"
                / "memory-archive"
                / "20260503-033235"
                / ".worktrees"
                / "feature-a"
                / ".workbuddy"
                / "memory"
                / "MEMORY.md"
            )
            archive_memory.parent.mkdir(parents=True)
            archive_memory.write_text("# Archived\n\n### Old\narchived.\n", encoding="utf-8")

            files = [path.relative_to(root).as_posix() for path in discover_memory_files(root)]

            self.assertIn(".codex/memory/MEMORY.md", files)
            self.assertNotIn(
                ".codex/cache/memory-archive/20260503-033235/.worktrees/feature-a/.workbuddy/memory/MEMORY.md",
                files,
            )

    def test_index_ignores_runtime_and_worktree_memory_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            memory_dir = root / ".codex" / "memory"
            memory_dir.mkdir(parents=True)
            (memory_dir / "MEMORY.md").write_text("# Long Memory\n\n### Active\nactive.\n", encoding="utf-8")
            runtime_memory = root / ".runtime" / "agent-a" / ".codex" / "memory" / "MEMORY.md"
            runtime_memory.parent.mkdir(parents=True)
            runtime_memory.write_text("# Runtime\n\n### Old\nruntime.\n", encoding="utf-8")
            worktree_memory = root / ".worktrees" / "feature-a" / ".codex" / "memory" / "MEMORY.md"
            worktree_memory.parent.mkdir(parents=True)
            worktree_memory.write_text("# Worktree\n\n### Old\nworktree.\n", encoding="utf-8")

            files = [path.relative_to(root).as_posix() for path in discover_memory_files(root)]

            self.assertIn(".codex/memory/MEMORY.md", files)
            self.assertNotIn(".runtime/agent-a/.codex/memory/MEMORY.md", files)
            self.assertNotIn(".worktrees/feature-a/.codex/memory/MEMORY.md", files)

    def test_no_legacy_reads_exclude_legacy_chunks_left_in_current_index(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            memory_dir = root / ".codex" / "memory"
            memory_dir.mkdir(parents=True)
            (memory_dir / "MEMORY.md").write_text("# Long Memory\n\n### Active\nactive.\n", encoding="utf-8")
            legacy_memory = root / "legacy" / "memory" / "MEMORY.md"
            legacy_memory.parent.mkdir(parents=True)
            legacy_memory.write_text("# Legacy\n\n### Old\nlegacy.\n", encoding="utf-8")
            (root / "CLAUDE.md").write_text("# Claude\n\n## Old\nlegacy agent instructions.\n", encoding="utf-8")
            (root / "CURSOR.md").write_text("# Cursor\n\n## Old\nlegacy cursor instructions.\n", encoding="utf-8")

            rebuild_index(root, include_legacy=True)
            old_ids = [result.id for result in search_memory(root, "legacy", include_legacy=True)]
            self.assertTrue(old_ids)

            legacy_memory.unlink()
            (root / "CLAUDE.md").unlink()
            (root / "CURSOR.md").unlink()
            status = index_status(root, include_legacy=False)

            self.assertTrue(status.is_current)
            self.assertEqual(status.stale_files, ())
            self.assertEqual(search_memory(root, "legacy"), [])
            for chunk_id in old_ids:
                self.assertIsNone(get_memory_by_id(root, chunk_id))
            self.assertEqual(context_from_ids(root, old_ids), "No indexed memory matched the requested IDs.")
            self.assertEqual(search_memory(root, "legacy", include_legacy=True), [])

    def test_legacy_reads_require_explicit_scope_even_after_legacy_indexing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            memory_dir = root / ".codex" / "memory"
            memory_dir.mkdir(parents=True)
            (memory_dir / "MEMORY.md").write_text("# Current\nCurrent fact.\n", encoding="utf-8")
            legacy = root / "legacy" / "memory" / "MEMORY.md"
            legacy.parent.mkdir(parents=True)
            legacy.write_text("# Historical-marker\nHistorical-marker fact.\n", encoding="utf-8")

            self.assertEqual(search_memory(root, "Historical-marker"), [])
            results = search_memory(root, "Historical-marker", include_legacy=True)
            self.assertTrue(results)
            self.assertIsNotNone(get_memory_by_id(root, results[0].id, include_legacy=True))
            self.assertEqual(search_memory(root, "Historical-marker"), [])
            self.assertIsNone(get_memory_by_id(root, results[0].id))

    def test_read_commands_share_scope_defaults(self) -> None:
        parser = build_parser()
        commands = [
            ["index-memory", "."],
            ["memory-status", "."],
            ["search-memory", ".", "query"],
            ["get-memory", ".", "chunk-id"],
            ["context-memory", ".", "--id", "chunk-id"],
        ]
        for command in commands:
            with self.subTest(command=command[0]):
                self.assertFalse(parser.parse_args(command).include_legacy)
                self.assertFalse(parser.parse_args(command + ["--no-legacy"]).include_legacy)
                self.assertTrue(parser.parse_args(command + ["--include-legacy"]).include_legacy)

    def test_reads_refresh_changed_and_deleted_current_sources(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            source = root / ".codex" / "memory" / "MEMORY.md"
            source.parent.mkdir(parents=True)
            source.write_text("# Old-observation\nOld-observation.\n", encoding="utf-8")
            old = search_memory(root, "Old-observation")[0]
            source.write_text("# Corrected-observation\nCorrected-observation.\n", encoding="utf-8")

            self.assertIsNone(get_memory_by_id(root, old.id))
            self.assertEqual(search_memory(root, "Old-observation"), [])
            current = search_memory(root, "Corrected-observation")[0]
            source.unlink()
            self.assertEqual(context_from_ids(root, [current.id]), "No indexed memory matched the requested IDs.")
            self.assertEqual(search_memory(root, "Corrected-observation"), [])

    def test_failed_rebuild_preserves_previous_index(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            source = root / ".codex" / "memory" / "MEMORY.md"
            source.parent.mkdir(parents=True)
            source.write_text("# Published\nPublished fact.\n", encoding="utf-8")
            rebuild_index(root, include_legacy=False)
            chunk = load_chunks(root, include_legacy=False)[0]
            with patch("project_memory_bootstrap_codex.memory_index.load_chunks", return_value=[chunk, chunk]):
                with self.assertRaises(sqlite3.IntegrityError):
                    rebuild_index(root, include_legacy=False)
            self.assertEqual(get_memory_by_id(root, chunk.id).text, chunk.text)

    def test_no_legacy_discovery_excludes_legacy_agent_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            (root / "CODEX.md").write_text("# CODEX\n", encoding="utf-8")
            (root / "README.md").write_text("# README\n", encoding="utf-8")
            (root / "AGENTS.md").write_text("# AGENTS\n", encoding="utf-8")
            (root / "CLAUDE.md").write_text("# CLAUDE\n", encoding="utf-8")
            (root / "CURSOR.md").write_text("# CURSOR\n", encoding="utf-8")
            memory_dir = root / ".codex" / "memory"
            memory_dir.mkdir(parents=True)
            (memory_dir / "MEMORY.md").write_text("# Long Memory\n", encoding="utf-8")

            files = [path.relative_to(root).as_posix() for path in discover_memory_files(root, include_legacy=False)]

            self.assertNotIn("CODEX.md", files)
            self.assertIn("README.md", files)
            self.assertIn("AGENTS.md", files)
            self.assertIn(".codex/memory/MEMORY.md", files)
            self.assertNotIn("CLAUDE.md", files)
            self.assertNotIn("CURSOR.md", files)

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


class GovernedReadableMemoryTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        registry = self.root / "registry.json"
        environment = patch.dict(os.environ, {"PROJECT_MEMORY_REGISTRY": str(registry)})
        environment.start()
        self.addCleanup(environment.stop)
        init_project(self.root)
        context = register_project(self.root, "readable-test", {"root": "."}, maintenance_enabled=True, registry_path=registry)
        state.initialize_state(context, new_registration=True)
        self.version = "opaquehashdeadbeef" * 1200
        self.locator = "specs/uniquelocator/contract.md"
        self.body = "完整规则正文。\n\n条件：仅在明确授权范围适用；来源变化时重新核验。"

    def publish(self, body: str | None = None) -> None:
        captured = state.capture(self.root, {
            "source_instance": "fixture", "object_id": "rule", "source_revision": self.version,
            "locator": self.locator, "kind": "observation", "observed_at": "2026-11-02T01:00:00+00:00",
            "excerpt": "Controlled fixture evidence.",
        })
        change = {
            "fact_id": "F-readable", "expected_version": None, "title": "可读规则", "body": body if body is not None else self.body,
            "kind": "decision", "scope": ["repo:root"], "status": "active",
            "sources": [{"source_id": captured["source_id"], "version": self.version, "locator": self.locator}],
            "verified_at": "2026-11-02", "verification": "核验隔离测试正文，不代表实际环境验收。",
            "expires_when": "正式要求被替代或适用范围改变。", "supersedes": ["F-prior"],
        }
        proposal = state.propose(self.root, {
            "candidate_id": captured["candidate_id"], "verified_by": "fixture-task",
            "verification_scope": "Only the isolated fixture.",
            "changes": [dict(change, fact_id="F-prior", status="superseded", supersedes=[], body="Old fixture rule."), change],
        })
        state.commit(self.root, proposal["proposal_id"])

    def test_large_source_metadata_does_not_displace_complete_readable_fact(self) -> None:
        self.publish()
        fetched = get_memory_by_id(self.root, "F-readable")
        self.assertIsNotNone(fetched)
        self.assertIn(self.body, fetched.text)
        for text in (fetched.text, json.dumps(asdict(fetched), ensure_ascii=False)):
            self.assertNotIn("<!-- project-memory-fact", text)
            self.assertNotIn(self.version, text)
            self.assertNotIn("source_id", text)
            for value in ("F-readable", "可读规则", "active", "revision: 1", "repo:root", "2026-11-02", "F-prior", "正式要求被替代或适用范围改变。", "核验隔离测试正文", "证据条数: 1", "完整来源/版本用inspect-memory或memory_inspect"):
                self.assertIn(value, text)
        context = context_from_ids(self.root, ["F-readable"], max_tokens=200)
        self.assertIn(self.body, context)
        self.assertIn("正式要求被替代或适用范围改变。", context)
        self.assertNotIn("完整条目超出", context)
        self.assertLess(fetched.tokens, 200)
        with sqlite3.connect(self.root / ".codex/cache/memory-index.sqlite") as connection:
            raw, stored_tokens = connection.execute("SELECT text,tokens FROM memory_chunks WHERE id='F-readable'").fetchone()
        self.assertIn(self.version, raw)
        self.assertIn("<!-- project-memory-fact", raw)
        self.assertGreater(stored_tokens, 200)
        self.assertEqual(state.inspect(self.root, "F-readable")["metadata"]["sources"][0]["version"], self.version)

    def test_fact_scoring_keeps_identity_and_locator_without_hash_or_json_keys(self) -> None:
        self.publish()
        for query in ("F-readable", "uniquelocator", "正式要求被替代", "F-prior"):
            with self.subTest(query=query):
                self.assertEqual(search_memory(self.root, query)[0].id, "F-readable")
        for query in ("opaquehashdeadbeef", "source_id", "schema_version", "fact:active", ".codex/memory/MEMORY.md"):
            with self.subTest(query=query):
                self.assertEqual(search_memory(self.root, query), [])

    def test_large_body_still_returns_locator_without_cutting_conditions(self) -> None:
        body = "BODY_START " + "完整条件说明。" * 400 + " END_CONDITION: only for this fixture."
        self.publish(body)
        context = context_from_ids(self.root, ["F-readable"], max_tokens=120)
        self.assertIn("F-readable", context)
        self.assertIn("完整条目超出本次预算", context)
        self.assertNotIn("BODY_START", context)
        self.assertNotIn("END_CONDITION", context)
        self.assertIn(body, get_memory_by_id(self.root, "F-readable").text)


if __name__ == "__main__":
    unittest.main()
