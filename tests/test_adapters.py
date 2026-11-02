from __future__ import annotations

import json
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from project_memory_bootstrap_codex import adapters
from project_memory_bootstrap_codex.errors import MemoryProtocolError


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.config = self.root / "settings.json"

    def write(self, value):
        self.config.write_text(json.dumps(value))
        self.config.chmod(0o600)

    def read(self):
        return json.loads(self.config.read_text())

    def error(self, code, call):
        with self.assertRaises(MemoryProtocolError) as caught:
            call()
        self.assertEqual(caught.exception.code, code)

    def test_command_hosts_preserve_foreign_values_and_are_idempotent(self):
        for tool in ("codex", "claude", "workbuddy"):
            with self.subTest(tool=tool):
                foreign = {"type": "command", "command": "project-memory-bootstrap-codex unrelated"}
                group = {"matcher": "*", "custom": "unchanged", "hooks": [foreign]}
                self.write({"env": {"API_KEY": "secret-sentinel"}, "plugin": ["existing"],
                            "hooks": {"SessionStart": [group], "OtherEvent": [{"unchanged": True}]}})
                first = adapters.install_adapter(tool, self.config)
                first_config = self.read()
                adapters.install_adapter(tool, self.config)
                self.assertEqual(first_config, self.read())
                self.assertEqual(first["status"], "configured_not_runtime_verified")
                self.assertEqual(self.read()["env"]["API_KEY"], "secret-sentinel")
                self.assertEqual(self.read()["hooks"]["SessionStart"][0], group)
                self.assertEqual(self.read()["hooks"]["OtherEvent"], [{"unchanged": True}])
                self.assertEqual(self.read()["plugin"], ["existing"])
                self.assertEqual(len(self.read()["hooks"]["Stop"]), 1)
                owner = self.root / f".project-memory-adapter-{tool}.json"
                self.assertNotIn("secret-sentinel", owner.read_text())
                self.assertEqual(stat.S_IMODE(owner.stat().st_mode), 0o600)
                self.assertEqual(stat.S_IMODE(self.config.stat().st_mode), 0o600)

    def test_recorded_old_driver_replaced_without_prefix_matching(self):
        with patch.object(adapters, "driver", return_value=["/old/python", "-I", "-m", "package.cli"]):
            adapters.install_adapter("claude", self.config)
        config = self.read()
        old = config["hooks"]["SessionStart"][0]["hooks"][0]
        similar = dict(old, command=old["command"] + " --unrelated")
        config["hooks"]["SessionStart"][0]["hooks"].append(similar)
        self.write(config)
        with patch.object(adapters, "driver", return_value=["/new/python", "-I", "-m", "package.cli"]):
            adapters.install_adapter("claude", self.config)
        groups = self.read()["hooks"]["SessionStart"]
        self.assertEqual(groups[0]["hooks"], [similar])
        self.assertIn("/new/python", groups[1]["hooks"][0]["command"])
        self.assertNotIn(old, [handler for group in groups for handler in group["hooks"]])

    def test_disabled_command_host_is_unchanged(self):
        for tool in ("claude", "codex", "workbuddy"):
            self.write({"disableAllHooks": True, "env": {"KEY": "secret-sentinel"}})
            before = self.config.read_bytes()
            self.error("HOOKS_DISABLED", lambda: adapters.install_adapter(tool, self.config))
            self.assertEqual(self.config.read_bytes(), before)

    def test_zcode_requires_workspace_and_preserves_disabled_foreign_hooks(self):
        self.error("HOOK_ENABLE_SCOPE_CHANGE", lambda: adapters.install_adapter("zcode", self.config))
        self.error("HOOK_ENABLE_SCOPE_CHANGE", lambda: adapters.install_adapter("zcode", self.config, project_root=self.root / "other"))
        self.write({"hooks": {"enabled": False, "events": {"Other": [{"type": "process", "command": "other"}]}}})
        before = self.config.read_bytes()
        self.error("HOOK_ENABLE_SCOPE_CHANGE", lambda: adapters.install_adapter("zcode", self.config, project_root=self.root))
        self.assertEqual(self.config.read_bytes(), before)

    def test_zcode_process_handlers_and_driver_upgrade(self):
        self.write({"hooks": {"enabled": False, "events": {}}, "env": {"KEY": "secret-sentinel"}})
        for executable in ("/old/python", "/new/python", "/new/python"):
            with patch.object(adapters, "driver", return_value=[executable, "-I", "-m", "package.cli"]):
                adapters.install_adapter("zcode", self.config, project_root=self.root)
        value = self.read()
        self.assertTrue(value["hooks"]["enabled"])
        self.assertEqual(value["hooks"]["events"]["Stop"], [{"hooks": [{"type": "process", "command": "/new/python",
                         "args": ["-I", "-m", "package.cli", "hook-memory", "--tool", "zcode"]}]}])
        self.assertEqual(value["env"]["KEY"], "secret-sentinel")

    def test_zcode_upgrades_recorded_direct_handlers_preserving_foreign_groups(self):
        old = {"type": "process", "command": "/old/python",
               "args": ["-I", "-m", "package.cli", "hook-memory", "--tool", "zcode"]}
        foreign = dict(old, args=old["args"] + ["--unrelated"])
        group = {"matcher": "startup", "hooks": [old, foreign], "custom": "unchanged"}
        self.write({"env": {"KEY": "secret-sentinel"}, "hooks": {"enabled": True, "events": {
            "SessionStart": [old, group], "Stop": [old],
            "PreToolUse": [{"matcher": "Bash", "hooks": [foreign]}]}}})
        owner = self.root / ".project-memory-adapter-zcode.json"
        owner.write_text(json.dumps({"tool": "zcode", "config_path": str(self.config),
                                    "handlers": {"SessionStart": old, "Stop": old}}))
        adapters.install_adapter("zcode", self.config, project_root=self.root)
        first = self.read()
        adapters.install_adapter("zcode", self.config, project_root=self.root)
        self.assertEqual(self.read(), first)
        events = first["hooks"]["events"]
        self.assertEqual(events["SessionStart"][0], dict(group, hooks=[foreign]))
        self.assertEqual(len(events["SessionStart"]), 2)
        self.assertEqual(len(events["Stop"]), 1)
        self.assertEqual(events["Stop"][0]["hooks"][0]["type"], "process")
        self.assertEqual(events["PreToolUse"], [{"matcher": "Bash", "hooks": [foreign]}])
        self.assertEqual(first["env"]["KEY"], "secret-sentinel")

    def test_zcode_disabled_grouped_foreign_handler_is_not_enabled(self):
        self.write({"hooks": {"enabled": False, "events": {"SessionStart": [{
            "matcher": "startup", "hooks": [{"type": "process", "command": "foreign", "args": []}]}]}}})
        before = self.config.read_bytes()
        self.error("HOOK_ENABLE_SCOPE_CHANGE", lambda: adapters.install_adapter("zcode", self.config, project_root=self.root))
        self.assertEqual(self.config.read_bytes(), before)

    def test_zcode_disabled_owned_direct_handlers_can_be_upgraded(self):
        old = {"type": "process", "command": "/old/python", "args": ["hook-memory", "--tool", "zcode"]}
        self.write({"hooks": {"enabled": False, "events": {
            "SessionStart": [old], "Stop": [{"matcher": ".*", "hooks": [old]}]}}})
        owner = self.root / ".project-memory-adapter-zcode.json"
        owner.write_text(json.dumps({"tool": "zcode", "config_path": str(self.config),
                                    "handlers": {"SessionStart": old, "Stop": old}}))
        adapters.install_adapter("zcode", self.config, project_root=self.root)
        value = self.read()
        self.assertTrue(value["hooks"]["enabled"])
        self.assertEqual(value["hooks"]["events"]["Stop"][0], {"matcher": ".*", "hooks": []})
        self.assertEqual(value["hooks"]["events"]["Stop"][1]["hooks"][0]["type"], "process")

    def test_opencode_resource_preserves_config_and_existing_plugins(self):
        self.write({"plugin": ["existing-plugin"], "env": {"KEY": "secret-sentinel"}})
        before = self.config.read_bytes()
        with patch.object(adapters, "driver", return_value=["/python with space", "-I", "-m", "package.cli"]):
            adapters.install_adapter("opencode", self.config)
            plugin = self.root / "plugins/project-memory-shared.js"
            first = plugin.read_bytes()
            adapters.install_adapter("opencode", self.config)
        self.assertEqual(self.config.read_bytes(), before)
        self.assertEqual(plugin.read_bytes(), first)
        self.assertNotIn("__PROJECT_MEMORY_DRIVER__", plugin.read_text())
        self.assertIn('const driver = ["/python with space", "-I", "-m", "package.cli"];', plugin.read_text())
        self.assertEqual(stat.S_IMODE(plugin.stat().st_mode), 0o600)
        self.config.unlink()
        adapters.install_adapter("opencode", self.config)
        self.assertFalse(self.config.exists())

    def test_opencode_v2_api_fixture(self):
        repository = Path(__file__).resolve().parents[1]
        with patch.object(adapters, "driver", return_value=["/fixture/python", "-I", "-m", "fixture.cli"]):
            adapters.install_adapter("opencode", self.config)
        result = subprocess.run(["node", "--experimental-vm-modules",
                                 str(repository / "tests/opencode-plugin-fixture.mjs"),
                                 str(self.root / "plugins/project-memory-shared.js")],
                                text=True, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("real host unverified", result.stdout)

    def test_opencode_rejects_foreign_plugin(self):
        plugin = self.root / "plugins/project-memory-shared.js"
        plugin.parent.mkdir()
        plugin.write_text("// user plugin\n")
        self.error("HOOK_PLUGIN_NOT_OWNED", lambda: adapters.install_adapter("opencode", self.config))
        self.assertEqual(plugin.read_text(), "// user plugin\n")
        self.assertFalse(self.config.exists())

    def test_concurrent_change_detected_before_replacement(self):
        self.write({"env": {"KEY": "secret-sentinel"}})
        original_replace = adapters._replace

        def concurrent_edit(path, expected, content):
            if path == self.config:
                self.config.write_text('{"concurrent": true}')
            return original_replace(path, expected, content)

        with patch.object(adapters, "_replace", side_effect=concurrent_edit):
            self.error("CONFIG_CHANGED", lambda: adapters.install_adapter("claude", self.config))
        self.assertEqual(self.read(), {"concurrent": True})
        self.assertFalse((self.root / ".project-memory-adapter-claude.json").exists())


if __name__ == "__main__":
    unittest.main()
