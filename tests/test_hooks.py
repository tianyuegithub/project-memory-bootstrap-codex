from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from project_memory_bootstrap_codex.bootstrap import init_project
from project_memory_bootstrap_codex.cli import main
from project_memory_bootstrap_codex.hooks import handle_hook, hook_status
from project_memory_bootstrap_codex.memory_state import candidate, health, initialize_state, pending
from project_memory_bootstrap_codex.project import register_project


class HookTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        environment = patch.dict(os.environ, {"PROJECT_MEMORY_REGISTRY": str(self.root / "registry.json"),
                                              "PROJECT_MEMORY_INTERNAL": "0"})
        environment.start()
        self.addCleanup(environment.stop)
        init_project(self.root)
        context = register_project(self.root, "hook-fixture", {"root": "."}, maintenance_enabled=True)
        initialize_state(context, new_registration=True)
        self.payload = {"cwd": str(self.root), "session_id": "fixture-session", "hook_event_name": "SessionStart"}

    def test_start_injects_identity_and_same_protocol_without_pending_work(self):
        result = handle_hook("claude", self.payload)
        text = result["hookSpecificOutput"]["additionalContext"]
        self.assertIn('"project_id": "hook-fixture"', text)
        self.assertIn("propose-memory", text)
        self.assertIn(" -I -m project_memory_bootstrap_codex.cli", text)
        self.assertEqual(pending(self.root), [])
        self.assertEqual(hook_status(self.root)["events"][0]["host_acceptance"], "unverified")

    def test_stop_deduplicates_native_message_and_retains_no_reply_text(self):
        payload = dict(self.payload, hook_event_name="Stop", message_id="m-1", last_assistant_message="Private prose not retained.")
        self.assertEqual(handle_hook("opencode", payload), {})
        self.assertEqual(handle_hook("opencode", payload), {})
        work = pending(self.root)
        self.assertEqual(len(work), 1)
        source = candidate(self.root, work[0]["candidate_id"])
        serialized = json.dumps(source)
        self.assertNotIn("Private prose", serialized)
        self.assertIn("response_sha256", serialized)
        self.assertIn("m-1", serialized)

    def test_unregistered_and_internal_sessions_do_not_record(self):
        with tempfile.TemporaryDirectory() as outside:
            self.assertEqual(handle_hook("codex", dict(self.payload, cwd=outside)), {})
        with patch.dict(os.environ, {"PROJECT_MEMORY_INTERNAL": "1"}):
            self.assertEqual(handle_hook("codex", self.payload), {})
        self.assertEqual(hook_status(self.root)["events"], [])

    def test_missing_state_is_visible_but_hook_cli_does_not_block(self):
        # A deleted suppression/publication ledger must never be reinitialized by a hook.
        ledger = self.root / ".codex/memory-state/state.sqlite"
        if not ledger.exists():
            ledger = next((self.root / ".codex/memory-state").glob("*.sqlite"))
        ledger.unlink()
        out = io.StringIO()
        with patch("sys.stdin", io.StringIO(json.dumps(self.payload))), contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = main(["hook-memory", "--tool", "codex"])
        self.assertEqual(code, 0)
        self.assertIn("systemMessage", json.loads(out.getvalue()))
        self.assertFalse(ledger.exists())

    def test_malformed_json_returns_nonblocking_diagnostic(self):
        out = io.StringIO()
        with patch("sys.stdin", io.StringIO("not-json")), contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = main(["hook-memory", "--tool", "workbuddy"])
        self.assertEqual(code, 0)
        self.assertIn("HOOK_INPUT_INVALID", json.loads(out.getvalue())["systemMessage"])

    def test_zcode_temporary_transcript_is_not_a_durable_source(self):
        payload = dict(self.payload, hook_event_name="Stop", transcript_path=str(self.root / "secret-temporary.json"))
        handle_hook("zcode", payload)
        source = candidate(self.root, pending(self.root)[0]["candidate_id"])
        serialized = json.dumps(source)
        self.assertIn("temporary_not_retained", serialized)
        self.assertNotIn("secret-temporary.json", serialized)


if __name__ == "__main__":
    unittest.main()
