from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from project_memory_bootstrap_codex import maintenance, memory_state
from project_memory_bootstrap_codex.bootstrap import init_project
from project_memory_bootstrap_codex.errors import MemoryProtocolError
from project_memory_bootstrap_codex.hooks import handle_hook
from project_memory_bootstrap_codex.project import register_project


class MaintenanceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        env = patch.dict(os.environ, {"PROJECT_MEMORY_REGISTRY": str(self.root / "registry.json")})
        env.start()
        self.addCleanup(env.stop)
        init_project(self.root)
        self.context = register_project(self.root, "maintenance-test", {"root": "."}, maintenance_enabled=True)
        memory_state.initialize_state(self.context, new_registration=True)

    def test_unfinished_attempt_survives_and_failure_does_not_clear_work(self):
        candidate = memory_state.capture(self.root, {"source_instance": "fixture", "object_id": "one", "source_revision": "1",
            "kind": "observation", "locator": "fixture://one", "observed_at": "2026-11-02T00:00:00Z"})
        receipt = maintenance.begin(self.root, "codex", "semantic-session")
        self.assertEqual(maintenance.runs(self.root)[0]["status"], "running")
        failed = maintenance.finish(self.root, receipt["run_id"], {"status": "failed", "error_code": "PROVIDER_QUOTA",
            "coverage": "No semantic candidate completed.", "operation_ids": []})
        self.assertEqual(failed["pending_candidates"], 1)
        self.assertEqual(memory_state.pending(self.root)[0]["candidate_id"], candidate["candidate_id"])

    def test_finish_requires_real_operations_and_is_idempotent(self):
        receipt = maintenance.begin(self.root, "claude", "semantic-session")
        payload = {"status": "succeeded", "coverage": "No new candidate in selected scope.", "operation_ids": ["invented"]}
        with self.assertRaises(MemoryProtocolError) as error:
            maintenance.finish(self.root, receipt["run_id"], payload)
        self.assertEqual(error.exception.code, "RUN_OPERATION_UNCOMMITTED")
        payload["operation_ids"] = []
        actual = maintenance.finish(self.root, receipt["run_id"], payload)
        self.assertEqual(actual, maintenance.finish(self.root, receipt["run_id"], payload))

    def test_dedicated_semantic_session_is_not_recaptured_by_stop_hook(self):
        maintenance.begin(self.root, "claude", "semantic-session")
        self.assertTrue(maintenance.is_internal_session(self.context, "claude", "semantic-session"))
        self.assertFalse(maintenance.is_internal_session(self.context, "zcode", "semantic-session"))
        self.assertEqual(handle_hook("claude", {"cwd": str(self.root), "session_id": "semantic-session", "hook_event_name": "Stop"}), {})
        self.assertEqual(memory_state.pending(self.root), [])


if __name__ == "__main__":
    unittest.main()
