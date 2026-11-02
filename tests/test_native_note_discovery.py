"""Native-note compensation scans use real capture and temporary providers."""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from project_memory_bootstrap_codex import discovery
from project_memory_bootstrap_codex import memory_state as state
from project_memory_bootstrap_codex import native_notes as notes
from project_memory_bootstrap_codex.bootstrap import init_project
from project_memory_bootstrap_codex.errors import MemoryProtocolError
from project_memory_bootstrap_codex.project import register_project


class NativeNoteDiscoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.root = self.base / 'project'
        self.root.mkdir()
        environment = patch.dict(os.environ, PROJECT_MEMORY_REGISTRY=str(self.base / 'registry.json'))
        environment.start()
        self.addCleanup(environment.stop)
        init_project(self.root)
        self.context = register_project(self.root, 'note-discovery', {'root': '.'}, maintenance_enabled=True)
        state.initialize_state(self.context, new_registration=True)
        self.providers = {tool: self.base / 'providers' / tool for tool in notes.TOOLS}
        provider = patch.object(notes, '_provider_base', side_effect=self.providers.__getitem__)
        provider.start()
        self.addCleanup(provider.stop)
        self.session_listing = patch.object(discovery, 'list_sessions', return_value=[])
        self.session_listing.start()
        self.addCleanup(self.session_listing.stop)

    def write_note(self, topic='topic.md', *, tool='claude', body='RAW_TOPIC_BODY_NOT_AN_EXCERPT\n'):
        if tool == 'zcode':
            suffix = hashlib.sha256(str(self.root).encode()).hexdigest()[:16]
            root = self.providers[tool] / 'projects' / f'fixture-{suffix}' / 'memory'
        else:
            slug = re.sub(r'[^a-zA-Z0-9]', '-', str(self.root))
            if tool == 'workbuddy':
                slug = slug.lstrip('-')
            root = self.providers[tool] / 'projects' / slug / 'memory'
        path = root / topic
        path.parent.mkdir(parents=True, exist_ok=True)
        header = 'name: Fixture topic\ndescription: Metadata-only fixture description\n'
        header += 'metadata:\n  node_type: memory\n  type: project\n' if tool == 'zcode' else 'type: project\n'
        path.write_text('---\n' + header + '---\n' + body, encoding='utf-8')
        return path, notes._locator(tool, self.root, topic)

    def scan(self, tools=('claude',)):
        return discovery.scan_sources(self.root, tools=tools)

    def cursor(self):
        with state._locked(self.context) as connection:
            return discovery._cursor(connection)

    def candidate(self, uri):
        record = self.cursor()['native_notes'][uri]
        return state.candidate(self.root, record['candidate_id'])

    def test_default_scan_discovers_all_supported_note_tools_without_raw_text_or_index(self):
        uris = []
        for tool in ('zcode', 'claude', 'workbuddy'):
            path, uri = self.write_note(tool=tool)
            (path.parent / 'MEMORY.md').write_text('INDEX_MUST_NEVER_BECOME_A_TOPIC')
            uris.append(uri)
        self.scan(tools=())
        with patch.object(notes, 'list_notes', wraps=notes.list_notes) as listing:
            result = discovery.scan_sources(self.root)
        self.assertEqual(set(listing.call_args.kwargs['tools']), {'zcode', 'claude', 'workbuddy'})
        self.assertEqual(result['native_notes_observed'], 3)
        self.assertEqual(result['new_candidates'], 3)
        self.assertEqual(result['errors'], [])
        cursor = self.cursor()['native_notes']
        self.assertEqual(set(cursor), set(uris))
        for uri in uris:
            payload = self.candidate(uri)['payload']
            self.assertEqual(payload['kind'], 'native_memory_note')
            self.assertEqual(payload['locator'], uri)
            self.assertEqual(payload['object_id'], uri)
            self.assertEqual(payload['source_revision'], notes.source_snapshot(self.root, uri)['source_revision'])
            metadata = json.loads(payload['excerpt'])
            self.assertNotIn('text', metadata)
            self.assertEqual(metadata['title'], 'Fixture topic')
            self.assertNotIn('RAW_TOPIC_BODY', json.dumps(payload))
        self.assertNotIn('RAW_TOPIC_BODY', json.dumps(cursor))
        self.assertNotIn('MEMORY.md', json.dumps(cursor))

    def test_selected_tools_and_replay_capture_only_changes(self):
        self.write_note(tool='zcode')
        _, uri = self.write_note(tool='claude')
        self.scan(tools=())
        with patch.object(notes, 'list_notes', wraps=notes.list_notes) as listing:
            first = self.scan()
        self.assertEqual(listing.call_args.kwargs['tools'], ('claude',))
        self.assertEqual(first['new_candidates'], 1)
        self.assertEqual(set(self.cursor()['native_notes']), {uri})
        with patch.object(state, 'capture', wraps=state.capture) as capture:
            second = self.scan()
        self.assertEqual(second['new_candidates'], 0)
        self.assertEqual(second['errors'], [])
        capture.assert_not_called()

    def test_content_change_missing_and_original_restoration_advance_one_source_epochs(self):
        path, uri = self.write_note()
        original = path.read_bytes()
        self.scan()
        entries = [dict(self.cursor()['native_notes'][uri])]
        first = self.candidate(uri)
        decision = state.propose(self.root, dict(candidate_id=first['candidate_id'], verified_by='fixture',
            verification_scope='Initial temporary note reviewed.', changes=[], skip_reason='No reusable knowledge to add.'))
        state.commit(self.root, decision['proposal_id'])
        path.write_bytes(original + b'\nACTUAL_BODY_REVISION\n')
        self.assertEqual(self.scan()['new_candidates'], 1)
        entries.append(dict(self.cursor()['native_notes'][uri]))
        path.unlink()
        self.assertEqual(self.scan()['new_candidates'], 1)
        entries.append(dict(self.cursor()['native_notes'][uri]))
        self.assertEqual(entries[-1]['revision'], 'missing')
        self.assertTrue(entries[-1]['removed'])
        self.assertEqual(self.scan()['new_candidates'], 0)
        path.write_bytes(original)
        self.assertEqual(self.scan()['new_candidates'], 1)
        entries.append(dict(self.cursor()['native_notes'][uri]))
        self.assertEqual(entries[0]['revision'], entries[-1]['revision'])
        self.assertFalse(entries[-1]['removed'])
        self.assertEqual(len({item['candidate_id'] for item in entries}), 4)
        with state._locked(self.context) as connection:
            rows = [connection.execute('SELECT source_id,source_epoch,status FROM candidates WHERE id=?', (item['candidate_id'],)).fetchone()
                    for item in entries]
        self.assertEqual([row['source_epoch'] for row in rows], [1, 2, 3, 4])
        self.assertEqual({row['source_id'] for row in rows}, {'native-memory.claude:' + uri})
        self.assertEqual(rows[-1]['status'], 'pending')

    def test_changed_source_after_catalog_does_not_advance_cursor_but_other_note_continues(self):
        changing, changing_uri = self.write_note('changing.md')
        healthy, healthy_uri = self.write_note('healthy.md')
        self.scan()
        previous = dict(self.cursor()['native_notes'][changing_uri])
        changing.write_text(changing.read_text() + '\nFIRST_CHANGE\n')
        healthy.write_text(healthy.read_text() + '\nHEALTHY_CHANGE\n')
        real_listing = notes.list_notes

        def change_after_listing(*args, **kwargs):
            catalog = real_listing(*args, **kwargs)
            changing.write_text(changing.read_text() + '\nRACE_CHANGE\n')
            return catalog

        with patch.object(notes, 'list_notes', side_effect=change_after_listing):
            result = self.scan()
        self.assertEqual(result['new_candidates'], 1)
        self.assertIn({'source': changing_uri, 'error_code': 'SOURCE_CHANGED'}, result['errors'])
        self.assertEqual(self.cursor()['native_notes'][changing_uri], previous)
        self.assertEqual(self.cursor()['native_notes'][healthy_uri]['revision'], hashlib.sha256(healthy.read_bytes()).hexdigest())
        self.assertEqual(self.scan()['new_candidates'], 1)

    def test_failed_capture_keeps_one_cursor_and_accepts_other_note(self):
        failed, failed_uri = self.write_note('failed.md')
        healthy, healthy_uri = self.write_note('healthy.md')
        self.scan()
        before = self.cursor()['native_notes']
        failed.write_text(failed.read_text() + '\nFAILED_VERSION\n')
        healthy.write_text(healthy.read_text() + '\nACCEPTED_VERSION\n')
        real_capture = state.capture

        def fail_one(workspace, payload):
            if payload['locator'] == failed_uri:
                raise MemoryProtocolError('FIXTURE_CAPTURE_FAILURE', 'Controlled temporary-source failure.')
            return real_capture(workspace, payload)

        with patch.object(state, 'capture', side_effect=fail_one):
            result = self.scan()
        after = self.cursor()['native_notes']
        self.assertEqual(result['new_candidates'], 1)
        self.assertEqual(result['errors'], [{'source': failed_uri, 'error_code': 'FIXTURE_CAPTURE_FAILURE'}])
        self.assertEqual(after[failed_uri], before[failed_uri])
        self.assertNotEqual(after[healthy_uri]['candidate_id'], before[healthy_uri]['candidate_id'])
        self.assertEqual(self.scan()['new_candidates'], 1)

    def test_sensitive_note_is_withheld_without_candidate_or_success_cursor(self):
        self.scan(tools=())
        _, sensitive_uri = self.write_note('sensitive.md', body='api_key=FICTIONAL_CREDENTIAL_123456789\n')
        _, healthy_uri = self.write_note('healthy.md')
        real_capture = state.capture
        with patch.object(state, 'capture', wraps=real_capture) as capture:
            result = self.scan()
        self.assertEqual(result['withheld'], 1)
        self.assertEqual(result['withheld_sources'], [{'locator': sensitive_uri, 'reason': 'NATIVE_MEMORY_SENSITIVE'}])
        self.assertEqual(result['errors'], [])
        self.assertEqual(result['new_candidates'], 1)
        self.assertNotIn(sensitive_uri, self.cursor()['native_notes'])
        self.assertIn(healthy_uri, self.cursor()['native_notes'])
        self.assertEqual([call.args[1]['locator'] for call in capture.call_args_list], [healthy_uri])
        self.assertNotIn('FICTIONAL_CREDENTIAL', json.dumps(result))

    def test_sensitive_change_keeps_previous_cursor_while_io_failure_remains_error(self):
        sensitive, sensitive_uri = self.write_note('sensitive.md')
        broken, broken_uri = self.write_note('broken.md')
        healthy, healthy_uri = self.write_note('healthy.md')
        self.scan()
        before = dict(self.cursor()['native_notes'][sensitive_uri])
        sensitive.write_text(sensitive.read_text() + '\napi_key=FICTIONAL_CREDENTIAL_123456789\n')
        broken.write_text(broken.read_text() + '\nCHANGED_BROKEN\n')
        healthy.write_text(healthy.read_text() + '\nCHANGED_HEALTHY\n')
        real_snapshot = notes.source_snapshot

        def fail_one(workspace, locator, **kwargs):
            if locator == broken_uri:
                raise OSError('Controlled fixture IO failure.')
            return real_snapshot(workspace, locator, **kwargs)

        with patch.object(notes, 'source_snapshot', side_effect=fail_one):
            result = self.scan()
        self.assertEqual(result['withheld'], 1)
        self.assertEqual(result['withheld_sources'], [{'locator': sensitive_uri, 'reason': 'NATIVE_MEMORY_SENSITIVE'}])
        self.assertEqual(result['errors'], [{'source': broken_uri, 'error_code': 'DISCOVERY_IO_ERROR'}])
        self.assertEqual(result['new_candidates'], 1)
        self.assertEqual(self.cursor()['native_notes'][sensitive_uri], before)
        self.assertEqual(self.cursor()['native_notes'][healthy_uri]['revision'], hashlib.sha256(healthy.read_bytes()).hexdigest())

    def test_source_forgetting_prevents_later_absence_recapture_and_error_loops(self):
        path, uri = self.write_note()
        self.scan()
        captured = self.candidate(uri)
        payload = captured['payload']
        source_id = payload['source_instance'] + ':' + payload['object_id']
        change = dict(fact_id='F-note-source', expected_version=None, title='Fixture note-derived fact',
            body='Verified temporary fixture knowledge.', kind='decision', scope=['repo:root'], status='active',
            sources=[dict(source_id=source_id, version=payload['source_revision'], locator=uri)],
            verified_at='2026-11-02', verification='Temporary note discovery and publication.', expires_when='Fixture changes.')
        proposal = state.propose(self.root, dict(candidate_id=captured['candidate_id'], verified_by='fixture',
            verification_scope='Temporary note evidence.', changes=[change]))
        state.commit(self.root, proposal['proposal_id'])
        old = state.inspect(self.root, 'F-note-source')
        control = state.capture(self.root, dict(source_instance='fixture-control', object_id='forget-note',
            source_revision='1', locator='fixture://forget-note', kind='observation',
            observed_at='2026-11-02T03:00:00+00:00', excerpt='Explicit source-forgetting fixture decision.'))
        change.update(expected_version=old['version'], body='', status='forgotten', sources=[], forget_sources=True)
        forget = state.propose(self.root, dict(candidate_id=control['candidate_id'], verified_by='fixture',
            verification_scope='Explicitly forget this fixture source.', changes=[change]))
        state.commit(self.root, forget['proposal_id'])
        self.assertEqual(notes.list_notes(self.root, tools=('claude',))['notes'], [])
        before = dict(self.cursor()['native_notes'][uri])
        for remove in (False, True):
            if remove:
                path.unlink()
            with patch.object(state, 'capture', wraps=state.capture) as capture:
                result = self.scan()
            self.assertEqual(result['new_candidates'], 0)
            self.assertEqual(result['errors'], [])
            self.assertEqual(result['native_notes_observed'], 0)
            self.assertEqual(self.cursor()['native_notes'][uri], before)
            capture.assert_not_called()

    def test_unavailable_root_is_not_a_missing_topic_observation(self):
        path, uri = self.write_note()
        self.scan()
        previous = dict(self.cursor()['native_notes'][uri])
        root = path.parent
        unavailable = root.with_name('unavailable-memory-route')
        root.rename(unavailable)
        result = self.scan()
        self.assertEqual(result['new_candidates'], 0)
        self.assertEqual(self.cursor()['native_notes'][uri], previous)
        route = result['coverage']['native_project_notes']['routes']['claude']['workspaces'][0]
        self.assertEqual(route['status'], 'NATIVE_MEMORY_ROOT_UNAVAILABLE')
        self.assertIn({'source': uri, 'error_code': 'NATIVE_MEMORY_ROOT_UNAVAILABLE'}, result['errors'])
        unavailable.rename(root)
        path.unlink()
        missing = self.scan()
        self.assertEqual(missing['new_candidates'], 1)
        self.assertEqual(missing['errors'], [])
        self.assertEqual(self.cursor()['native_notes'][uri]['revision'], 'missing')

    def test_legacy_cursor_preserves_files_sessions_and_adds_native_notes_only_on_capture(self):
        self.scan(tools=())
        receipt = state.capture(self.root, dict(source_instance='native-session.codex', object_id='previous',
            source_revision='fixture-activity', locator='codex://session/previous', kind='session_activity',
            observed_at='2026-11-02T03:00:00+00:00', excerpt='Prior accepted activity metadata.'))
        discovery._merge_cursor(self.context, 'sessions', 'codex:previous',
            dict(revision='fixture-activity', candidate_id=receipt['candidate_id'], observed_at='2026-11-02T03:00:00+00:00', removed=False))
        legacy = self.cursor()
        legacy.pop('native_notes')
        with state._locked(self.context) as connection:
            with connection:
                connection.execute("UPDATE meta SET value=? WHERE key='discovery_v1'", (json.dumps(legacy),))
        _, uri = self.write_note()
        result = self.scan()
        current = self.cursor()
        self.assertEqual(result['new_candidates'], 1)
        self.assertEqual(current['files'], legacy['files'])
        self.assertEqual(current['sessions'], legacy['sessions'])
        self.assertEqual(set(current['native_notes']), {uri})
        self.assertTrue(current['native_notes'][uri]['candidate_id'])


if __name__ == '__main__':
    unittest.main()
