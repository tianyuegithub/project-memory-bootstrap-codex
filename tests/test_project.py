import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from project_memory_bootstrap_codex.errors import MemoryProtocolError
from project_memory_bootstrap_codex.project import register_alias, register_project, resolve_context


class ProjectIdentityTests(unittest.TestCase):
    def test_repository_config_alone_does_not_enable_this_machines_maintenance(self):
        self.register(maintenance_enabled=True)
        self.registry.unlink()
        context = self.resolve(self.root)
        self.assertIsNotNone(context)
        self.assertFalse(context.registered)
        self.assertFalse(context.maintenance_enabled)
    def test_unrelated_missing_registration_does_not_disable_current_project(self):
        self.register()
        other = self.base / 'retired'
        other.mkdir()
        register_project(other, 'retired', {'root': '.'}, registry_path=self.registry)
        (other / '.project-memory.json').unlink()
        self.assertEqual(self.resolve(self.root).project_id, 'example')

    def test_alias_storage_cannot_follow_a_link_outside_the_project(self):
        self.register()
        outside = self.base / 'outside'
        outside.mkdir()
        (self.root / '.codex').symlink_to(outside, target_is_directory=True)
        with self.assertRaises(MemoryProtocolError):
            register_alias(self.root, self.base / 'checkout', 'root', registry_path=self.registry)
        self.assertFalse((outside / 'memory-state/workspaces.json').exists())

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.root = self.base / 'project'
        self.root.mkdir()
        self.registry = self.base / 'registry.json'

    def git(self, root, *args):
        return subprocess.run(['git', '-C', str(root), *args], check=True, capture_output=True, text=True).stdout.strip()

    def register(self, members=None, **kwargs):
        return register_project(self.root, 'example', members or {'root': '.'}, registry_path=self.registry, **kwargs)

    def resolve(self, path):
        return resolve_context(path, registry_path=self.registry)

    def assertCode(self, code, function):
        with self.assertRaises(MemoryProtocolError) as raised:
            function()
        self.assertEqual(raised.exception.code, code)

    def test_plain_root_and_longest_member(self):
        root = self.register({'root': '.', 'api': 'api'})
        nested = self.root / 'api' / 'src'
        nested.mkdir(parents=True)
        context = self.resolve(nested)
        self.assertEqual(context.repository_id, 'api')
        self.assertEqual(context.root, self.root)
        self.assertEqual(context.workspace_root, self.root / 'api')
        self.assertEqual(root.memory_file, self.root / '.codex/memory/MEMORY.md')
        self.assertEqual(root.state_dir, self.root / '.codex/memory-state')
        self.assertFalse(root.maintenance_enabled)

    def test_git_root_and_independent_registered_child(self):
        self.git(self.root, 'init')
        child = self.root / 'api'
        child.mkdir()
        self.git(child, 'init')
        self.register({'root': '.', 'api': 'api'})
        self.assertEqual(self.resolve(self.root).repository_id, 'root')
        self.assertEqual(self.resolve(child).repository_id, 'api')

    def test_unknown_nested_git_repository_is_rejected(self):
        self.git(self.root, 'init')
        self.register()
        unknown = self.root / 'unknown'
        unknown.mkdir()
        self.git(unknown, 'init')
        self.assertCode('PROJECT_UNREGISTERED', lambda: self.resolve(unknown))

    def test_worktree_uses_common_dir(self):
        self.git(self.root, 'init')
        self.git(self.root, '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '--allow-empty', '-m', 'initial')
        self.register()
        workspace = self.base / 'worktree'
        self.git(self.root, 'worktree', 'add', '-b', 'test-worktree', str(workspace))
        context = self.resolve(workspace)
        self.assertEqual(context.root, self.root)
        self.assertEqual(context.workspace_root, workspace)
        self.assertEqual(context.repository_id, 'root')

    def test_explicit_alias_and_similar_unrelated_path(self):
        self.register()
        workspace = self.base / 'copy'
        workspace.mkdir()
        self.git(workspace, 'init')
        register_alias(self.root, workspace, 'root', registry_path=self.registry)
        context = self.resolve(workspace)
        self.assertEqual(context.root, self.root)
        self.assertEqual(context.workspace_root, workspace)
        unrelated = self.base / 'project-unrelated'
        unrelated.mkdir()
        self.assertIsNone(self.resolve(unrelated))
        self.assertIsNone(self.resolve(self.base))

    def test_member_escape_is_rejected(self):
        self.assertCode('PROJECT_CONFIG_INVALID', lambda: self.register({'bad': '../outside'}))
        self.assertCode('PROJECT_CONFIG_INVALID', lambda: self.register({'bad': str(self.base)}))
        outside = self.base / 'outside'
        outside.mkdir()
        (self.root / 'linked').symlink_to(outside, target_is_directory=True)
        self.assertCode('PROJECT_CONFIG_INVALID', lambda: self.register({'bad': 'linked'}))
        self.assertFalse((self.root / '.project-memory.json').exists())

    def test_registration_preserves_instance_members_and_settings(self):
        first = self.register({'root': '.', 'api': 'api'}, maintenance_enabled=True)
        second = self.register({'worker': 'worker'})
        self.assertEqual(first.state_instance_id, second.state_instance_id)
        self.assertTrue(second.maintenance_enabled)
        config = json.loads((self.root / '.project-memory.json').read_text())
        self.assertEqual(config['members'], {'root': '.', 'api': 'api', 'worker': 'worker'})
        self.assertCode('PROJECT_ID_MISMATCH', lambda: register_project(self.root, 'other', {'root': '.'}, registry_path=self.registry))
        self.assertFalse((self.root / '.codex/memory-state/ledger.sqlite').exists())

    def test_registry_and_alias_entries_preserved(self):
        other = self.base / 'other'
        other.mkdir()
        register_project(other, 'other', {'root': '.'}, registry_path=self.registry)
        self.register()
        for name in ('copy-one', 'copy-two'):
            workspace = self.base / name
            workspace.mkdir()
            register_alias(self.root, workspace, 'root', registry_path=self.registry)
        registry = json.loads(self.registry.read_text())
        self.assertEqual(set(registry['projects']), {str(other), str(self.root)})
        aliases = json.loads((self.root / '.codex/memory-state/workspaces.json').read_text())
        self.assertEqual(len(aliases), 2)
        self.assertEqual(self.resolve(self.base / 'copy-one').project_id, 'example')

    def test_malformed_config_and_invalid_schema_fail_explicitly(self):
        config = self.root / '.project-memory.json'
        config.write_text('{broken')
        self.assertCode('PROJECT_CONFIG_INVALID', lambda: self.resolve(self.root))
        config.write_text(json.dumps({'schema_version': 2}))
        self.assertCode('PROJECT_CONFIG_INVALID', lambda: self.resolve(self.root))

    def test_ambiguous_aliases_are_rejected(self):
        self.register()
        other = self.base / 'other'
        other.mkdir()
        register_project(other, 'other', {'root': '.'}, registry_path=self.registry)
        workspace = self.base / 'copy'
        workspace.mkdir()
        register_alias(self.root, workspace, 'root', registry_path=self.registry)
        register_alias(other, workspace, 'root', registry_path=self.registry)
        self.assertCode('PROJECT_AMBIGUOUS', lambda: self.resolve(workspace))

    def test_no_registry_needed_for_canonical_ancestor(self):
        self.register()
        self.registry.unlink()
        self.assertEqual(self.resolve(self.root).project_id, 'example')


if __name__ == '__main__':
    unittest.main()
