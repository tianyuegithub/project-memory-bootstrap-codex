from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from project_memory_bootstrap_codex.bootstrap import doctor, init_project, scan_shell_scripts


class BootstrapTests(unittest.TestCase):
    def test_init_project_creates_required_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            results = init_project(root, today="2026-04-26", with_agents=True)

            statuses = {result.path.name: result.status for result in results}
            self.assertEqual(statuses["CODEX.md"], "written")
            self.assertEqual(statuses["MEMORY.md"], "written")
            self.assertEqual(statuses["2026-04-26.md"], "written")
            self.assertEqual(statuses["AGENTS.md"], "written")

            self.assertTrue((root / "CODEX.md").exists())
            self.assertTrue((root / ".codex" / "memory" / "MEMORY.md").exists())
            self.assertTrue((root / ".codex" / "memory" / "2026-04-26.md").exists())

            check = doctor(root)
            self.assertTrue(check.ok, "\n".join(check.messages))

    def test_init_project_does_not_overwrite_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            codex = root / "CODEX.md"
            codex.write_text("custom\n", encoding="utf-8")

            init_project(root, today="2026-04-26")

            self.assertEqual(codex.read_text(encoding="utf-8"), "custom\n")

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


if __name__ == "__main__":
    unittest.main()
