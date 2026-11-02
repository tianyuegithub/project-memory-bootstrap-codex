import json
import os
import sqlite3
import subprocess
import tempfile
import unittest
from urllib.parse import quote
from pathlib import Path
from unittest.mock import patch

from project_memory_bootstrap_codex.errors import MemoryProtocolError
from project_memory_bootstrap_codex import native_sources
from project_memory_bootstrap_codex.native_sources import list_sessions, read_messages, source_snapshot
from project_memory_bootstrap_codex.project import register_project
from project_memory_bootstrap_codex.bootstrap import init_project
from project_memory_bootstrap_codex.memory_state import initialize_state


class NativeSourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.workspace = self.base / 'project'
        self.workspace.mkdir()
        (self.workspace / 'member').mkdir()
        self.registry = self.base / 'registry.json'
        init_project(self.workspace)
        context = register_project(self.workspace, 'test', {'root': '.', 'member': 'member'}, registry_path=self.registry)
        initialize_state(context, new_registration=True)
        self.env = patch.dict(os.environ, PROJECT_MEMORY_REGISTRY=str(self.registry))
        self.env.start()
        self.addCleanup(self.env.stop)
        self.home = self.base / 'provider'
        self.home.mkdir()
        with sqlite3.connect(self.home / 'workbuddy.db') as db:
            db.execute('CREATE TABLE sessions(id TEXT PRIMARY KEY,cwd TEXT,deleted_at TEXT,updated_at TEXT)')

    def write(self, relative, rows, tail=''):
        path = self.home / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(''.join(json.dumps(row) + '\n' for row in rows) + tail)
        return path

    def read(self, tool, session='s', **kwargs):
        return read_messages(self.workspace, tool, session, home=self.home, **kwargs)

    def test_codex_public_only_stable_ids_partial_line_and_sensitive(self):
        rows = [{'type': 'session_meta', 'payload': {'id': 's', 'cwd': str(self.workspace)}}]
        for role, content in [('user', [{'type': 'input_text', 'text': 'public'}]),
                              ('assistant', [{'type': 'reasoning', 'text': 'hidden'}, {'type': 'output_text', 'text': 'answer'}]),
                              ('system', [{'type': 'output_text', 'text': 'hidden system'}]),
                              ('assistant', [{'type': 'output_text', 'text': 'token=abcdefgh12345678'}])]:
            rows.append({'timestamp': 'now', 'type': 'response_item', 'payload': {'type': 'message', 'role': role, 'content': content}})
        for field, value in [('phase', 'analysis'), ('channel', 'reasoning')]:
            rows.append({'type': 'response_item', 'payload': {'type': 'message', 'role': 'assistant', field: value, 'content': [{'type': 'output_text', 'text': 'hidden phase'}]}})
        self.write('sessions/date/s.jsonl', rows, '{"unfinished":')
        messages = self.read('codex')
        self.assertEqual([m.get('text') for m in messages], ['public', 'answer', None])
        self.assertEqual(messages[-1]['status'], 'sensitive_text_omitted')
        self.assertNotIn('abcdefgh', json.dumps(messages))
        self.assertNotIn('hidden', json.dumps(messages))
        self.assertEqual(messages[0]['message_id'], 'now:2')
        self.assertEqual(self.read('codex', message_id='now:2'), messages[:1])
        self.assertEqual(messages, self.read('codex'))

    def test_codex_empty_session_is_listed_without_fabricated_text(self):
        self.write('sessions/empty.jsonl', [{'type': 'session_meta', 'payload': {'id': 's', 'cwd': str(self.workspace)}}])
        self.assertEqual([s['session_id'] for s in list_sessions(self.workspace, 'codex', home=self.home)], ['s'])
        self.assertEqual(self.read('codex'), [])

    def test_codex_metadata_cannot_change_project(self):
        foreign = self.base / 'project-other'
        foreign.mkdir()
        register_project(foreign, 'foreign', {'root': '.'}, registry_path=self.registry)
        self.write('sessions/s.jsonl', [
            {'type': 'session_meta', 'payload': {'id': 's', 'cwd': str(foreign)}},
            {'type': 'session_meta', 'payload': {'id': 's', 'cwd': str(self.workspace)}},
            {'type': 'response_item', 'payload': {'type': 'message', 'role': 'user', 'cwd': str(self.workspace), 'content': [{'type': 'input_text', 'text': 'foreign'}]}}
        ])
        self.assertEqual(self.read('codex'), [])
        self.assertEqual(list_sessions(self.workspace, 'codex', home=self.home), [])

    def test_claude_record_identity_member_and_exclusions(self):
        def row(identifier, **extra):
            return dict(type='assistant', sessionId='s', uuid=identifier,
                        cwd=str(self.workspace / 'member'), message={'role': 'assistant', 'content': [{'type': 'thinking', 'thinking': 'HIDDEN_THINKING_MARKER'}, {'type': 'text', 'text': 'visible'}]}, **extra)
        records = [row('ok'), row('meta', isMeta=True), row('side', isSidechain=True)]
        records.append({**row('wrong-role'), 'message': {'role': 'user', 'content': 'hidden'}})
        records.append({**row('foreign'), 'cwd': str(self.base / 'project-other')})
        self.write('projects/arbitrary/s.jsonl', records)
        self.write('projects/arbitrary/subagents/child.jsonl', [row('child')])
        self.assertEqual([m['message_id'] for m in self.read('claude')], ['ok'])
        summary = list_sessions(self.workspace, 'claude', home=self.home)[0]
        self.assertEqual(summary['actual_workspace'], str(self.workspace / 'member'))
        self.assertEqual(summary['repository_id'], 'member')
        self.assertNotIn('HIDDEN_THINKING_MARKER', json.dumps(self.read('claude')))

    def test_codex_archived_recall_and_active_session_identity_deduplication(self):
        meta = {'type': 'session_meta', 'payload': {'id': 's', 'cwd': str(self.workspace)}}
        def message(text):
            return {'type': 'response_item', 'payload': {'type': 'message', 'role': 'assistant', 'content': [
                {'type': 'reasoning', 'text': 'HIDDEN_ARCHIVE_REASONING'}, {'type': 'output_text', 'text': text}]}}
        archived = self.write('archived_sessions/rollout-s.jsonl', [meta, message('archived public')])
        summaries = list_sessions(self.workspace, 'codex', home=self.home)
        self.assertEqual(len(summaries), 1)
        self.assertTrue(summaries[0]['archived'])
        self.assertEqual([item['text'] for item in self.read('codex')], ['archived public'])
        active = self.write('sessions/date/rollout-s.jsonl', [meta, message('active public')])
        summaries = list_sessions(self.workspace, 'codex', home=self.home)
        self.assertEqual(len(summaries), 1)
        self.assertFalse(summaries[0]['archived'])
        self.assertEqual(summaries[0]['locator'], str(active))
        self.assertEqual([item['text'] for item in self.read('codex')], ['active public'])
        self.assertNotIn('HIDDEN_', json.dumps(self.read('codex')))
        self.assertTrue(archived.is_file())

    def test_workbuddy_deleted_indexed_session_does_not_open_jsonl(self):
        self.write('projects/p/s.jsonl', [], '{bad complete deleted body}\n')
        with sqlite3.connect(self.home / 'workbuddy.db') as db:
            db.execute('INSERT INTO sessions VALUES (?,?,?,?)', ('s', str(self.workspace), '2026-11-01', '2026-11-02'))
        with patch.object(native_sources, '_records', side_effect=AssertionError('deleted body must remain unread')):
            summaries = list_sessions(self.workspace, 'workbuddy', home=self.home)
            with self.assertRaises(MemoryProtocolError) as raised:
                self.read('workbuddy')
        self.assertEqual(raised.exception.code, 'NATIVE_SOURCE_INACTIVE')
        self.assertEqual(len(summaries), 1)
        self.assertEqual(summaries[0]['source_state'], 'deleted')
        self.assertEqual(summaries[0]['deleted_at'], '2026-11-01')
        self.assertNotIn('text', summaries[0])

    def test_workbuddy_owned_active_index_without_jsonl_is_unavailable_to_read(self):
        foreign = self.base / 'foreign'
        foreign.mkdir()
        register_project(foreign, 'foreign', {'root': '.'}, registry_path=self.registry)
        with sqlite3.connect(self.home / 'workbuddy.db') as db:
            db.execute('INSERT INTO sessions VALUES (?,?,NULL,?)', ('s', str(self.workspace), '2026-11-02'))
            db.execute('INSERT INTO sessions VALUES (?,?,NULL,?)', ('foreign', str(foreign), '2026-11-02'))
        summaries = list_sessions(self.workspace, 'workbuddy', home=self.home)
        self.assertEqual([item['session_id'] for item in summaries], ['s'])
        self.assertEqual(summaries[0]['source_state'], 'active')
        self.assertEqual(summaries[0]['locator'], str(self.home / 'workbuddy.db'))
        with self.assertRaises(MemoryProtocolError) as raised:
            self.read('workbuddy')
        self.assertEqual(raised.exception.code, 'NATIVE_SOURCE_UNAVAILABLE')
        self.assertEqual(self.read('workbuddy', session='unknown'), [])
        self.assertEqual(self.read('workbuddy', session='foreign'), [])

    def test_workbuddy_requires_index_and_marks_unindexed_cli_sessions(self):
        path = self.write('projects/p/s.jsonl', [{'type': 'message', 'sessionId': 's', 'id': 'm', 'cwd': str(self.workspace),
                                               'role': 'user', 'content': [{'type': 'input_text', 'text': 'public CLI'}]}])
        summaries = list_sessions(self.workspace, 'workbuddy', home=self.home)
        self.assertEqual(summaries[0]['source_state'], 'unindexed')
        self.assertEqual([item['text'] for item in self.read('workbuddy')], ['public CLI'])
        with sqlite3.connect(self.home / 'workbuddy.db') as db:
            db.execute('INSERT INTO sessions VALUES (?,?,NULL,?)', ('s', str(self.workspace), '2026-11-02'))
        indexed = list_sessions(self.workspace, 'workbuddy', home=self.home)[0]
        self.assertEqual(indexed['source_state'], 'active')
        self.assertEqual(indexed['locator'], str(path))
        self.assertEqual([item['text'] for item in self.read('workbuddy')], ['public CLI'])
        (self.home / 'workbuddy.db').unlink()
        with patch.object(native_sources, '_records', side_effect=AssertionError('unverified index state')):
            for operation in [lambda: list_sessions(self.workspace, 'workbuddy', home=self.home), lambda: self.read('workbuddy')]:
                with self.assertRaises(MemoryProtocolError) as raised:
                    operation()
                self.assertEqual(raised.exception.code, 'NATIVE_SOURCE_UNAVAILABLE')
        self.assertTrue(path.is_file())
        with sqlite3.connect(self.home / 'workbuddy.db') as db:
            db.execute('CREATE TABLE unknown_index(id TEXT)')
        with self.assertRaises(MemoryProtocolError) as raised:
            self.read('workbuddy')
        self.assertEqual(raised.exception.code, 'NATIVE_SOURCE_FORMAT_INVALID')

    def test_workbuddy_index_cwd_must_agree_and_cannot_override_foreign_ownership(self):
        self.write('projects/p/s.jsonl', [{'type': 'message', 'sessionId': 's', 'id': 'm', 'cwd': str(self.workspace),
                                         'role': 'user', 'content': [{'type': 'input_text', 'text': 'public'}]}])
        foreign = self.base / 'foreign'
        foreign.mkdir()
        register_project(foreign, 'foreign', {'root': '.'}, registry_path=self.registry)
        with sqlite3.connect(self.home / 'workbuddy.db') as db:
            db.execute('INSERT INTO sessions VALUES (?,?,NULL,?)', ('s', str(foreign), '2026-11-02'))
        self.assertEqual(self.read('workbuddy'), [])
        self.assertEqual(list_sessions(self.workspace, 'workbuddy', home=self.home), [])
        with sqlite3.connect(self.home / 'workbuddy.db') as db:
            db.execute('UPDATE sessions SET cwd=?', (str(self.workspace / 'member'),))
        with self.assertRaises(MemoryProtocolError) as raised:
            self.read('workbuddy')
        self.assertEqual(raised.exception.code, 'NATIVE_SOURCE_FORMAT_INVALID')

    def test_workbuddy_current_user_query_projection_excludes_host_context(self):
        query = 'Remember fixture-marker-419 only within this isolated fixture; no business or Git changes.'
        hidden = '<system-reminder data-role="identity"><identity_context>HOST_CONTEXT_NOT_PUBLIC</identity_context></system-reminder>\n'
        hidden += '<system-reminder data-role="project"><project_guidance>password policy requires curated evidence</project_guidance></system-reminder>\n'
        record = dict(type='message', sessionId='s', id='m', cwd=str(self.workspace), role='user',
                      providerData={'startsNewUserRequest': True, 'agent': 'test', 'conversationRequestId': 'fixture-request'},
                      content=[{'type': 'input_text', 'text': hidden + '<user_query>' + query + '</user_query>'}])
        path = self.write('projects/p/s.jsonl', [record,
            {'type': 'function_call_result', 'sessionId': 's', 'cwd': str(self.workspace), 'output': 'TOOL_OUTPUT_NOT_PUBLIC'},
            {'type': 'reasoning', 'sessionId': 's', 'cwd': str(self.workspace), 'rawContent': 'REASONING_NOT_PUBLIC'}])
        message = self.read('workbuddy')[0]
        self.assertEqual(message['text'], query)
        self.assertEqual(message['projection_type'], 'workbuddy_user_query')
        self.assertEqual(message['record_locator'], str(path) + '#line=1')
        self.assertEqual(message['locator'], 'native-message://workbuddy/s/m')
        self.assertNotIn('HOST_CONTEXT_NOT_PUBLIC', json.dumps(message))
        self.assertNotIn('TOOL_OUTPUT_NOT_PUBLIC', json.dumps(message))
        self.assertNotIn('REASONING_NOT_PUBLIC', json.dumps(message))
        record['content'][0]['text'] = hidden.replace('HOST_CONTEXT_NOT_PUBLIC', 'DIFFERENT_HOST_CONTEXT') + '<user_query>' + query + '</user_query>'
        self.write('projects/p/s.jsonl', [record])
        self.assertEqual(self.read('workbuddy')[0]['source_revision'], message['source_revision'])
        with patch.object(native_sources, '_root', return_value=self.home):
            snapshot = source_snapshot(self.workspace, message['locator'])
        self.assertEqual(snapshot['source_revision'], message['source_revision'])

    def test_workbuddy_query_projection_preserves_sensitive_original_and_unknown_envelopes(self):
        prefix = '<system-reminder data-role="project">password policy in host context</system-reminder>'
        cases = [
            ('<user_query>password policy in the original user text</user_query>', {'startsNewUserRequest': True}),
            ('<user_query>safe</user_query><user_query>password policy</user_query>', {'startsNewUserRequest': True}),
            ('<user_query><auxiliary>host context</auxiliary>safe</user_query>', {'startsNewUserRequest': True}),
            ('<user_query>safe</user_query>', {'startsNewUserRequest': False}),
            ('<user_query>safe</user_query>', {'startsNewUserRequest': True, 'isAdditionalData': True}),
            ('<user_query>safe</user_query>unclassified suffix', {'startsNewUserRequest': True}),
        ]
        for index, (content, provider) in enumerate(cases):
            self.write('projects/p/s.jsonl', [dict(type='message', sessionId='s', id='m', cwd=str(self.workspace), role='user',
                providerData=provider, content=[{'type': 'input_text', 'text': prefix + content}])])
            with self.subTest(example=index):
                message = self.read('workbuddy')[0]
                self.assertNotIn('text', message)
                self.assertEqual(message['reason'], 'credential_context_requires_curated_evidence')
        quoted = '<system-reminder data-role="conversation">password policy; quoted <user_query>safe excerpt</user_query></system-reminder>'
        self.write('projects/p/s.jsonl', [dict(type='message', sessionId='s', id='m', cwd=str(self.workspace), role='user',
            providerData={'startsNewUserRequest': True}, content=[{'type': 'input_text', 'text': quoted}])])
        self.assertNotIn('text', self.read('workbuddy')[0])

    def test_workbuddy_file_name_not_project_evidence(self):
        self.write('projects/encoded-project/s.jsonl', [
            {'type': 'message', 'sessionId': 's', 'id': 'm', 'cwd': str(self.workspace), 'role': 'user', 'content': [{'type': 'input_text', 'text': 'hello'}, {'type': 'reasoning', 'text': 'hidden'}]},
            {'type': 'message', 'sessionId': 's', 'id': 'foreign', 'cwd': str(self.base / 'project-other'), 'role': 'assistant', 'content': [{'type': 'output_text', 'text': 'foreign'}]}
        ])
        self.assertEqual([m['text'] for m in self.read('workbuddy')], ['hello'])

    def test_listing_uses_only_headers_and_never_builds_messages(self):
        fixtures = [
            ('codex', 'sessions/s.jsonl', {'type': 'session_meta', 'payload': {'id': 's', 'cwd': str(self.workspace)}}),
            ('claude', 'projects/c/s.jsonl', {'sessionId': 's', 'cwd': str(self.workspace)}),
            ('workbuddy', 'projects/w/s.jsonl', {'sessionId': 's', 'cwd': str(self.workspace)}),
        ]
        for tool, path, header in fixtures:
            with self.subTest(tool=tool):
                source = self.write(path, [header], '{bad complete json}\n')
                with patch.object(native_sources, '_message', side_effect=AssertionError('body extraction')):
                    sessions = list_sessions(self.workspace, tool, home=self.home)
                self.assertEqual([item['session_id'] for item in sessions], ['s'])
                source.unlink()

    def test_unselected_corrupt_files_do_not_affect_exact_session(self):
        for tool in ['codex', 'claude', 'workbuddy']:
            with self.subTest(tool=tool):
                if tool == 'codex':
                    rows = [{'type': 'session_meta', 'payload': {'id': 's', 'cwd': str(self.workspace)}},
                            {'type': 'response_item', 'payload': {'type': 'message', 'id': 'm', 'role': 'user', 'content': [{'type': 'input_text', 'text': 'selected'}]}}]
                    selected, other = 'sessions/rollout-date-s.jsonl', 'sessions/other.jsonl'
                elif tool == 'claude':
                    rows = [{'type': 'user', 'sessionId': 's', 'uuid': 'm', 'cwd': str(self.workspace), 'message': {'role': 'user', 'content': 'selected'}}]
                    selected, other = 'projects/p/s.jsonl', 'projects/p/other.jsonl'
                else:
                    rows = [{'type': 'message', 'sessionId': 's', 'id': 'm', 'cwd': str(self.workspace), 'role': 'user', 'content': [{'type': 'input_text', 'text': 'selected'}]}]
                    selected, other = 'projects/p/s.jsonl', 'projects/p/other.jsonl'
                source = self.write(selected, rows)
                corrupt = self.write(other, [], '{bad complete json}\n')
                self.assertEqual([item['text'] for item in self.read(tool)], ['selected'])
                source.unlink()
                corrupt.unlink()

    def test_foreign_listing_stops_at_identity_header(self):
        foreign = self.base / 'elsewhere'
        foreign.mkdir()
        for tool in ['claude', 'workbuddy']:
            source = self.write('projects/p/s.jsonl', [{'sessionId': 's', 'cwd': str(foreign)}], '{bad complete json}\n')
            self.assertEqual(list_sessions(self.workspace, tool, home=self.home), [])
            source.unlink()

    def test_jsonl_message_filter_stops_after_selected_message(self):
        self.write('projects/p/s.jsonl', [{'type': 'user', 'sessionId': 's', 'uuid': 'm', 'cwd': str(self.workspace),
                                         'message': {'role': 'user', 'content': 'selected'}}], '{bad complete json}\n')
        self.assertEqual([item['text'] for item in self.read('claude', message_id='m')], ['selected'])

    def test_record_identity_must_match_claude_workbuddy_filename(self):
        for tool in ['claude', 'workbuddy']:
            source = self.write('projects/p/s.jsonl', [{'sessionId': 'forged', 'cwd': str(self.workspace)}])
            self.assertEqual(list_sessions(self.workspace, tool, home=self.home), [])
            self.assertEqual(self.read(tool), [])
            source.unlink()

    def test_message_filter_precedes_text_extraction_and_caches_cwd(self):
        rows = [{'type': 'user', 'sessionId': 's', 'uuid': str(i), 'cwd': str(self.workspace),
                 'message': {'role': 'user', 'content': 'message ' + str(i)}} for i in range(10)]
        self.write('projects/p/s.jsonl', rows)
        with patch.object(native_sources, '_belongs', wraps=native_sources._belongs) as belongs:
            with patch.object(native_sources, '_text', wraps=native_sources._text) as extract:
                messages = self.read('claude', message_id='8')
        self.assertEqual([item['text'] for item in messages], ['message 8'])
        self.assertEqual(belongs.call_count, 1)
        self.assertEqual(extract.call_count, 1)

    def test_sqlite_listing_needs_no_message_tables(self):
        db, _ = self.database()
        db.execute('INSERT INTO session VALUES (?,?,1,NULL)', ('s', str(self.workspace)))
        db.executescript('DROP TABLE message; DROP TABLE part;')
        db.commit()
        with patch.object(native_sources, '_message', side_effect=AssertionError('body extraction')):
            self.assertEqual([item['session_id'] for item in list_sessions(self.workspace, 'zcode', home=self.home)], ['s'])

    def test_sqlite_message_filter_ignores_unselected_invalid_data(self):
        db, _ = self.database()
        db.execute('INSERT INTO session VALUES (?,?,1,NULL)', ('s', str(self.workspace)))
        db.execute('INSERT INTO message VALUES (?,?,2,?,1)', ('bad', 's', '{invalid'))
        db.execute('INSERT INTO message VALUES (?,?,2,?,2)', ('good', 's', json.dumps({'role': 'user', 'visibility': 'user-visible'})))
        db.execute('INSERT INTO part VALUES (?,?,?,2,?,1)', ('p', 'good', 's', json.dumps({'type': 'text', 'text': 'selected'})))
        db.commit()
        self.assertEqual([item['text'] for item in self.read('zcode', message_id='good')], ['selected'])

    def database(self):
        path = self.home / 'cli/db/db.sqlite'
        path.parent.mkdir(parents=True)
        db = sqlite3.connect(path)
        db.executescript('''PRAGMA journal_mode=WAL;
        CREATE TABLE session(id TEXT,directory TEXT,time_updated INTEGER,time_deleted INTEGER);
        CREATE TABLE message(id TEXT,session_id TEXT,time_updated INTEGER,data TEXT,sequence INTEGER);
        CREATE TABLE part(id TEXT,message_id TEXT,session_id TEXT,time_updated INTEGER,data TEXT,sequence INTEGER);
        ''')
        self.addCleanup(db.close)
        return db, path

    def test_zcode_default_visibility_public_projection_excludes_internal_metadata(self):
        db, _ = self.database()
        db.execute('INSERT INTO session VALUES (?,?,1,NULL)', ('s', str(self.workspace)))
        cases = [
            ('user', {'role': 'user'}),
            ('assistant', {'role': 'assistant'}),
            ('semantic-user', {'role': 'user', 'semantics': {'origin': 'real_user'}}),
            ('semantic-assistant', {'role': 'assistant', 'semantics': {'origin': 'agent_runtime', 'kind': 'assistant_response', 'uiVisibility': 'visible', 'transcriptVisibility': 'visible'}}),
            ('model', {'role': 'user', 'visibility': 'model-only', 'synthetic': True}),
            ('synthetic', {'role': 'user', 'synthetic': True}),
            ('hidden', {'role': 'assistant', 'semantics': {'kind': 'system_reminder', 'uiVisibility': 'hidden'}}),
            ('provider', {'role': 'user', 'metadata': {'visibility': 'model-only'}}),
            ('summary', {'role': 'assistant', 'summary': {}}),
            ('unknown-source', {'role': 'user', 'source': 'unrecognized'}),
        ]
        for sequence, (identifier, data) in enumerate(cases):
            db.execute('INSERT INTO message VALUES (?,?,2,?,?)', (identifier, 's', json.dumps(data), sequence))
            db.execute('INSERT INTO part VALUES (?,?,?,2,?,1)', (identifier, identifier, 's', json.dumps({'type': 'text', 'text': identifier})))
        for sequence, data in enumerate([
                {'type': 'text', 'text': 'HIDDEN_IGNORED', 'ignored': True},
                {'type': 'text', 'text': 'HIDDEN_SYNTHETIC', 'synthetic': True},
                {'type': 'text', 'text': 'HIDDEN_PART_METADATA', 'metadata': {'visibility': 'model-only'}},
                {'type': 'reasoning', 'text': 'HIDDEN_REASONING'},
                {'type': 'tool', 'text': 'HIDDEN_TOOL'}], 2):
            db.execute('INSERT INTO part VALUES (?,?,?,2,?,?)', ('hidden-' + str(sequence), 'semantic-user', 's', json.dumps(data), sequence))
        db.commit()
        messages = self.read('zcode')
        self.assertEqual([message['text'] for message in messages], ['user', 'assistant', 'semantic-user', 'semantic-assistant'])
        self.assertNotIn('HIDDEN_', json.dumps(messages))

    def test_zcode_model_only_part_omits_entire_message_without_visible_semantics(self):
        db, _ = self.database()
        db.execute('INSERT INTO session VALUES (?,?,1,NULL)', ('s', str(self.workspace)))
        db.execute('INSERT INTO message VALUES (?,?,2,?,1)', ('internal', 's', json.dumps({'role': 'user'})))
        db.execute('INSERT INTO part VALUES (?,?,?,2,?,1)', ('ordinary', 'internal', 's', json.dumps({'type': 'text', 'text': 'HIDDEN_SIBLING'})))
        db.execute('INSERT INTO part VALUES (?,?,?,2,?,2)', ('context', 'internal', 's', json.dumps({'type': 'text', 'text': 'HIDDEN_CONTEXT', 'metadata': {'visibility': 'model-only'}})))
        db.commit()
        self.assertEqual(self.read('zcode'), [])

    def test_zcode_missing_database_is_unavailable_for_listing_and_reading(self):
        for operation in [lambda: list_sessions(self.workspace, 'zcode', home=self.home), lambda: self.read('zcode')]:
            with self.assertRaises(MemoryProtocolError) as raised:
                operation()
            self.assertEqual(raised.exception.code, 'NATIVE_SOURCE_UNAVAILABLE')

    def test_zcode_live_wal_deleted_visibility_and_parts(self):
        db, path = self.database()
        db.execute('INSERT INTO session VALUES (?,?,1,NULL)', ('s', str(self.workspace)))
        db.execute('INSERT INTO session VALUES (?,?,1,2)', ('deleted', str(self.workspace)))
        for i, session, visibility in [('yes', 's', 'user-visible'), ('unknown', 's', 'unknown-format'), ('no', 'deleted', 'user-visible')]:
            db.execute('INSERT INTO message VALUES (?,?,2,?,1)', (i, session, json.dumps({'role': 'assistant', 'visibility': visibility})))
            for seq, kind, text in [(2, 'text', 'second'), (0, 'reasoning', 'private'), (1, 'text', 'first')]:
                db.execute('INSERT INTO part VALUES (?,?,?,2,?,?)', (i + str(seq), i, session, json.dumps({'type': kind, 'text': text}), seq))
        db.commit()
        self.assertTrue(Path(str(path) + '-wal').exists())
        before = path.read_bytes()
        self.assertEqual([m['text'] for m in self.read('zcode')], ['first\nsecond'])
        self.assertEqual([s['session_id'] for s in list_sessions(self.workspace, 'zcode', home=self.home)], ['s'])
        self.assertEqual(path.read_bytes(), before)

    def opencode_database(self, cwd=None):
        path = self.home / 'opencode.db'
        with sqlite3.connect(path) as db:
            db.execute('CREATE TABLE session_v2(id TEXT,directory TEXT,time_updated INTEGER)')
            db.execute('INSERT INTO session_v2 VALUES (?,?,?)', ('s', str(cwd or self.workspace), 1000))
        return path

    def opencode_projected(self, messages, **extra):
        value = {'info': {'id': 's', 'location': {'directory': str(self.workspace)}, 'time': {'updated': 1000}}, 'messages': messages}
        value.update(extra)
        return subprocess.CompletedProcess(['/mock/opencode'], 0, json.dumps(value), 'NEVER_EXPOSE_STDERR')

    def test_opencode_projected_public_text_only_with_stable_ids_and_time(self):
        self.opencode_database()
        projected = self.opencode_projected([
            {'id': 'msg_user', 'type': 'user', 'text': 'question', 'time': {'created': 2000}},
            {'id': 'msg_system', 'type': 'system', 'role': 'user', 'text': 'HIDDEN_SYSTEM'},
            {'id': 'msg_synthetic', 'type': 'synthetic', 'role': 'assistant', 'text': 'HIDDEN_SYNTHETIC'},
            {'id': 'msg_idle', 'type': 'idle', 'text': 'HIDDEN_IDLE'},
            {'id': 'msg_assistant', 'type': 'assistant', 'text': 'HIDDEN_TOP_TEXT', 'native': {'text': 'HIDDEN_NATIVE'},
             'content': [{'type': 'text', 'text': 'first'}, {'type': 'reasoning', 'text': 'HIDDEN_REASONING'},
                         {'type': 'tool', 'text': 'HIDDEN_TOOL'}, {'type': 'text', 'text': 'second'}]},
            {'id': 'msg_secret', 'type': 'user', 'text': 'token=abcdefgh12345678'},
        ])
        with patch.object(native_sources.shutil, 'which', return_value='/mock/opencode'):
            with patch.object(native_sources, 'run_export', return_value=projected) as run:
                messages = self.read('opencode')
                selected = self.read('opencode', message_id='msg_assistant')
        self.assertEqual([item.get('text') for item in messages], ['question', 'first\nsecond', None])
        self.assertEqual(selected, messages[1:2])
        self.assertEqual(messages[-1]['status'], 'sensitive_text_omitted')
        self.assertNotIn('HIDDEN_', json.dumps(messages))
        self.assertNotIn('abcdefgh', json.dumps(messages))
        self.assertEqual(messages[0]['observed_at'], '1970-01-01T00:00:02+00:00')
        self.assertEqual(messages[1]['observed_at'], '1970-01-01T00:00:01+00:00')
        self.assertEqual(messages[1]['locator'], 'native-message://opencode/s/msg_assistant')
        self.assertEqual(messages[1]['record_locator'], 'opencode://session/s/message/msg_assistant')
        self.assertEqual(run.call_args.args[0], ['/mock/opencode', 'session', 'export', 's'])
        self.assertEqual(run.call_args.kwargs['cwd'], str(self.workspace))
        self.assertNotIn('shell', run.call_args.kwargs)

    def test_opencode_listing_metadata_only_and_cross_project_export_not_invoked(self):
        foreign = self.base / 'other-project'
        foreign.mkdir()
        register_project(foreign, 'foreign', {'root': '.'}, registry_path=self.registry)
        self.opencode_database(cwd=foreign)
        with patch.object(native_sources, 'run_export', side_effect=AssertionError('must not export')):
            self.assertEqual(list_sessions(self.workspace, 'opencode', home=self.home), [])
            self.assertEqual(self.read('opencode'), [])
        with sqlite3.connect(self.home / 'opencode.db') as db:
            db.execute('UPDATE session_v2 SET directory=?', (str(self.workspace / 'member'),))
        with patch.object(native_sources, 'run_export', side_effect=AssertionError('must not export')):
            summary = list_sessions(self.workspace, 'opencode', home=self.home)[0]
        self.assertEqual(summary['actual_workspace'], str(self.workspace / 'member'))
        self.assertEqual(summary['observed_at'], 1000)

    def test_opencode_export_rejects_mismatched_identity_and_unknown_schema(self):
        self.opencode_database()
        malformed = [
            {'info': {'id': 'wrong', 'location': {'directory': str(self.workspace)}}, 'messages': []},
            {'info': {'id': 's', 'location': {'directory': str(self.workspace / 'member')}}, 'messages': []},
            {'session': 'old format', 'messages': []},
            {'info': {'id': 's', 'location': {'directory': str(self.workspace)}}, 'messages': [{'id': 'msg_old', 'role': 'assistant', 'text': 'old'}]},
            {'info': {'id': 's', 'location': {'directory': str(self.workspace)}}, 'messages': [{'id': 'msg_assistant', 'type': 'assistant', 'text': 'cannot substitute for content'}]},
        ]
        with patch.object(native_sources.shutil, 'which', return_value='/mock/opencode'):
            for value in malformed:
                with self.subTest(value=value):
                    reply = subprocess.CompletedProcess([], 0, json.dumps(value), 'NEVER_EXPOSE_STDERR')
                    with patch.object(native_sources, 'run_export', return_value=reply):
                        with self.assertRaises(MemoryProtocolError) as raised:
                            self.read('opencode')
                        self.assertEqual(raised.exception.code, 'NATIVE_SOURCE_FORMAT_INVALID')
                        self.assertNotIn('NEVER_EXPOSE', str(raised.exception))

    def test_opencode_missing_provider_distinguished_from_empty_sessions_and_cli_failure(self):
        with self.assertRaises(MemoryProtocolError) as raised:
            self.read('opencode')
        self.assertEqual(raised.exception.code, 'NATIVE_SOURCE_UNAVAILABLE')
        self.opencode_database()
        with patch.object(native_sources.shutil, 'which', return_value=None):
            with self.assertRaises(MemoryProtocolError) as raised:
                self.read('opencode')
        self.assertEqual(raised.exception.code, 'NATIVE_SOURCE_UNAVAILABLE')
        with patch.object(native_sources.shutil, 'which', return_value='/mock/opencode'):
            with patch.object(native_sources, 'run_export', return_value=subprocess.CompletedProcess([], 1, 'private output', 'private stderr')):
                with self.assertRaises(MemoryProtocolError) as raised:
                    self.read('opencode')
                self.assertEqual(raised.exception.code, 'NATIVE_SOURCE_UNAVAILABLE')
                self.assertNotIn('private', str(raised.exception))
        with sqlite3.connect(self.home / 'opencode.db') as db:
            db.execute('DELETE FROM session_v2')
        self.assertEqual(list_sessions(self.workspace, 'opencode', home=self.home), [])
        self.assertEqual(self.read('opencode'), [])

    def test_native_message_snapshot_normalizes_identity_and_tracks_public_revision(self):
        db, path = self.database()
        sid, mid = 's/slash', 'm one'
        db.execute('INSERT INTO session VALUES (?,?,1,NULL)', (sid, str(self.workspace)))
        db.execute('INSERT INTO message VALUES (?,?,2,?,1)', (mid, sid, json.dumps({'role': 'assistant'})))
        db.execute('INSERT INTO part VALUES (?,?,?,2,?,1)', ('p', mid, sid, json.dumps({'type': 'text', 'text': 'public revision one'})))
        db.commit()
        canonical = 'native-message://zcode/s%2Fslash/m%20one'
        with patch.object(native_sources, '_root', return_value=self.home):
            first = source_snapshot(self.workspace, canonical)
            variant = source_snapshot(self.workspace, 'native-message://zcode/%73%2fslash/%6d%20one')
            self.assertEqual(first, variant)
            self.assertEqual(first['source_instance'], 'native-message.zcode')
            self.assertEqual(first['object_id'], canonical)
            self.assertEqual(first['locator'], canonical)
            self.assertNotIn('text', first)
            self.assertNotIn('body', first)
            message = self.read('zcode', session=sid, message_id=mid)[0]
            self.assertEqual(message['locator'], canonical)
            self.assertTrue(message['record_locator'].startswith(str(path) + '#session='))
            db.execute('UPDATE part SET data=?', (json.dumps({'type': 'text', 'text': 'public revision two'}),))
            db.commit()
            second = source_snapshot(self.workspace, canonical)
        self.assertEqual(second['object_id'], first['object_id'])
        self.assertNotEqual(second['source_revision'], first['source_revision'])

    def test_native_message_snapshot_rejects_foreign_hidden_sensitive_and_deleted(self):
        db, _ = self.database()
        foreign = self.base / 'foreign'
        foreign.mkdir()
        register_project(foreign, 'foreign', {'root': '.'}, registry_path=self.registry)
        for sid, cwd, removed, data, text in [
                ('foreign', foreign, None, {'role': 'user'}, 'foreign public'),
                ('hidden', self.workspace, None, {'role': 'assistant', 'synthetic': True}, 'HIDDEN_CONTEXT'),
                ('sensitive', self.workspace, None, {'role': 'user'}, 'token=FICTIONAL_VALUE_12345'),
                ('deleted', self.workspace, 3, {'role': 'user'}, 'deleted public')]:
            db.execute('INSERT INTO session VALUES (?,?,1,?)', (sid, str(cwd), removed))
            db.execute('INSERT INTO message VALUES (?,?,2,?,1)', ('m', sid, json.dumps(data)))
            db.execute('INSERT INTO part VALUES (?,?,?,2,?,1)', ('p-' + sid, 'm', sid, json.dumps({'type': 'text', 'text': text})))
        db.commit()
        with patch.object(native_sources, '_root', return_value=self.home):
            for sid in ['foreign', 'hidden', 'deleted', 'missing']:
                with self.subTest(source=sid):
                    with self.assertRaises(MemoryProtocolError) as raised:
                        source_snapshot(self.workspace, f'native-message://zcode/{sid}/m')
                    self.assertEqual(raised.exception.code, 'NATIVE_MESSAGE_UNAVAILABLE')
            with self.assertRaises(MemoryProtocolError) as raised:
                source_snapshot(self.workspace, 'native-message://zcode/sensitive/m')
            self.assertEqual(raised.exception.code, 'NATIVE_MESSAGE_SENSITIVE')

    def test_native_message_snapshot_never_restores_deleted_workbuddy_text(self):
        with sqlite3.connect(self.home / 'workbuddy.db') as db:
            db.execute('INSERT INTO sessions VALUES (?,?,?,?)', ('s', str(self.workspace), 'deleted', 'updated'))
        with patch.object(native_sources, '_root', return_value=self.home):
            with patch.object(native_sources, '_records', side_effect=AssertionError('deleted record must not be read')):
                with self.assertRaises(MemoryProtocolError) as raised:
                    source_snapshot(self.workspace, 'native-message://workbuddy/s/m')
        self.assertEqual(raised.exception.code, 'NATIVE_MESSAGE_UNAVAILABLE')

    def test_native_message_snapshot_rejects_invalid_locators_and_ambiguous_results(self):
        invalid = ['file:///zcode/s/m', 'native-message://unsupported/s/m', 'native-message://zcode/s',
                   'native-message://zcode/s/m/extra', 'native-message://zcode//m', 'native-message://zcode/s/',
                   'native-message://zcode/s/m?revision=missing', 'native-message://zcode/s/m?',
                   'native-message://zcode/s/m#raw', 'native-message://zcode/s/m%00',
                   'native-message://zcode:80/s/m']
        with patch.object(native_sources, 'read_messages', side_effect=AssertionError('invalid locator must not read')):
            for index, locator in enumerate(invalid):
                with self.subTest(locator=index):
                    with self.assertRaises(MemoryProtocolError) as raised:
                        source_snapshot(self.workspace, locator)
                    self.assertEqual(raised.exception.code, 'NATIVE_MESSAGE_INVALID')
        with patch.object(native_sources, 'read_messages', return_value=[{}, {}]):
            with self.assertRaises(MemoryProtocolError) as raised:
                source_snapshot(self.workspace, 'native-message://zcode/s/m')
        self.assertEqual(raised.exception.code, 'NATIVE_MESSAGE_UNAVAILABLE')

    def test_native_credential_context_quarantines_entire_message_and_snapshot(self):
        snippets = ['密码口径见[[fixture-note]]（FICTIONAL_VALUE_ONE 全链无效，保持secret值）',
                    '**dev库口令**：默认 FICTIONAL_VALUE_TWO，以及${PG_PASSWORD:-FICTIONAL_FALLBACK}',
                    'curl -u fixture-user:FICTIONAL_VALUE_THREE https://fixture.invalid/',
                    '密码通过 SecretRef 解析，不保存原文。',
                    'PG fixture-user/FICTIONAL_PG_VALUE，通过 .pgpass 免密使用。',
                    'PGPASSWORD=FICTIONAL_ASSIGNMENT_VALUE',
                    'PasswordEncoderHelper.encode("FICTIONAL_ENCODING_VALUE")',
                    'UAT 登录 fixture-tenant/fixture-user/FICTIONAL_LOGIN_VALUE 有效。']
        for index, snippet in enumerate(snippets):
            self.write('sessions/s.jsonl', [
                {'type': 'session_meta', 'payload': {'id': 's', 'cwd': str(self.workspace)}},
                {'type': 'response_item', 'payload': {'id': 'm', 'type': 'message', 'role': 'user',
                 'content': [{'type': 'input_text', 'text': snippet}]}}])
            with self.subTest(example=index):
                message = self.read('codex')[0]
                self.assertNotIn('text', message)
                self.assertEqual(message['status'], 'sensitive_text_omitted')
                self.assertEqual(message['reason'], 'credential_context_requires_curated_evidence')
                self.assertNotIn('FICTIONAL_', json.dumps(message))
                with patch.object(native_sources, '_root', return_value=self.home):
                    with self.assertRaises(MemoryProtocolError) as raised:
                        source_snapshot(self.workspace, 'native-message://codex/s/m')
                self.assertEqual(raised.exception.code, 'NATIVE_MESSAGE_SENSITIVE')

    def test_native_non_auth_feedback_budget_tokens_and_variable_reference_remain_readable(self):
        text = '筛选交互正常；预算 token 为 4096，output tokens 为 512。仅引用 ${PG_PASSWORD}。'
        self.write('sessions/s.jsonl', [
            {'type': 'session_meta', 'payload': {'id': 's', 'cwd': str(self.workspace)}},
            {'type': 'response_item', 'payload': {'id': 'm', 'type': 'message', 'role': 'assistant',
             'content': [{'type': 'output_text', 'text': text}]}}])
        self.assertEqual(self.read('codex')[0]['text'], text)
        with patch.object(native_sources, '_root', return_value=self.home):
            snapshot = source_snapshot(self.workspace, 'native-message://codex/s/m')
        self.assertEqual(snapshot['source_instance'], 'native-message.codex')

    def test_unsupported_and_unregistered_and_unknown_format(self):
        with self.assertRaises(MemoryProtocolError) as raised:
            self.read('unknown-tool')
        self.assertEqual(raised.exception.code, 'NATIVE_SOURCE_UNSUPPORTED')
        self.registry.unlink()
        with self.assertRaises(MemoryProtocolError) as raised:
            self.read('claude')
        self.assertEqual(raised.exception.code, 'PROJECT_UNREGISTERED')
        register_project(self.workspace, 'test', {'root': '.'}, registry_path=self.registry)
        self.write('sessions/s.jsonl', [{'raw': 'unknown'}])
        with self.assertRaises(MemoryProtocolError) as raised:
            self.read('codex')
        self.assertEqual(raised.exception.code, 'NATIVE_SOURCE_FORMAT_INVALID')


if __name__ == '__main__':
    unittest.main()
