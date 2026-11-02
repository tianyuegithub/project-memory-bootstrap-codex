from __future__ import annotations

import builtins
import hashlib
import json
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import quote

from project_memory_bootstrap_codex import native_notes as notes
from project_memory_bootstrap_codex.errors import MemoryProtocolError
from project_memory_bootstrap_codex.project import register_alias, register_project
from project_memory_bootstrap_codex.bootstrap import init_project
from project_memory_bootstrap_codex.memory_state import initialize_state


class NativeNotesTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.root = self.base / 'project'
        self.root.mkdir()
        self.api = self.root / 'api'
        self.api.mkdir()
        self.git(self.root, 'init', '-q')
        self.git(self.api, 'init', '-q')
        registry = self.base / 'registry.json'
        environment = patch.dict(os.environ, {'PROJECT_MEMORY_REGISTRY': str(registry)})
        environment.start()
        self.addCleanup(environment.stop)
        self.registry = registry
        init_project(self.root)
        context = register_project(self.root, 'fixture', {'root': '.', 'api': 'api'}, registry_path=registry)
        initialize_state(context, new_registration=True)
        self.providers = {tool: self.base / 'providers' / tool for tool in notes.TOOLS}
        provider = patch.object(notes, '_provider_base', side_effect=self.providers.__getitem__)
        provider.start()
        self.addCleanup(provider.stop)

    def git(self, root, *args):
        return subprocess.run(['git', '-C', str(root), *args], check=True, capture_output=True, text=True).stdout

    def memory_root(self, tool='zcode', workspace=None):
        workspace = workspace or self.root
        base = self.providers[tool]
        if tool == 'zcode':
            digest = hashlib.sha256(str(workspace).encode()).hexdigest()[:16]
            return base / 'projects' / f'{workspace.name}-{digest}' / 'memory'
        slug = re.sub(r'[^a-zA-Z0-9]', '-', str(workspace))
        if tool == 'workbuddy':
            slug = slug.lstrip('-')
        return base / 'projects' / slug / 'memory'

    def write_note(self, topic='主题.md', *, tool='zcode', workspace=None, header=None, body='完整正文。\n条件：只适用于本测试。\n'):
        root = self.memory_root(tool, workspace)
        root.mkdir(parents=True, exist_ok=True)
        target = root / topic
        target.parent.mkdir(parents=True, exist_ok=True)
        if header is None:
            header = 'name: 原生主题\ndescription: 中文说明\n'
            header += 'metadata:\n  node_type: memory\n  type: project\n  originSessionId: session-1\n' if tool == 'zcode' else 'type: project\n'
        target.write_text('---\n' + header + '---\n' + body, encoding='utf-8')
        return target, notes._locator(tool, workspace or self.root, topic)

    def assertCode(self, code, callback):
        with self.assertRaises(MemoryProtocolError) as raised:
            callback()
        self.assertEqual(raised.exception.code, code)

    def test_lists_only_topic_metadata_and_query_matches_chinese(self):
        topic, locator = self.write_note()
        root = topic.parent
        (root / 'MEMORY.md').write_text('This index is not a topic.')
        (root / 'ignored.txt').write_text('not a note')
        result = notes.list_notes(self.root, tools=('zcode',), query='中文')
        self.assertEqual(len(result['notes']), 1)
        meta = result['notes'][0]
        self.assertEqual(meta['locator'], locator)
        self.assertEqual(meta['title'], '原生主题')
        self.assertEqual(meta['note_type'], 'project')
        self.assertEqual(meta['origin_session_id'], 'session-1')
        self.assertEqual(meta['repo'], 'root')
        self.assertEqual(meta['source_revision'], hashlib.sha256(topic.read_bytes()).hexdigest())
        self.assertNotIn('text', meta)
        self.assertNotIn('完整正文', json.dumps(result, ensure_ascii=False))
        self.assertEqual(result['errors'], [])
        self.assertEqual(notes.list_notes(self.root, tools=('zcode',), query='主题.md')['notes'][0]['locator'], locator)
        self.assertEqual(notes.list_notes(self.root, tools=('zcode',), query='完整正文')['notes'], [])

    def test_fixed_claude_and_workbuddy_public_schema_routes(self):
        for tool in ('claude', 'workbuddy'):
            with self.subTest(tool=tool):
                _, locator = self.write_note(tool=tool)
                result = notes.list_notes(self.root, tools=(tool,))
                self.assertEqual(len(result['notes']), 1)
                self.assertEqual(result['notes'][0]['tool'], tool)
                self.assertIn('只适用于本测试', notes.read_note(self.root, locator)['text'])

    def test_workbuddy_legacy_route_only_if_present_and_default_takes_priority(self):
        legacy = self.providers['workbuddy'] / 'memories' / str(self.root).lstrip('/')
        legacy.mkdir(parents=True)
        _, locator = self.write_note(tool='workbuddy')
        default = self.memory_root('workbuddy')
        (default / '主题.md').replace(legacy / 'old.md')
        default.rmdir()
        result = notes.list_notes(self.root, tools=('workbuddy',))
        self.assertEqual(len(result['notes']), 1)
        self.assertEqual(result['coverage']['workbuddy']['workspaces'][0]['route'], 'legacy-existing')
        self.write_note(tool='workbuddy')
        result = notes.list_notes(self.root, tools=('workbuddy',))
        self.assertEqual(result['notes'][0]['locator'], locator)
        self.assertEqual(len(result['notes']), 1)

    def test_default_routes_missing_are_not_claimed_as_no_memory(self):
        result = notes.list_notes(self.root)
        self.assertEqual(result['notes'], [])
        for value in result['coverage'].values():
            self.assertIn('custom workspaceIdentity', value['not_covered'])
            self.assertTrue(all(row['status'] == 'NATIVE_MEMORY_ROOT_UNAVAILABLE' for row in value['workspaces']))
        locator = notes._locator('zcode', self.root, 'missing.md')
        self.assertCode('NATIVE_MEMORY_ROOT_UNAVAILABLE', lambda: notes.source_snapshot(self.root, locator, allow_missing=True))

    def test_members_worktrees_and_explicit_aliases_share_project_scope(self):
        self.git(self.root, '-c', 'user.name=Fixture', '-c', 'user.email=test@example.invalid', 'commit', '--allow-empty', '-qm', 'initial')
        worktree = self.base / 'worktree'
        self.git(self.root, 'worktree', 'add', '-qb', 'fixture-worktree', str(worktree))
        alias = self.base / 'copy'
        alias.mkdir()
        self.git(alias, 'init', '-q')
        register_alias(self.root, alias, 'api', registry_path=self.registry)
        for workspace in (self.root, self.api, worktree, alias):
            self.write_note(workspace=workspace)
        unrelated = self.base / 'project-lookalike'
        unrelated.mkdir()
        self.write_note(workspace=unrelated)
        result = notes.list_notes(worktree, tools=('zcode',))
        self.assertEqual({item['workspace'] for item in result['notes']}, {str(p) for p in (self.root, self.api, worktree, alias)})
        self.assertEqual(next(item['repo'] for item in result['notes'] if item['workspace'] == str(alias)), 'api')
        api_locator = notes._locator('zcode', self.api, '主题.md')
        self.assertIn('完整正文', notes.read_note(worktree, api_locator)['text'])
        self.assertCode('PROJECT_UNREGISTERED', lambda: notes.list_notes(unrelated))

    def test_repo_config_without_local_registration_cannot_read(self):
        _, locator = self.write_note()
        self.registry.unlink()
        self.assertCode('PROJECT_UNREGISTERED', lambda: notes.list_notes(self.root))
        self.assertCode('PROJECT_UNREGISTERED', lambda: notes.read_note(self.root, locator))
        self.assertCode('PROJECT_UNREGISTERED', lambda: notes.source_snapshot(self.root, locator))

    def test_other_registered_project_locator_is_rejected(self):
        other = self.base / 'other'
        other.mkdir()
        register_project(other, 'other', {'root': '.'}, registry_path=self.registry)
        _, locator = self.write_note(workspace=other)
        self.assertCode('NATIVE_MEMORY_SCOPE_MISMATCH', lambda: notes.read_note(self.root, locator))
        self.assertCode('NATIVE_MEMORY_SCOPE_MISMATCH', lambda: notes.source_snapshot(self.root, locator))

    def test_locator_workspace_must_be_exact_repository_or_alias(self):
        nested = self.root / 'src'
        nested.mkdir()
        self.write_note(workspace=nested)
        locator = notes._locator('zcode', nested, '主题.md')
        self.assertCode('NATIVE_MEMORY_SCOPE_MISMATCH', lambda: notes.read_note(self.root, locator))
        unknown = self.root / 'unknown-repository'
        unknown.mkdir()
        self.git(unknown, 'init', '-q')
        self.assertCode('PROJECT_UNREGISTERED', lambda: notes.list_notes(unknown))

    def test_unsafe_and_invalid_locators_are_rejected(self):
        self.write_note()
        for topic in ('../outside.md', '/outside.md', 'nested/../../outside.md', 'MEMORY.md', 'ignored.txt', 'a\\b.md', 'null\0.md'):
            locator = notes._locator('zcode', self.root, topic)
            with self.subTest(topic=topic):
                self.assertCode('NATIVE_MEMORY_LOCATOR_INVALID', lambda: notes.read_note(self.root, locator))
        for locator in ('file:///tmp/note.md', 'native-memory://unknown/a/b', notes._locator('zcode', self.root, '主题.md') + '?override=/tmp', 'native-memory://zcode/not-absolute/topic.md'):
            with self.subTest(locator=locator):
                self.assertCode('NATIVE_MEMORY_LOCATOR_INVALID', lambda: notes.read_note(self.root, locator))

    def test_symlinked_files_and_directories_cannot_escape_provider_root(self):
        self.write_note()
        root = self.memory_root()
        external = self.base / 'external'
        external.mkdir()
        (external / 'note.md').write_text('private outside text')
        (root / 'escape.md').symlink_to(external / 'note.md')
        (root / 'directory').symlink_to(external, target_is_directory=True)
        for topic in ('escape.md', 'directory/note.md'):
            locator = notes._locator('zcode', self.root, topic)
            self.assertCode('NATIVE_MEMORY_PATH_ESCAPE', lambda: notes.read_note(self.root, locator))
            self.assertCode('NATIVE_MEMORY_PATH_ESCAPE', lambda: notes.source_snapshot(self.root, locator, allow_missing=True))
        result = notes.list_notes(self.root, tools=('zcode',))
        self.assertEqual(len(result['notes']), 1)
        self.assertEqual([item['code'] for item in result['errors']], ['NATIVE_MEMORY_PATH_ESCAPE', 'NATIVE_MEMORY_PATH_ESCAPE'])
        self.assertNotIn('private outside text', json.dumps(result))

    def test_provider_root_link_is_rejected(self):
        self.write_note()
        root = self.memory_root()
        replacement = self.base / 'elsewhere'
        root.rename(replacement)
        root.symlink_to(replacement, target_is_directory=True)
        result = notes.list_notes(self.root, tools=('zcode',))
        self.assertEqual(result['notes'], [])
        self.assertEqual(result['errors'][0]['code'], 'NATIVE_MEMORY_PATH_ESCAPE')

    def test_matching_zcode_hash_ambiguity_does_not_guess_slug(self):
        self.write_note()
        root = self.memory_root()
        suffix = root.parent.name.rsplit('-', 1)[1]
        (root.parent.parent / f'another-slug-{suffix}' / 'memory').mkdir(parents=True)
        result = notes.list_notes(self.root, tools=('zcode',))
        self.assertEqual(result['notes'], [])
        self.assertEqual(result['errors'][0]['code'], 'NATIVE_MEMORY_ROOT_AMBIGUOUS')

    def test_unknown_or_malformed_frontmatter_is_not_accepted(self):
        for tool, header in (
            ('zcode', 'name: Topic\ndescription: Description\n'),
            ('zcode', 'name: Topic\ndescription: Description\nmetadata:\n  node_type: note\n  type: project\n'),
            ('zcode', 'name: Topic\ndescription: Description\nmetadata:\n  node_type: memory\n  type: [project]\n'),
            ('claude', 'name: Topic\ndescription: Description\ntype: [project]\n'),
            ('workbuddy', 'name: Topic\ndescription: Description\nunknown: true\n'),
            ('zcode', 'name: [not, scalar]\ndescription: Description\n'),
            ('zcode', 'name: [malformed\n'),
        ):
            with self.subTest(tool=tool, header=header):
                _, locator = self.write_note(tool=tool, header=header)
                self.assertCode('FORMAT_INVALID', lambda: notes.read_note(self.root, locator))
        root = self.memory_root()
        (root / 'no-frontmatter.md').write_text('Unknown free-form body.')
        result = notes.list_notes(self.root, tools=('zcode',))
        self.assertTrue(result['errors'])
        self.assertTrue(all(item['code'] == 'FORMAT_INVALID' for item in result['errors']))
        _, locator = self.write_note(header='name: [password: abcdefghijklmnop\n')
        import traceback
        try:
            notes.read_note(self.root, locator)
        except MemoryProtocolError as exc:
            rendered = ''.join(traceback.format_exception(type(exc), exc, exc.__traceback__))
            self.assertNotIn('abcdefghijklmnop', rendered)

    def test_missing_yaml_parser_is_explicit(self):
        _, locator = self.write_note()
        original_import = builtins.__import__
        def without_yaml(name, *args, **kwargs):
            if name == 'yaml':
                raise ImportError('fixture missing optional parser')
            return original_import(name, *args, **kwargs)
        with patch('builtins.__import__', side_effect=without_yaml):
            self.assertCode('NATIVE_MEMORY_PARSER_UNAVAILABLE', lambda: notes.read_note(self.root, locator))

    def test_sensitive_metadata_and_body_are_omitted_without_leaking(self):
        secret = 'password: abcdefghijklmnop'
        header = f'name: "{secret}"\ndescription: Description\nmetadata:\n  node_type: memory\n  type: feedback\n  ignored: "not selected"\n'
        _, locator = self.write_note(header=header, body=secret + '\n')
        result = notes.list_notes(self.root, tools=('zcode',))
        self.assertTrue(result['notes'][0]['sensitive_metadata_omitted'])
        self.assertNotIn('title', result['notes'][0])
        self.assertEqual(notes.list_notes(self.root, tools=('zcode',), query='abcdefghijklmnop')['notes'], [])
        fetched = notes.read_note(self.root, locator)
        self.assertTrue(fetched['sensitive_text_omitted'])
        self.assertNotIn('text', fetched)
        self.assertNotIn('abcdefghijklmnop', json.dumps([result, fetched]))

    def test_only_whitelisted_scalar_metadata_is_returned(self):
        header = 'name: Topic\ndescription: Description\nunknown:\n  password: abcdefghijklmnop\nmetadata:\n  node_type: memory\n  type: reference\n  arbitrary: [data, ignored]\n'
        _, locator = self.write_note(header=header)
        fetched = notes.read_note(self.root, locator)
        self.assertEqual(fetched['title'], 'Topic')
        self.assertNotIn('unknown', fetched)
        self.assertNotIn('arbitrary', fetched)
        self.assertNotIn('abcdefghijklmnop', json.dumps(fetched))

    def test_native_credential_context_quarantines_notes_metadata_and_snapshot(self):
        snippets = ['密码口径见[[fixture-note]]（FICTIONAL_VALUE_ONE 全链无效，保持secret值）',
                    '**dev库口令**：默认 FICTIONAL_VALUE_TWO，以及${PG_PASSWORD:-FICTIONAL_FALLBACK}',
                    'curl -u fixture-user:FICTIONAL_VALUE_THREE https://fixture.invalid/']
        for index, snippet in enumerate(snippets):
            with self.subTest(example=index):
                path, locator = self.write_note(body=snippet)
                listed = notes.list_notes(self.root, tools=('zcode',))
                exported = notes.read_note(self.root, locator)
                self.assertTrue(exported['sensitive_text_omitted'])
                self.assertEqual(exported['reason'], 'credential_context_requires_curated_evidence')
                self.assertNotIn('text', exported)
                self.assertNotIn('FICTIONAL_', json.dumps([listed, exported]))
                self.assertEqual(exported['source_revision'], hashlib.sha256(path.read_bytes()).hexdigest())
                self.assertCode('NATIVE_MEMORY_SENSITIVE', lambda: notes.source_snapshot(self.root, locator))
        header = ('name: "username FICTIONAL_METADATA_VALUE"\n'
                  'description: "Cookie credentials fixture"\n'
                  'metadata:\n  node_type: memory\n  type: project\n')
        _, locator = self.write_note(header=header, body='Otherwise harmless fixture body.')
        catalog = notes.list_notes(self.root, tools=('zcode',))
        item = catalog['notes'][0]
        self.assertTrue(item['sensitive_metadata_omitted'])
        self.assertEqual(item['reason'], 'credential_context_requires_curated_evidence')
        self.assertNotIn('title', item)
        self.assertNotIn('description', item)
        self.assertNotIn('FICTIONAL_METADATA_VALUE', json.dumps(catalog))
        self.assertNotIn('text', notes.read_note(self.root, locator))
        self.assertCode('NATIVE_MEMORY_SENSITIVE', lambda: notes.source_snapshot(self.root, locator))

    def test_safe_native_feedback_budget_tokens_and_reference_only_remain_readable(self):
        body = '筛选反馈已复核。预算 token 为 4096，output tokens 为 512。仅引用 ${PG_PASSWORD}。\n'
        _, locator = self.write_note(body=body)
        self.assertEqual(notes.read_note(self.root, locator)['text'], body)
        snapshot = notes.source_snapshot(self.root, locator)
        self.assertEqual(snapshot['source_instance'], 'native-memory.zcode')
        self.assertNotIn('text', snapshot)

    def test_native_note_namespace_cannot_bypass_credential_quarantine(self):
        from project_memory_bootstrap_codex import memory_state as state
        register_project(self.root, 'fixture', {'root': '.'}, maintenance_enabled=True, registry_path=self.registry)
        path, locator = self.write_note(body='密码口径见[[fixture]]，FICTIONAL_NAMESPACE_VALUE 仅为测试。')
        for namespace in ('ai-arbitrary', 'native-memory.zcode'):
            with self.subTest(namespace=namespace):
                self.assertCode('NATIVE_MEMORY_SENSITIVE', lambda: state.capture(self.root, dict(
                    source_instance=namespace, object_id='custom-object', source_revision=hashlib.sha256(path.read_bytes()).hexdigest(),
                    locator=locator, kind='observation', observed_at='2026-11-02T03:00:00+00:00', excerpt='Safe annotation only.')))
        self.assertEqual(state.pending(self.root), [])

    def test_snapshot_is_byte_revision_and_restoration_keeps_identity(self):
        path, locator = self.write_note()
        raw = path.read_bytes()
        first = notes.source_snapshot(self.root, locator)
        self.assertEqual(first, {'source_instance': 'native-memory.zcode', 'object_id': locator, 'locator': locator, 'source_revision': hashlib.sha256(raw).hexdigest()})
        path.unlink()
        self.assertCode('NATIVE_MEMORY_TOPIC_MISSING', lambda: notes.source_snapshot(self.root, locator))
        missing = notes.source_snapshot(self.root, locator, allow_missing=True)
        self.assertEqual(missing['source_revision'], 'missing')
        self.assertEqual(missing['object_id'], first['object_id'])
        self.assertNotIn('text', missing)
        path.write_bytes(raw + b'\nchanged\n')
        changed = notes.source_snapshot(self.root, locator)
        self.assertNotEqual(changed['source_revision'], first['source_revision'])
        self.assertEqual(changed['object_id'], first['object_id'])
        path.write_bytes(raw)
        self.assertEqual(notes.source_snapshot(self.root, locator), first)


if __name__ == '__main__':
    unittest.main()
