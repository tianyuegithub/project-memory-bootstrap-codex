from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class DailyAutomationTemplateTests(unittest.TestCase):
    def test_prompt_is_lightweight_status_check_without_daily_consolidation(self) -> None:
        template = (ROOT / "templates" / "codex-automation-daily-memory-consolidation.toml").read_text(
            encoding="utf-8"
        )
        prompt_match = re.search(r'^prompt = "(.*)"$', template, re.MULTILINE)

        self.assertIsNotNone(prompt_match)
        assert prompt_match is not None
        prompt = prompt_match.group(1)

        self.assertIn("project-memory-bootstrap-codex memory-status . --no-legacy", prompt)
        self.assertIn("project-memory-bootstrap-codex index-memory . --no-legacy", prompt)
        self.assertIn("Do not run `consolidate-memory`", prompt)
        self.assertIn("do not scan `.runtime/`, `.worktrees/`, or `.codex/cache/memory-archive/`", prompt)
        self.assertNotIn("--agent-apply", prompt)
        self.assertNotIn("archive writes", prompt)

    def test_template_uses_low_reasoning_for_daily_check(self) -> None:
        template = (ROOT / "templates" / "codex-automation-daily-memory-consolidation.toml").read_text(
            encoding="utf-8"
        )

        self.assertIn('reasoning_effort = "low"', template)
        self.assertNotIn('reasoning_effort = "high"', template)


if __name__ == "__main__":
    unittest.main()
