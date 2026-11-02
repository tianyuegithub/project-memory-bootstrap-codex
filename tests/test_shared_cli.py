from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class SharedCliTests(unittest.TestCase):
    def test_cli_round_trip_from_registered_alias_and_missing_state(self):
        repository = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            root, alias = base / "project", base / "checkout"
            root.mkdir()
            alias.mkdir()
            environment = dict(os.environ, PYTHONPATH=str(repository / "src"),
                               PROJECT_MEMORY_REGISTRY=str(base / "registry.json"))

            def invoke(*arguments, payload=None, expected=0, decode=True):
                result = subprocess.run([sys.executable, "-m", "project_memory_bootstrap_codex.cli", *map(str, arguments)],
                    input=json.dumps(payload) if payload is not None else None, text=True, capture_output=True,
                    cwd=repository, env=environment, timeout=15)
                self.assertEqual(result.returncode, expected, result.stderr)
                if expected:
                    self.assertEqual(result.stdout, "")
                    return json.loads(result.stderr)
                return json.loads(result.stdout) if decode else result.stdout

            invoke("init", root, decode=False)
            self.assertFalse((root / "CODEX.md").exists())
            self.assertEqual(list((root / ".codex/memory").glob("20??-??-??.md")), [])
            invoke("register-project", root, "--id", "cli-fixture", "--enable-maintenance")
            invoke("register-alias", root, alias, "--repository", "root")
            context = invoke("project-context", alias)
            self.assertEqual(context["root"], str(root))
            self.assertEqual(context["workspace_root"], str(alias))
            evidence = alias / "evidence.md"
            evidence.write_text("This is only a controlled CLI fixture.\n")
            (root / "evidence.md").write_text("Different canonical content; must not be mistaken for the alias.\n")
            version = hashlib.sha256(evidence.read_bytes()).hexdigest()
            captured = invoke("capture-memory", alias, payload={
                "source_instance": "cli-test", "object_id": "evidence", "source_revision": version,
                "locator": "evidence.md", "kind": "file", "observed_at": "2026-11-02T01:00:00+00:00",
            })
            self.assertEqual(captured["locator"], str(evidence))
            self.assertEqual(len(invoke("pending-memory", root)), 1)
            proposed = invoke("propose-memory", root, payload={
                "candidate_id": captured["candidate_id"], "verified_by": "cli-integration-test",
                "verification_scope": "Only the alias fixture, not the canonical source or a deployment.",
                "changes": [{"fact_id": "F-cli-proof", "expected_version": None, "title": "CLI fixture",
                    "body": "UNIQUE-CLI-PROOF is an observation of the registered alias fixture.",
                    "kind": "verification", "scope": ["repo:root"], "status": "active",
                    "sources": [{"source_id": captured["source_id"], "version": version, "locator": captured["locator"]}],
                    "verified_at": "2026-11-02", "verification": "Read the alias fixture only.", "expires_when": "The fixture changes."}],
            })
            receipt = invoke("commit-memory", root, proposed["proposal_id"])
            self.assertEqual(receipt["project_id"], "cli-fixture")
            fetched = invoke("get-memory", alias, "F-cli-proof", "--json")
            self.assertIn("UNIQUE-CLI-PROOF", fetched["text"])
            self.assertEqual(invoke("memory-health", root)["status"], "ok")
            self.assertEqual(invoke("pending-memory", alias), [])
            error = invoke("consolidate-memory", root, "--dry-run", expected=2)
            self.assertEqual(error["error"]["code"], "LEGACY_CONSOLIDATION_DISABLED")
            (root / ".codex/memory-state/ledger.sqlite").unlink()
            error = invoke("get-memory", alias, "F-cli-proof", "--json", expected=2)
            self.assertEqual(error["error"]["code"], "STATE_MISSING")


if __name__ == "__main__":
    unittest.main()
