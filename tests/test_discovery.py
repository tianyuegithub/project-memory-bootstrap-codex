import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from project_memory_bootstrap_codex import discovery
from project_memory_bootstrap_codex import memory_state as state
from project_memory_bootstrap_codex import maintenance
from project_memory_bootstrap_codex.bootstrap import init_project
from project_memory_bootstrap_codex.errors import MemoryProtocolError
from project_memory_bootstrap_codex.project import register_project


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.root = self.base / 'project'
        self.root.mkdir()
        self.registry = self.base / 'registry.json'
        env = patch.dict(os.environ, PROJECT_MEMORY_REGISTRY=str(self.registry))
        env.start()
        self.addCleanup(env.stop)
        init_project(self.root)
        self.context = register_project(self.root, 'fixture', {'root': '.', 'member': 'member'}, maintenance_enabled=True, registry_path=self.registry)
        (self.root / 'member').mkdir()
        state.initialize_state(self.context, new_registration=True)
        self.provider = self.base / 'provider'
        self.provider.mkdir()

    def scan(self, **kwargs):
        kwargs.setdefault('tools', ())
        return discovery.scan_sources(self.root, **kwargs)

    def cursor(self):
        with state._locked(self.context) as connection:
            return discovery._cursor(connection)

    def payloads(self):
        return [state.candidate(self.root, item['candidate_id'])['payload'] for item in state.pending(self.root)]

    def session(self, session_id='s', cwd=None):
        path = self.provider / 'sessions' / (session_id + '.jsonl')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({'type': 'session_meta', 'payload': {'id': session_id, 'cwd': str(cwd or self.root)}}) + '\n' + '{malformed body intentionally not read}\n')
        return path

    def test_replay_zero_new_candidates_and_native_metadata_only(self):
        self.session(cwd=self.root / 'member')
        with patch('project_memory_bootstrap_codex.native_sources.read_messages', side_effect=AssertionError('must not read messages')):
            first = self.scan(tools=('codex',), provider_roots={'codex': self.provider})
            second = self.scan(tools=('codex',), provider_roots={'codex': self.provider})
        self.assertGreater(first['new_candidates'], 0)
        self.assertEqual(first['sessions_observed'], 1)
        self.assertEqual(first['errors'], [])
        self.assertEqual(second['new_candidates'], 0)
        self.assertEqual(second['unchanged'], first['files_observed'] + 1)
        candidates = self.payloads()
        self.assertTrue(any(candidate['kind'] == 'session_activity' for candidate in candidates))
        self.assertNotIn('malformed body', json.dumps(candidates))
        self.assertIn('源码文件', second['coverage']['not_covered'])

    def test_internal_maintenance_session_is_not_captured(self):
        self.session()
        maintenance.begin(self.root, 'codex', 's')
        result = self.scan(tools=('codex',), provider_roots={'codex': self.provider})
        self.assertEqual(result['internal_sessions_skipped'], 1)
        self.assertEqual(result['sessions_observed'], 1)
        self.assertFalse(any(candidate['kind'] == 'session_activity' for candidate in self.payloads()))
        self.assertEqual(self.cursor()['sessions'], {})

    def test_file_changes_and_removal_keep_one_source_identity(self):
        path = self.root / 'docs/需求.md'
        path.parent.mkdir(exist_ok=True)
        path.write_text('first owner version')
        self.scan()
        path.write_text('second owner version')
        changed = self.scan()
        self.assertEqual(changed['new_candidates'], 1)
        path.unlink()
        removed = self.scan()
        self.assertEqual(removed['new_candidates'], 1)
        removed_candidates = [candidate for candidate in self.payloads() if candidate['source_revision'] == 'missing']
        self.assertEqual(len(removed_candidates), 1)
        self.assertEqual(removed_candidates[0]['source_instance'], 'file')
        self.assertEqual(removed_candidates[0]['kind'], 'file')
        self.assertTrue(self.cursor()['files'][str(path)]['removed'])
        self.assertEqual(removed_candidates[0]['locator'], str(path))
        self.assertNotIn('excerpt', removed_candidates[0])
        self.assertEqual(self.scan()['new_candidates'], 0)

    def test_restored_identical_file_gets_epoch_three_and_invalidates_deletion(self):
        path = self.root / 'docs/需求.md'
        path.parent.mkdir(exist_ok=True)
        path.write_text('owner version A')
        self.scan()
        first = self.cursor()['files'][str(path)]
        initial_decision = state.propose(self.root, {'candidate_id': first['candidate_id'], 'verified_by': 'fixture',
            'verification_scope': 'Reviewed owner version A.', 'changes': [], 'skip_reason': 'No new knowledge in initial owner version.'})
        state.commit(self.root, initial_decision['proposal_id'])
        path.unlink()
        self.assertEqual(self.scan()['new_candidates'], 1)
        deletion = self.cursor()['files'][str(path)]
        deletion_proposal = state.propose(self.root, {'candidate_id': deletion['candidate_id'], 'verified_by': 'fixture',
            'verification_scope': 'Observed the owner file absent.', 'changes': [{
                'fact_id': 'F-file-absent', 'expected_version': None, 'title': 'Owner file is absent',
                'body': 'The fixture owner file was observed absent.', 'kind': 'decision', 'scope': ['repo:root'],
                'status': 'active', 'sources': [{'source_id': 'file:' + str(path), 'version': 'missing', 'locator': str(path)}],
                'verified_at': '2026-11-02', 'verification': 'Fixture file absence observation.',
                'expires_when': 'The owner file reappears.'}]})
        path.write_text('owner version A')
        restored_scan = self.scan()
        restored = self.cursor()['files'][str(path)]
        self.assertEqual(restored_scan['new_candidates'], 1)
        self.assertEqual(restored['revision'], first['revision'])
        self.assertNotEqual(restored['candidate_id'], first['candidate_id'])
        self.assertFalse(restored['removed'])
        with state._locked(self.context) as connection:
            restored_row = connection.execute('SELECT source_epoch,status FROM candidates WHERE id=?', (restored['candidate_id'],)).fetchone()
        self.assertEqual(tuple(restored_row), (3, 'pending'))
        with self.assertRaises(MemoryProtocolError) as raised:
            state.commit(self.root, deletion_proposal['proposal_id'])
        self.assertEqual(raised.exception.code, 'SOURCE_CHANGED')
        retired = state.propose(self.root, {'candidate_id': deletion['candidate_id'], 'verified_by': 'fixture',
            'verification_scope': 'Compared absence with restored owner version A.', 'changes': [],
            'skip_reason': 'The owner file has reappeared; retire the stale absence candidate.'})
        self.assertEqual(state.commit(self.root, retired['proposal_id'])['facts'], [])
        pending_ids = {item['candidate_id'] for item in state.pending(self.root)}
        self.assertNotIn(deletion['candidate_id'], pending_ids)
        self.assertIn(restored['candidate_id'], pending_ids)

    def test_failed_source_keeps_cursor_and_other_sources_continue(self):
        self.scan()
        readme = self.root / 'README.md'
        readme.write_text('changed')
        previous = self.cursor()['files'][str(readme)]
        agent = self.root / 'AGENTS.md'
        agent.write_text(agent.read_text() + '\nowner changed\n')
        real_capture = state.capture

        def fail_one(workspace, payload):
            if payload['locator'] == str(readme):
                raise MemoryProtocolError('FIXTURE_CAPTURE_FAILURE', 'controlled')
            return real_capture(workspace, payload)

        with patch.object(state, 'capture', side_effect=fail_one):
            result = self.scan(tools=('unknown-provider', 'codex'), provider_roots={'codex': self.provider})
        self.assertEqual(result['new_candidates'], 1)
        self.assertEqual({error['error_code'] for error in result['errors']}, {'FIXTURE_CAPTURE_FAILURE', 'NATIVE_SOURCE_UNSUPPORTED'})
        self.assertEqual(self.cursor()['files'][str(readme)], previous)
        self.assertEqual(self.scan()['new_candidates'], 1)

    def test_crash_after_capture_replays_receipt_without_loss_or_duplicate(self):
        with patch.object(discovery, '_merge_cursor', side_effect=RuntimeError('simulated process termination')):
            with self.assertRaises(RuntimeError):
                self.scan()
        accepted = len(state.pending(self.root))
        self.assertEqual(accepted, 1)
        self.assertEqual(self.cursor()['files'], {})
        result = self.scan()
        self.assertEqual(result['new_candidates'], result['files_observed'] - accepted)
        self.assertEqual(len(state.pending(self.root)), result['files_observed'])
        self.assertEqual(len(self.cursor()['files']), result['files_observed'])

    def test_scope_reads_only_owners_and_keeps_raw_text_out_of_ledger(self):
        sensitive = self.root / '.env'
        source = self.root / 'src/business.py'
        source.parent.mkdir()
        sensitive.write_text('secret=not-to-read-12345')
        source.write_text('never read business source')
        archive = self.root / 'openspec/changes/archive/old/spec.md'
        archive.parent.mkdir(parents=True)
        archive.write_text('owner contract text not stored')
        real_read_bytes = Path.read_bytes
        seen = []

        def read_bytes(path):
            seen.append(path)
            if path in {sensitive, source}:
                raise AssertionError('outside owner scope')
            return real_read_bytes(path)

        with patch.object(Path, 'read_bytes', read_bytes):
            result = self.scan()
        self.assertEqual(result['errors'], [])
        self.assertIn(archive, seen)
        self.assertNotIn(sensitive, seen)
        self.assertNotIn(source, seen)
        stored = json.dumps(self.payloads()) + json.dumps(self.cursor())
        self.assertNotIn('owner contract text not stored', stored)
        self.assertNotIn('secret=not-to-read', stored)

    def test_requires_local_registration_and_enabled_maintenance(self):
        register_project(self.root, 'fixture', {'root': '.'}, maintenance_enabled=False, registry_path=self.registry)
        with self.assertRaises(MemoryProtocolError) as raised:
            self.scan()
        self.assertEqual(raised.exception.code, 'MAINTENANCE_DISABLED')
        self.registry.unlink()
        with self.assertRaises(MemoryProtocolError) as raised:
            self.scan()
        self.assertEqual(raised.exception.code, 'PROJECT_UNREGISTERED')

    def test_cursor_merge_preserves_other_entries_and_rejects_older_scan(self):
        newer = dict(revision='new', candidate_id='candidate-new', observed_at='2026-11-02T02:00:00+00:00', removed=False)
        older = dict(revision='old', candidate_id='candidate-old', observed_at='2026-11-02T01:00:00+00:00', removed=False)
        discovery._merge_cursor(self.context, 'sessions', 'codex:one', newer)
        discovery._merge_cursor(self.context, 'sessions', 'claude:two', older)
        discovery._merge_cursor(self.context, 'sessions', 'codex:one', older)
        cursor = self.cursor()['sessions']
        self.assertEqual(cursor['codex:one'], newer)
        self.assertEqual(cursor['claude:two'], older)

    def test_zcode_activity_uses_session_time_not_message_body(self):
        path = self.provider / 'cli/db/db.sqlite'
        path.parent.mkdir(parents=True)
        with sqlite3.connect(path) as db:
            db.execute('CREATE TABLE session(id TEXT,directory TEXT,time_updated INTEGER,time_deleted INTEGER)')
            db.execute('INSERT INTO session VALUES (?,?,1000,NULL)', ('s/id', str(self.root)))
        first = self.scan(tools=('zcode',), provider_roots={'zcode': self.provider})
        self.assertEqual(first['sessions_observed'], 1)
        candidate = next(candidate for candidate in self.payloads() if candidate['kind'] == 'session_activity')
        self.assertEqual(candidate['source_revision'], '1000')
        self.assertEqual(candidate['locator'], 'zcode://session/s%2Fid')
        with sqlite3.connect(path) as db:
            db.execute('UPDATE session SET time_updated=2000')
        self.assertEqual(self.scan(tools=('zcode',), provider_roots={'zcode': self.provider})['new_candidates'], 1)

    def test_workbuddy_deletion_and_restore_change_the_same_source_epoch(self):
        database = self.provider / 'workbuddy.db'
        with sqlite3.connect(database) as db:
            db.execute('CREATE TABLE sessions(id TEXT,cwd TEXT,deleted_at INTEGER,updated_at INTEGER)')
            db.execute('INSERT INTO sessions VALUES (?,?,NULL,1000)', ('wb-one', str(self.root)))
        transcript = self.provider / 'projects/fixture/wb-one.jsonl'
        transcript.parent.mkdir(parents=True)
        transcript.write_text(json.dumps({'type': 'session-meta', 'sessionId': 'wb-one', 'cwd': str(self.root)}) + '\n')
        self.scan(tools=('workbuddy',), provider_roots={'workbuddy': self.provider})
        first = next(x for x in state.pending(self.root) if x['source_id'] == 'native-session.workbuddy:wb-one')
        proposal = state.propose(self.root, {'candidate_id': first['candidate_id'], 'verified_by': 'fixture',
            'verification_scope': 'Controlled activity only.', 'changes': [], 'skip_reason': 'No knowledge in fixture activity.'})
        state.commit(self.root, proposal['proposal_id'])
        with sqlite3.connect(database) as db:
            db.execute('UPDATE sessions SET deleted_at=2000,updated_at=2000')
        original_open = Path.open

        def guarded_open(path, *args, **kwargs):
            if path == transcript:
                raise AssertionError('Deleted native transcript must not be opened.')
            return original_open(path, *args, **kwargs)

        with patch.object(Path, 'open', guarded_open):
            result = self.scan(tools=('workbuddy',), provider_roots={'workbuddy': self.provider})
        self.assertEqual(result['errors'], [])
        deleted = next(x for x in state.pending(self.root) if x['source_id'] == first['source_id'])
        self.assertEqual(deleted['source_revision'], 'deleted:2000')
        with state._locked(self.context) as db:
            self.assertEqual(db.execute('SELECT source_epoch FROM candidates WHERE id=?', (deleted['candidate_id'],)).fetchone()[0], 2)
        with sqlite3.connect(database) as db:
            db.execute('UPDATE sessions SET deleted_at=NULL,updated_at=3000')
        self.scan(tools=('workbuddy',), provider_roots={'workbuddy': self.provider})
        restored = next(x for x in state.pending(self.root) if x['source_id'] == first['source_id'] and x['source_revision'] == first['source_revision'])
        with state._locked(self.context) as db:
            self.assertEqual(db.execute('SELECT source_epoch FROM candidates WHERE id=?', (restored['candidate_id'],)).fetchone()[0], 3)
        self.assertEqual(restored['source_revision'], first['source_revision'])
        self.assertNotEqual(restored['candidate_id'], first['candidate_id'])


if __name__ == '__main__':
    unittest.main()
