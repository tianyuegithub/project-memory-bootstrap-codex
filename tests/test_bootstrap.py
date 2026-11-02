from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from project_memory_bootstrap_codex.bootstrap import doctor, find_project_root, init_project, scan_shell_scripts
from project_memory_bootstrap_codex import templates


class BootstrapTests(unittest.TestCase):
    def setUp(self) -> None:
        home = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        patcher = patch.dict(os.environ, {"HOME": home.name})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_init_project_creates_required_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            results = init_project(root, today="2026-04-26", with_agents=True)

            statuses = {result.path.name: result.status for result in results}
            self.assertEqual(statuses["README.md"], "written")
            self.assertEqual(statuses["MEMORY.md"], "written")
            self.assertEqual(statuses["2026-04-26.md"], "written")
            self.assertEqual(statuses["AGENTS.md"], "written")

            self.assertTrue((root / "README.md").exists())
            self.assertTrue((root / ".codex" / "memory" / "MEMORY.md").exists())
            self.assertTrue((root / ".codex" / "memory" / "2026-04-26.md").exists())
            agents_text = (root / "AGENTS.md").read_text(encoding="utf-8")
            readme_text = (root / "README.md").read_text(encoding="utf-8")
            self.assertIn("search-memory", agents_text)
            self.assertIn("待核验", readme_text)
            self.assertFalse((root / "CODEX.md").exists())
            self.assertNotIn("每次进入本项目", agents_text)

            check = doctor(root)
            self.assertTrue(check.ok, "\n".join(check.messages))

    def test_init_project_does_not_overwrite_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            codex = root / "README.md"
            codex.write_text("custom\n", encoding="utf-8")

            init_project(root, today="2026-04-26")

            self.assertEqual(codex.read_text(encoding="utf-8"), "custom\n")

    def test_init_project_rejects_symlink_escape_when_forcing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "repo"
            root.mkdir()
            external = base / "outside.md"
            external.write_text("do not overwrite\n", encoding="utf-8")
            (root / "README.md").symlink_to(external)

            with self.assertRaises(ValueError):
                init_project(root, today="2026-04-26", force=True)

            self.assertEqual(external.read_text(encoding="utf-8"), "do not overwrite\n")

    def test_init_project_rejects_parent_symlink_escape_before_mkdir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "repo"
            root.mkdir()
            outside = base / "outside"
            outside.mkdir()
            (root / ".codex").symlink_to(outside, target_is_directory=True)

            with self.assertRaises(ValueError):
                init_project(root, today="2026-04-26", force=True)

            self.assertFalse((outside / "memory").exists())

    def test_scan_shell_scripts_in_expected_locations(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            scripts = root / "scripts"
            scripts.mkdir()
            expected = scripts / "start.sh"
            expected.write_text("#!/usr/bin/env bash\n", encoding="utf-8")
            ignored = root / "misc"
            ignored.mkdir()
            (ignored / "ignored.sh").write_text("#!/usr/bin/env bash\n", encoding="utf-8")

            found = scan_shell_scripts(root)

            self.assertIn(expected.resolve(), found)
            self.assertNotIn((ignored / "ignored.sh").resolve(), found)

    def test_doctor_accepts_chinese_metadata_colons(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            init_project(root, today="2026-04-26")
            memory = root / ".codex" / "memory" / "MEMORY.md"
            memory.write_text(
                "# Long Memory\n\n"
                "## 条目 1\n"
                "- 类型：stable_fact\n"
                "- 范围：repo\n"
                "- 来源：test\n"
                "- 最近核验：2026-06-11\n"
                "- 稳定性：高\n"
                "- 失效条件：规则变化\n"
                "- 替代关系：无\n",
                encoding="utf-8",
            )

            check = doctor(root)

            self.assertTrue(check.ok, "\n".join(check.messages))
            self.assertTrue(any(message.startswith("legacy:") for message in check.messages))
            self.assertTrue(any("age alone does not invalidate" in message for message in check.messages))

    def test_doctor_fails_when_codex_cache_is_git_tracked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            init_project(root, today="2026-06-11")
            subprocess.run(["git", "init"], cwd=root, check=True, stdout=subprocess.DEVNULL)
            tracked_cache = root / ".codex" / "cache" / "memory-archive" / "MANIFEST.md"
            tracked_cache.parent.mkdir(parents=True)
            tracked_cache.write_text("# archived\n", encoding="utf-8")
            subprocess.run(["git", "add", ".codex/cache/memory-archive/MANIFEST.md"], cwd=root, check=True)

            check = doctor(root)

            self.assertFalse(check.ok)
            self.assertTrue(any(message.startswith("tracked-cache:") for message in check.messages))

    def test_doctor_ignores_unrelated_global_dynamic_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            root.mkdir()
            init_project(root, today="2026-06-11")
            home = Path(tmp) / "home"
            global_codex = home / ".codex"
            global_codex.mkdir(parents=True)
            (global_codex / "AGENTS.md").write_text("<claude-mem-context>\n$CMEM\n</claude-mem-context>\n", encoding="utf-8")

            with patch("project_memory_bootstrap_codex.bootstrap.Path.home", return_value=home):
                check = doctor(root)

            self.assertTrue(check.ok, "\n".join(check.messages))
            self.assertFalse(any(message.startswith("dynamic-context:") for message in check.messages))


    def test_default_init_has_no_codex_or_date_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            results = init_project(root)
            self.assertEqual({item.path.relative_to(root.resolve()).as_posix() for item in results}, {
                "README.md", "AGENTS.md", ".codex/memory/MEMORY.md",
            })
            self.assertEqual(list((root / ".codex/memory").glob("20??-??-??.md")), [])
            self.assertTrue(doctor(root).ok)

    def test_explicitly_omitted_agents_is_reported_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            init_project(root, with_agents=False)
            check = doctor(root)
            self.assertFalse(check.ok)
            self.assertIn("missing: AGENTS.md", check.messages)

    def test_init_preserves_all_existing_managed_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            init_project(root, today="2026-04-26")
            paths = [root / "README.md", root / "AGENTS.md", root / ".codex/memory/MEMORY.md", root / ".codex/memory/2026-04-26.md"]
            for path in paths:
                path.write_text("user content\n", encoding="utf-8")
            self.assertTrue(all(item.status == "skipped" for item in init_project(root, today="2026-04-26")))
            self.assertTrue(all(path.read_text(encoding="utf-8") == "user content\n" for path in paths))

    def test_find_root_uses_resolved_project_context(self) -> None:
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch("project_memory_bootstrap_codex.bootstrap.resolve_context", return_value=SimpleNamespace(root=root)) as resolve:
                self.assertEqual(find_project_root(root / "member"), root)
                resolve.assert_called_once_with(root / "member")

    def test_find_root_retains_nearest_git_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            nested = root / "child"
            nested.mkdir()
            self.assertEqual(find_project_root(nested), root.resolve())

    def test_doctor_rejects_tracked_state(self) -> None:
        for directory in (".state", ".codex/memory-state"):
            with self.subTest(directory=directory), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                init_project(root)
                subprocess.run(["git", "init", "-q"], cwd=root, check=True)
                target = root / directory / "local-state"
                target.parent.mkdir(parents=True)
                target.write_text("state", encoding="utf-8")
                subprocess.run(["git", "add", str(target.relative_to(root))], cwd=root, check=True)
                self.assertFalse(doctor(root).ok)

    def test_doctor_validates_each_managed_fact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            init_project(root)
            blocks = []
            for index in range(2):
                metadata = {"schema_version": 1, "project_id": "test-project", "fact_id": f"F-{index}", "title": "测试要求", "revision": 1, "status": "active", "kind": "requirement", "scope": ["repo:root"], "sources": [{"source_id": "test:rule", "version": "v1", "locator": "spec.md"}], "verified_at": "2026-11-02", "verification": "测试协议格式", "expires_when": "正式要求变化时"}
                blocks.append("<!-- project-memory-fact " + json.dumps(metadata, ensure_ascii=False) + f" -->\n## F-{index}：测试要求\n\n测试断言。\n<!-- /project-memory-fact -->")
            memory = root / ".codex/memory/MEMORY.md"
            memory.write_text("\n\n".join(blocks), encoding="utf-8")
            check = doctor(root)
            self.assertTrue(check.ok, "\n".join(check.messages))
            self.assertIn("ok: MEMORY.md validated 2 managed fact(s)", check.messages)
            memory.write_text(blocks[0] + "\n<!-- project-memory-fact {} -->\ninvalid\n<!-- /project-memory-fact -->", encoding="utf-8")
            check = doctor(root)
            self.assertFalse(check.ok)
            self.assertTrue(any(message.startswith("invalid-facts:") for message in check.messages))

    def test_distribution_templates_match_executable_owner(self) -> None:
        root = Path(__file__).resolve().parents[1]
        for relative, content in {
            "AGENTS.md": templates.AGENTS_TEMPLATE,
            "CODEX.md": templates.CODEX_TEMPLATE,
            ".codex/memory/MEMORY.md": templates.MEMORY_TEMPLATE,
            ".codex/memory/YYYY-MM-DD.md": templates.DAY_TEMPLATE,
        }.items():
            with self.subTest(relative=relative):
                self.assertEqual((root / "templates" / relative).read_text(encoding="utf-8"), content)


if __name__ == "__main__":
    unittest.main()
