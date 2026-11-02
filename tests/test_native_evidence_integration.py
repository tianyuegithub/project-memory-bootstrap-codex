"""Native evidence integration using temporary providers and the real ledger."""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from project_memory_bootstrap_codex import memory_state as state
from project_memory_bootstrap_codex import native_notes as notes
from project_memory_bootstrap_codex import native_sources as messages
from project_memory_bootstrap_codex.bootstrap import init_project
from project_memory_bootstrap_codex.errors import MemoryProtocolError
from project_memory_bootstrap_codex.memory_index import get_memory_by_id
from project_memory_bootstrap_codex.project import register_project


class NativeEvidenceIntegrationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.root = self.base / 'project'
        self.root.mkdir()
        self.registry = self.base / 'registry.json'
        environment = patch.dict(os.environ, PROJECT_MEMORY_REGISTRY=str(self.registry))
        environment.start()
        self.addCleanup(environment.stop)
        init_project(self.root)
        self.context = register_project(self.root, 'integration', {'root': '.'},
                                        maintenance_enabled=True, registry_path=self.registry)
        state.initialize_state(self.context, new_registration=True)
        self.message_base = self.base / 'messages'
        self.note_base = self.base / 'notes'
        providers = patch.object(messages, '_root', side_effect=lambda tool, home: self.message_base / tool)
        providers.start()
        self.addCleanup(providers.stop)
        note_providers = patch.object(notes, '_provider_base', side_effect=lambda tool: self.note_base / tool)
        note_providers.start()
        self.addCleanup(note_providers.stop)
        self.sequence = 0

    def write_message(self, *, session='s', message='m', body='Fixture public message.', workspace=None):
        path = self.message_base / 'codex/sessions' / (session + '.jsonl')
        path.parent.mkdir(parents=True, exist_ok=True)
        records = [
            {'type': 'session_meta', 'payload': {'id': session, 'cwd': str(workspace or self.root)}},
            {'type': 'response_item', 'payload': {'id': message, 'type': 'message', 'role': 'user', 'content': [
                {'type': 'input_text', 'text': body}, {'type': 'reasoning', 'text': 'HIDDEN_FIXTURE_REASONING'}]}}
        ]
        path.write_text(''.join(json.dumps(record) + '\n' for record in records), encoding='utf-8')
        return path, f'native-message://codex/{session}/{message}'

    def write_note(self, *, topic='topic.md', body='Fixture project note.\n', workspace=None):
        workspace = workspace or self.root
        slug = re.sub(r'[^a-zA-Z0-9]', '-', str(workspace))
        path = self.note_base / 'claude/projects' / slug / 'memory' / topic
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('---\nname: Fixture topic\ndescription: Fixture project evidence\ntype: project\n---\n' + body,
                        encoding='utf-8')
        return path, notes._locator('claude', workspace, topic)

    def snapshot(self, locator, *, allow_missing=False):
        if locator.startswith('native-message://'):
            return messages.source_snapshot(self.root, locator)
        return notes.source_snapshot(self.root, locator, allow_missing=allow_missing)

    def capture(self, locator, *, revision=None, namespace='fixture-ai', excerpt='First safe annotation.'):
        self.sequence += 1
        if revision is None:
            revision = self.snapshot(locator)['source_revision']
        return state.capture(self.root, dict(source_instance=namespace, object_id='agent-chosen-object',
            locator=locator, source_revision=revision, kind='observation',
            observed_at=f'2026-11-02T02:00:{self.sequence:02d}+00:00', excerpt=excerpt))

    def control(self):
        self.sequence += 1
        return state.capture(self.root, dict(source_instance='fixture-control', object_id=str(self.sequence),
            source_revision='1', locator='fixture://control', kind='observation',
            observed_at=f'2026-11-02T02:00:{self.sequence:02d}+00:00', excerpt='Explicit fixture decision.'))

    def change(self, fact_id, source, **extra):
        change = dict(fact_id=fact_id, expected_version=None, title='Fixture qualified fact',
            body='Qualified fixture knowledge.', kind='decision', scope=['repo:root'], status='active',
            sources=[dict(source_id=source['source_id'], version=source['source_revision'], locator=source['locator'])],
            verified_at='2026-11-02', verification='Temporary provider and real ledger integration.',
            expires_when='The fixture source changes.')
        change.update(extra)
        return change

    def propose(self, source, changes, **extra):
        return state.propose(self.root, dict(candidate_id=source['candidate_id'], verified_by='integration-fixture',
            verification_scope='Only the temporary provider fixture.', changes=changes, **extra))['proposal_id']

    def assertCode(self, code, callback):
        with self.assertRaises(MemoryProtocolError) as raised:
            callback()
        self.assertEqual(raised.exception.code, code)
        return raised.exception

    def test_native_uris_override_agent_namespaces_encoding_and_excerpt_without_duplication(self):
        _, message_uri = self.write_message()
        _, note_uri = self.write_note()
        variants = [(message_uri, 'native-message://codex/%73/%6d', 'native_message', 'native-message.codex'),
                    (note_uri, note_uri.replace('%2F', '%2f').replace('/topic.md', '/%74opic.md'),
                     'native_memory_note', 'native-memory.claude')]
        for canonical, variant, kind, instance in variants:
            with self.subTest(kind=kind):
                first = self.capture(canonical, namespace='ai-one')
                second = self.capture(variant, namespace='ai-two', excerpt='A different safe annotation.')
                self.assertEqual(first['candidate_id'], second['candidate_id'])
                self.assertEqual(first['source_id'], instance + ':' + canonical)
                self.assertEqual(first['source_id'], second['source_id'])
                self.assertTrue(second['duplicate'])
                stored = state.candidate(self.root, first['candidate_id'])['payload']
                self.assertEqual(stored['kind'], kind)
                self.assertEqual(stored['source_instance'], instance)
                self.assertEqual(stored['object_id'], canonical)
                self.assertEqual(stored['excerpt'], 'First safe annotation.')
                receipt = state.commit(self.root, self.propose(first, [self.change('F-' + kind, first)]))
                self.assertEqual(receipt['status'], 'committed')

    def test_changed_native_message_and_note_reject_previously_proposed_facts(self):
        for carrier in ('message', 'note'):
            with self.subTest(carrier=carrier):
                path, uri = self.write_message() if carrier == 'message' else self.write_note()
                source = self.capture(uri)
                proposal = self.propose(source, [self.change('F-stale-' + carrier, source)])
                if carrier == 'message':
                    self.write_message(body='Changed public fixture message.')
                else:
                    path.write_text(path.read_text() + '\nChanged note revision.\n')
                self.assertCode('SOURCE_CHANGED', lambda: state.commit(self.root, proposal))
                self.assertIn(source['candidate_id'], {row['candidate_id'] for row in state.pending(self.root)})
                self.assertIsNone(get_memory_by_id(self.root, 'F-stale-' + carrier))

    def test_note_absence_and_identical_restoration_create_epoch_three(self):
        path, uri = self.write_note()
        original = path.read_bytes()
        first = self.capture(uri)
        state.commit(self.root, self.propose(first, [self.change('F-note-first', first)]))
        path.unlink()
        missing = self.capture(uri, revision='missing')
        missing_proposal = self.propose(missing, [self.change('F-note-missing', missing)])
        self.assertEqual(missing['source_epoch'], 2)
        path.write_bytes(original)
        restored = self.capture(uri)
        self.assertEqual(restored['source_revision'], first['source_revision'])
        self.assertEqual(restored['source_epoch'], 3)
        self.assertNotEqual(restored['candidate_id'], first['candidate_id'])
        self.assertFalse(restored['duplicate'])
        self.assertEqual(restored['status'], 'pending')
        self.assertCode('SOURCE_CHANGED', lambda: state.commit(self.root, missing_proposal))
        state.commit(self.root, self.propose(missing, [], skip_reason='The fixture note reappeared; retire obsolete absence.'))
        self.assertIn(restored['candidate_id'], {row['candidate_id'] for row in state.pending(self.root)})
        self.assertNotIn(missing['candidate_id'], {row['candidate_id'] for row in state.pending(self.root)})

    def forget(self, source, fact_id, *, forget_sources):
        old = state.inspect(self.root, fact_id)
        control = self.control()
        change = self.change(fact_id, source, expected_version=old['version'], status='forgotten', body='',
                             sources=[], forget_sources=forget_sources)
        return state.commit(self.root, self.propose(control, [change]))

    def test_explicit_source_forgetting_blocks_message_and_note_exports(self):
        for carrier in ('message', 'note'):
            with self.subTest(carrier=carrier):
                _, uri = self.write_message() if carrier == 'message' else self.write_note()
                source = self.capture(uri)
                fact_id = 'F-forget-' + carrier
                state.commit(self.root, self.propose(source, [self.change(fact_id, source)]))
                receipt = self.forget(source, fact_id, forget_sources=True)
                self.assertIn(source['source_id'], receipt['suppressed_sources'])
                self.assertIsNone(get_memory_by_id(self.root, fact_id))
                self.assertCode('FACT_SUPPRESSED', lambda: state.inspect(self.root, fact_id))
                self.assertCode('SOURCE_SUPPRESSED', lambda: self.capture(uri))
                if carrier == 'message':
                    self.assertEqual(messages.read_messages(self.root, 'codex', 's'), [])
                    self.assertCode('SOURCE_SUPPRESSED', lambda: messages.read_messages(self.root, 'codex', 's', message_id='m'))
                else:
                    self.assertEqual(notes.list_notes(self.root, tools=('claude',))['notes'], [])
                    self.assertCode('SOURCE_SUPPRESSED', lambda: notes.read_note(self.root, uri))

    def test_fact_only_forgetting_preserves_native_source_visibility(self):
        for carrier in ('message', 'note'):
            with self.subTest(carrier=carrier):
                _, uri = self.write_message() if carrier == 'message' else self.write_note()
                source = self.capture(uri)
                fact_id = 'F-fact-only-' + carrier
                state.commit(self.root, self.propose(source, [self.change(fact_id, source)]))
                receipt = self.forget(source, fact_id, forget_sources=False)
                self.assertEqual(receipt['suppressed_sources'], [])
                self.assertIsNone(get_memory_by_id(self.root, fact_id))
                self.assertNotIn(source['source_id'], state.source_suppressions(self.context))
                if carrier == 'message':
                    exported = messages.read_messages(self.root, 'codex', 's', message_id='m')
                    self.assertEqual(exported[0]['text'], 'Fixture public message.')
                else:
                    self.assertEqual(len(notes.list_notes(self.root, tools=('claude',))['notes']), 1)
                    self.assertEqual(notes.read_note(self.root, uri)['text'], 'Fixture project note.\n')

    def test_state_loss_blocks_all_public_native_body_exports(self):
        _, message_uri = self.write_message()
        _, note_uri = self.write_note()
        self.capture(message_uri)
        self.capture(note_uri)
        ledger = self.context.state_dir / 'ledger.sqlite'
        ledger.unlink()
        self.assertCode('STATE_MISSING', lambda: messages.read_messages(self.root, 'codex', 's'))
        self.assertCode('STATE_MISSING', lambda: messages.read_messages(self.root, 'codex', 's', message_id='m'))
        self.assertCode('STATE_MISSING', lambda: notes.list_notes(self.root, tools=('claude',)))
        self.assertCode('STATE_MISSING', lambda: notes.read_note(self.root, note_uri))
        self.assertFalse(ledger.exists())

    def test_sensitive_sources_do_not_leak_and_cross_project_uris_are_rejected(self):
        fictional = 'FICTIONAL_CREDENTIAL_VALUE_12345'
        _, message_uri = self.write_message(body='password="' + fictional + '"')
        exported = messages.read_messages(self.root, 'codex', 's', message_id='m')
        self.assertEqual(exported[0]['status'], 'sensitive_text_omitted')
        self.assertNotIn('text', exported[0])
        self.assertCode('NATIVE_MESSAGE_SENSITIVE', lambda: self.capture(message_uri, revision=exported[0]['source_revision']))
        _, note_uri = self.write_note(body='密码：`' + fictional + '`\n')
        note_export = notes.read_note(self.root, note_uri)
        self.assertTrue(note_export['sensitive_text_omitted'])
        self.assertNotIn('text', note_export)
        self.assertNotIn(fictional, json.dumps([exported, note_export, state.pending(self.root)]))
        foreign = self.base / 'other-project'
        foreign.mkdir()
        register_project(foreign, 'other', {'root': '.'}, registry_path=self.registry)
        path, foreign_note = self.write_note(workspace=foreign)
        self.assertCode('NATIVE_MEMORY_SCOPE_MISMATCH', lambda: self.capture(foreign_note, revision=hashlib.sha256(path.read_bytes()).hexdigest()))
        self.write_message(session='foreign', workspace=foreign)
        self.assertCode('NATIVE_MESSAGE_UNAVAILABLE', lambda: self.capture('native-message://codex/foreign/m', revision='untrusted-revision'))
        self.assertEqual(state.pending(self.root), [])

    def test_unverified_observation_still_rejects_same_version_different_excerpt(self):
        first = self.control()
        stored = state.candidate(self.root, first['candidate_id'])['payload']
        changed = dict(stored, observed_at='2026-11-02T03:00:00+00:00', excerpt='Different unverified annotation.')
        self.assertCode('SOURCE_VERSION_CONFLICT', lambda: state.capture(self.root, changed))
        self.assertEqual(state.candidate(self.root, first['candidate_id'])['payload'], stored)


if __name__ == '__main__':
    unittest.main()
