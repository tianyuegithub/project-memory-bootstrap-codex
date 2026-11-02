from __future__ import annotations

from dataclasses import asdict
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.server.fastmcp.exceptions import ToolError

from project_memory_bootstrap_codex.bootstrap import init_project
from project_memory_bootstrap_codex import mcp_server, memory_state
from project_memory_bootstrap_codex.errors import MemoryProtocolError
from project_memory_bootstrap_codex.memory_index import get_memory_by_id
from project_memory_bootstrap_codex.project import register_project


class McpServerTests(unittest.IsolatedAsyncioTestCase):
    async def test_official_stdio_client_registered_fixture_round_trip(self):
        self.assertEqual(importlib.metadata.version("mcp"), "1.26.0")
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            registry = base / "registry.json"
            environment = dict(os.environ, PROJECT_MEMORY_REGISTRY=str(registry),
                               PROJECT_MEMORY_TEST_PROVIDER_ROOT=str(base / "provider-home"))
            with patch.dict(os.environ, {"PROJECT_MEMORY_REGISTRY": str(registry)}):
                roots = {}
                for name, enabled in (("project", True), ("other", True), ("disabled", False)):
                    root = base / name
                    root.mkdir()
                    init_project(root)
                    context = register_project(root, name, {"root": "."}, maintenance_enabled=enabled)
                    memory_state.initialize_state(context, new_registration=True)
                    roots[name] = root
                unregistered = base / "unregistered"
                unregistered.mkdir()
                init_project(unregistered)
                sessions_dir = base / "provider-home/.codex/sessions"
                sessions_dir.mkdir(parents=True)
                for name, root in roots.items():
                    rows = [{"type": "session_meta", "payload": {"id": "ses-" + name, "cwd": str(root)}},
                            {"timestamp": "now", "type": "response_item", "payload": {"type": "message", "role": "user",
                             "content": [{"type": "input_text", "text": "public " + name}]}}]
                    (sessions_dir / ("ses-" + name + ".jsonl")).write_text("".join(json.dumps(row) + "\n" for row in rows))
                note_body = "Controlled ZCode topic body.\n"
                note_root = (base / "provider-home/.zcode/cli/memories/projects" /
                             ("fixture-" + hashlib.sha256(str(roots["project"]).encode()).hexdigest()[:16]) / "memory")
                note_root.mkdir(parents=True)
                note_root.joinpath("topic.md").write_text("---\nname: MCP topic\ndescription: Controlled schema topic\n"
                    "metadata:\n  node_type: memory\n  type: project\n  originSessionId: ses-project\n---\n" + note_body)
                # Test-only bootstrap isolates provider discovery without changing HOME
                # or adding a production server parameter for arbitrary reader roots.
                bootstrap = """from pathlib import Path
import os
from unittest.mock import patch
from project_memory_bootstrap_codex import mcp_server, native_sources, native_notes
from project_memory_bootstrap_codex.errors import MemoryProtocolError

def fixture_root(tool, home=None):
    if tool not in native_sources.TOOLS:
        raise MemoryProtocolError("NATIVE_SOURCE_UNSUPPORTED", "Unsupported fixture tool.")
    assert home is None
    return Path(os.environ["PROJECT_MEMORY_TEST_PROVIDER_ROOT"]) / ("." + tool)

def fixture_note_base(tool):
    base = Path(os.environ["PROJECT_MEMORY_TEST_PROVIDER_ROOT"])
    return {"zcode": base / ".zcode/cli/memories", "claude": base / ".claude", "workbuddy": base / ".workbuddy"}[tool]

with patch.object(native_sources, "_root", side_effect=fixture_root), patch.object(native_notes, "_provider_base", side_effect=fixture_note_base):
    mcp_server.main()
"""
                parameters = StdioServerParameters(command=sys.executable,
                    args=["-I", "-c", bootstrap], env=environment)
                with (base / "server-stderr.log").open("w+") as errors:
                    async with stdio_client(parameters, errlog=errors) as (read, write):
                        async with ClientSession(read, write) as client:
                            initialized = await client.initialize()
                            self.assertEqual(initialized.serverInfo.name, "shared-project-memory")
                            tools = {tool.name: tool for tool in (await client.list_tools()).tools}
                            self.assertEqual(set(tools), {"memory_project_context", "memory_protocol", "memory_search",
                                "memory_get", "memory_inspect", "memory_capture", "memory_propose", "memory_commit", "memory_health",
                                "memory_pending", "memory_candidate", "memory_scan_sources", "memory_native_sessions", "memory_native_messages",
                                "memory_native_message", "memory_native_notes", "memory_native_note", "memory_begin_maintenance", "memory_finish_maintenance", "memory_maintenance_runs"})
                            for name, tool in tools.items():
                                self.assertFalse(tool.annotations.openWorldHint)
                                self.assertEqual(tool.annotations.readOnlyHint, name not in {
                                    "memory_capture", "memory_propose", "memory_commit", "memory_scan_sources",
                                    "memory_begin_maintenance", "memory_finish_maintenance"})
                            self.assertEqual(tools["memory_capture"].inputSchema["properties"]["payload"]["type"], "object")
                            self.assertEqual(tools["memory_search"].inputSchema["properties"]["limit"]["default"], 5)
                            self.assertEqual(tools["memory_protocol"].inputSchema["properties"]["brief"]["default"], True)
                            self.assertEqual(set(tools["memory_commit"].inputSchema["required"]), {"workspace", "proposal_id"})

                            self.assertEqual(set(tools["memory_native_notes"].inputSchema["required"]), {"workspace"})
                            self.assertEqual(set(tools["memory_native_note"].inputSchema["required"]), {"workspace", "locator"})
                            for optional in ("query", "role"):
                                self.assertIsNone(tools["memory_native_messages"].inputSchema["properties"][optional]["default"])
                            self.assertEqual(set(tools["memory_native_messages"].inputSchema["required"]), {"workspace", "tool", "session_id"})

                            async def call(name, values, error=None):
                                result = await client.call_tool(name, values)
                                if error:
                                    self.assertTrue(result.isError, result)
                                    self.assertIn(error, result.content[0].text)
                                    return result
                                self.assertFalse(result.isError, result)
                                if result.structuredContent is not None:
                                    structured = result.structuredContent
                                    return structured["result"] if set(structured) == {"result"} else structured
                                return json.loads(result.content[0].text)

                            root = roots["project"]
                            workspace = {"workspace": str(root)}
                            identity = await call("memory_project_context", workspace)
                            self.assertEqual(identity["root"], str(root))
                            self.assertEqual(identity["project_id"], "project")
                            self.assertTrue(identity["registered"])
                            self.assertNotIn("projects", identity)
                            self.assertIn("text", await call("memory_protocol", {}))
                            payload = {"source_instance": "mcp-fixture", "object_id": "observation", "source_revision": "1",
                                "locator": "test://controlled-observation", "kind": "observation",
                                "observed_at": "2026-11-02T01:00:00+00:00", "excerpt": "Verified fixture observation."}
                            source = await call("memory_capture", dict(workspace, payload=payload))
                            self.assertEqual(memory_state.candidate(root, source["candidate_id"])["candidate_id"], source["candidate_id"])
                            self.assertEqual(await call("memory_candidate", dict(workspace, candidate_id=source["candidate_id"])),
                                             memory_state.candidate(root, source["candidate_id"]))
                            self.assertEqual((await call("memory_pending", dict(workspace, source_prefix="mcp-fixture")))[0]["candidate_id"], source["candidate_id"])
                            self.assertEqual(await call("memory_pending", dict(workspace, source_prefix="not-present")), [])
                            change = {"fact_id": "F-mcp-fixture", "expected_version": None, "title": "MCP fixture",
                                "body": "MCPUNIQUE proof from controlled fixture.", "kind": "decision", "scope": ["repo:root"],
                                "status": "active", "sources": [{"source_id": source["source_id"], "version": "1", "locator": source["locator"]}],
                                "verified_at": "2026-11-02", "verification": "Controlled fixture, no model or GUI acceptance.",
                                "expires_when": "The fixture changes."}
                            proposal_payload = {"candidate_id": source["candidate_id"], "verified_by": "mcp-test",
                                "verification_scope": "Only the isolated fixture.", "changes": [change]}
                            proposal = await call("memory_propose", dict(workspace, payload=proposal_payload))
                            # IDs in one project's ledger cannot be consumed in another registered project.
                            await call("memory_propose", {"workspace": str(roots["other"]), "payload": proposal_payload}, "CANDIDATE_NOT_FOUND")
                            await call("memory_commit", {"workspace": str(roots["other"]), "proposal_id": proposal["proposal_id"]}, "PROPOSAL_NOT_FOUND")
                            receipt = await call("memory_commit", dict(workspace, proposal_id=proposal["proposal_id"]))
                            self.assertEqual(receipt["project_id"], "project")
                            fact = await call("memory_get", dict(workspace, fact_id="F-mcp-fixture"))
                            self.assertEqual(fact, asdict(get_memory_by_id(root, "F-mcp-fixture")))
                            self.assertIn("MCPUNIQUE", fact["text"])
                            self.assertEqual((await call("memory_inspect", dict(workspace, fact_id="F-mcp-fixture"))), memory_state.inspect(root, "F-mcp-fixture"))
                            search = await call("memory_search", dict(workspace, query="MCPUNIQUE"))
                            self.assertEqual(search[0]["id"], "F-mcp-fixture")
                            self.assertEqual((await call("memory_health", workspace))["status"], "ok")
                            native = await call("memory_native_sessions", dict(workspace, tool="codex"))
                            self.assertEqual([row["session_id"] for row in native], ["ses-project"])
                            metadata = await call("memory_native_messages", dict(workspace, tool="codex", session_id="ses-project"))
                            self.assertEqual(len(metadata), 1)
                            self.assertNotIn("text", metadata[0])
                            public = await call("memory_native_message", dict(workspace, tool="codex", session_id="ses-project",
                                                message_id=metadata[0]["message_id"]))
                            self.assertEqual(public[0]["text"], "public project")
                            filtered = await call("memory_native_messages", dict(workspace, tool="codex", session_id="ses-project",
                                                  query="PUBLIC PROJECT", role="user"))
                            self.assertEqual(filtered, metadata)
                            self.assertEqual(await call("memory_native_messages", dict(workspace, tool="codex", session_id="ses-project",
                                                        query="not present", role="user")), [])
                            self.assertEqual(await call("memory_native_messages", dict(workspace, tool="codex", session_id="ses-project", role="assistant")), [])
                            await call("memory_native_messages", dict(workspace, tool="codex", session_id="ses-project", role="system"), "NATIVE_SOURCE_INPUT_INVALID")
                            topics = await call("memory_native_notes", dict(workspace, tool="zcode", query="SCHEMA TOPIC"))
                            self.assertEqual(len(topics["notes"]), 1)
                            note_metadata = topics["notes"][0]
                            self.assertNotIn("text", note_metadata)
                            self.assertNotIn(note_body.strip(), json.dumps(topics))
                            self.assertEqual(note_metadata["note_type"], "project")
                            self.assertEqual(note_metadata["origin_session_id"], "ses-project")
                            self.assertEqual(topics["errors"], [])
                            self.assertEqual((await call("memory_native_notes", dict(workspace, tool="zcode", query="absent topic")))["notes"], [])
                            note = await call("memory_native_note", dict(workspace, locator=note_metadata["locator"]))
                            self.assertEqual(note["text"], note_body)
                            await call("memory_native_note", {"workspace": str(roots["other"]), "locator": note["locator"]}, "NATIVE_MEMORY_SCOPE_MISMATCH")
                            note_source = await call("memory_capture", dict(workspace, payload={**payload, "source_instance": "native-memory.zcode",
                                "object_id": note["locator"], "source_revision": note["source_revision"], "locator": note["locator"], "kind": "native_memory_note"}))
                            note_change = {**change, "fact_id": "F-mcp-native-note", "body": note_body,
                                "sources": [{"source_id": note_source["source_id"], "version": note["source_revision"], "locator": note["locator"]}]}
                            note_proposal = await call("memory_propose", dict(workspace, payload={**proposal_payload,
                                "candidate_id": note_source["candidate_id"], "changes": [note_change]}))
                            await call("memory_commit", dict(workspace, proposal_id=note_proposal["proposal_id"]))
                            old_note = await call("memory_inspect", dict(workspace, fact_id="F-mcp-native-note"))
                            forget_source = await call("memory_capture", dict(workspace, payload={**payload, "object_id": "explicit-forget-note"}))
                            forget_change = {**change, "fact_id": "F-mcp-native-note", "expected_version": old_note["version"],
                                "status": "forgotten", "body": "", "forget_sources": True,
                                "sources": [{"source_id": forget_source["source_id"], "version": "1", "locator": forget_source["locator"]}]}
                            forget_proposal = await call("memory_propose", dict(workspace, payload={**proposal_payload,
                                "candidate_id": forget_source["candidate_id"], "changes": [forget_change]}))
                            await call("memory_commit", dict(workspace, proposal_id=forget_proposal["proposal_id"]))
                            self.assertEqual((await call("memory_native_notes", dict(workspace, tool="zcode")))["notes"], [])
                            await call("memory_native_note", dict(workspace, locator=note["locator"]), "SOURCE_SUPPRESSED")
                            self.assertTrue(note_root.joinpath("topic.md").exists())
                            self.assertEqual(await call("memory_native_messages", dict(workspace, tool="codex", session_id="ses-other")), [])
                            scan = await call("memory_scan_sources", dict(workspace, files_only=True))
                            self.assertEqual(scan["coverage"]["tools"], [])
                            self.assertGreater(scan["files_observed"], 0)
                            self.assertGreater(scan["new_candidates"], 0)
                            scan_native = await call("memory_scan_sources", dict(workspace, tools=["codex"]))
                            self.assertEqual(scan_native["sessions_observed"], 1)
                            run = await call("memory_begin_maintenance", dict(workspace, tool="codex", session_id="ses-project"))
                            self.assertEqual(run["status"], "running")
                            finished = await call("memory_finish_maintenance", dict(workspace, run_id=run["run_id"],
                                payload={"status": "succeeded", "coverage": "Only controlled MCP fixture.",
                                         "operation_ids": [receipt["operation_id"]]}))
                            self.assertEqual(finished["status"], "succeeded")
                            self.assertEqual((await call("memory_maintenance_runs", workspace))[0], finished)
                            await call("memory_finish_maintenance", {"workspace": str(roots["other"]), "run_id": run["run_id"],
                                "payload": {"status": "succeeded", "coverage": "Cross-project must reject."}}, "RUN_NOT_FOUND")
                            for name in set(tools) - {"memory_protocol"}:
                                arguments = {"workspace": str(unregistered)}
                                if name in {"memory_capture", "memory_propose"}: arguments["payload"] = {}
                                if name in {"memory_get", "memory_inspect"}: arguments["fact_id"] = "F-mcp-fixture"
                                if name == "memory_commit": arguments["proposal_id"] = proposal["proposal_id"]
                                if name == "memory_search": arguments["query"] = "MCPUNIQUE"
                                if name == "memory_candidate": arguments["candidate_id"] = source["candidate_id"]
                                if name.startswith("memory_native_") and name not in {"memory_native_note", "memory_native_notes"} or name == "memory_begin_maintenance": arguments["tool"] = "codex"
                                if name == "memory_native_note": arguments["locator"] = note["locator"]
                                if name in {"memory_native_messages", "memory_native_message", "memory_begin_maintenance"}: arguments["session_id"] = "ses-project"
                                if name == "memory_native_message": arguments["message_id"] = metadata[0]["message_id"]
                                if name == "memory_finish_maintenance": arguments.update(run_id=run["run_id"], payload={})
                                await call(name, arguments, "PROJECT_UNREGISTERED")
                            await call("memory_capture", {"workspace": str(roots["disabled"]), "payload": payload}, "MAINTENANCE_DISABLED")
                            # Publication reads must detect external edits instead of bypassing the document owner.
                            root.joinpath(".codex/memory/MEMORY.md").write_text("unpublished external edit\n")
                            await call("memory_get", dict(workspace, fact_id="F-mcp-fixture"), "EXTERNAL_EDIT_CONFLICT")

    def test_errors_never_expose_rejected_payload_or_unknown_exception(self):
        for exception, code in ((MemoryProtocolError("INPUT_INVALID", "secret-sentinel"), "INPUT_INVALID"),
                                (RuntimeError("secret-sentinel"), "MEMORY_OPERATION_FAILED")):
            with patch.object(mcp_server, "resolve_context", side_effect=exception):
                with self.assertRaises(ToolError) as caught:
                    mcp_server.memory_project_context("/unregistered")
            self.assertIn(code, str(caught.exception))
            self.assertNotIn("secret-sentinel", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
