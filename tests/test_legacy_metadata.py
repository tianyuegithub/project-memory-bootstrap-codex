from __future__ import annotations

import datetime as dt
import tempfile
import unittest
from pathlib import Path

from project_memory_bootstrap_codex.memory_consolidation import _conflict_items, _expired_day_memory_archive_paths, _promote_items, _stale_items
from project_memory_bootstrap_codex.memory_index import load_chunks


class LegacyMetadataTests(unittest.TestCase):
    def test_chinese_metadata_is_detected_and_protects_unresolved_history(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            memory = root / ".codex/memory"
            memory.mkdir(parents=True)
            (memory / "MEMORY.md").write_text("# Memory\n\n## F-old：历史事实\n- 最近核验：2026-01-01\n")
            (memory / "2026-01-01.md").write_text("# Events\n\n## 可晋升线索\n- 是否可晋升：是（仍需人工核验）\n")
            (memory / "2026-01-02.md").write_text("# Events\n\n## 证据冲突\n- 类别：conflict（冲突）\n")
            chunks = load_chunks(root, include_legacy=False)
            self.assertEqual(len(_stale_items(chunks, dt.date(2026, 11, 2))), 1)
            self.assertEqual(len(_promote_items(chunks)), 1)
            self.assertEqual(len(_conflict_items(chunks)), 1)
            self.assertEqual(_expired_day_memory_archive_paths(root, dt.date(2026, 11, 2), 180), [])


if __name__ == "__main__":
    unittest.main()
