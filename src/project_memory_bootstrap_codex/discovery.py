"""Compensating discovery of owner files and native session activity, not prose."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
from pathlib import Path
from urllib.parse import quote

from . import memory_state as state
from .errors import MemoryProtocolError
from .native_sources import list_sessions
from .maintenance import is_internal_session

FILE_OWNERS = ('AGENTS.md', 'README.md', 'docs/README.md', 'docs/工程执行总览.md',
               'docs/需求.md', 'docs/架构设计.md', 'docs/项目现状与成熟度基线.md')
FILE_GLOBS = ('openspec/specs/**/*.md', 'openspec/changes/**/*.md')


def _cursor(connection):
    row = connection.execute("SELECT value FROM meta WHERE key='discovery_v1'").fetchone()
    if row is None:
        return {'schema_version': 1, 'files': {}, 'sessions': {}, 'native_notes': {}}
    try:
        value = json.loads(row[0])
    except (ValueError, TypeError):
        raise MemoryProtocolError('DISCOVERY_CURSOR_INVALID', '发现游标格式无效') from None
    if (not isinstance(value, dict) or value.get('schema_version') != 1
            or not isinstance(value.get('files'), dict) or not isinstance(value.get('sessions'), dict)
            or ('native_notes' in value and not isinstance(value['native_notes'], dict))):
        raise MemoryProtocolError('DISCOVERY_CURSOR_INVALID', '发现游标格式无效')
    value.setdefault('native_notes', {})
    return value


def _merge_cursor(context, section, key, value):
    # capture owns its own lock; only merge the accepted receipt afterwards.
    with state._locked(context) as connection:
        state._recover(connection, context)
        cursor = _cursor(connection)
        previous = cursor[section].get(key)
        if previous is None or previous['observed_at'] <= value['observed_at']:
            cursor[section][key] = value
            with connection:
                connection.execute('INSERT OR REPLACE INTO meta(key,value) VALUES (?,?)',
                                   ('discovery_v1', json.dumps(cursor, ensure_ascii=False, sort_keys=True)))


def _in_scope(root, path):
    try:
        relative = Path(path).relative_to(root)
    except ValueError:
        return False
    if relative.as_posix() in FILE_OWNERS:
        return True
    return (relative.suffix == '.md' and len(relative.parts) >= 3
            and relative.parts[:2] in {('openspec', 'specs'), ('openspec', 'changes')})


def scan_sources(workspace: Path, *, tools: tuple[str, ...] = ('codex', 'claude', 'zcode', 'workbuddy', 'opencode'),
                 provider_roots: dict[str, Path] | None = None) -> dict:
    """Capture changed owner hashes and metadata activity, never native messages.

    Cursors describe accepted candidates, not semantic review, stable native
    message IDs, or a claim that every project action was observed.
    """
    observed_at = dt.datetime.now(dt.timezone.utc).isoformat()
    context = state._context(Path(workspace), writing=True)
    with state._locked(context) as connection:
        state._recover(connection, context)
        baseline = _cursor(connection)
    result = dict(files_observed=0, sessions_observed=0, internal_sessions_skipped=0, new_candidates=0, unchanged=0, errors=[], withheld=0, withheld_sources=[],
                  coverage=dict(tools=list(tools), file_scope=list(FILE_OWNERS + FILE_GLOBS),
                                not_covered=['源码文件', 'GUI 活动', '原生会话公开正文与隐藏思考'],
                                session_activity='JSONL 文件 size/mtime_ns 或 ZCode/OpenCode 会话更新时间；不是消息标识或语义结论'))

    def error(source, exc):
        code = exc.code if isinstance(exc, MemoryProtocolError) else 'DISCOVERY_IO_ERROR'
        if code == 'NATIVE_MEMORY_SENSITIVE':
            result['withheld'] += 1
            result['withheld_sources'].append({'locator': source, 'reason': code})
        else:
            result['errors'].append({'source': source, 'error_code': code})

    def receive(section, key, revision, payload, *, removed=False):
        # A short recheck avoids work already accepted by concurrent scans.
        with state._locked(context) as connection:
            state._recover(connection, context)
            current = _cursor(connection)[section].get(key)
        if current and (current['revision'] == revision or current['observed_at'] > observed_at):
            result['unchanged'] += 1
            return
        receipt = state.capture(Path(workspace), payload)
        _merge_cursor(context, section, key, dict(revision=revision, candidate_id=receipt['candidate_id'],
                                                observed_at=observed_at, removed=removed))
        result['unchanged' if receipt['duplicate'] else 'new_candidates'] += 1

    # All filesystem enumeration and hashing stays outside the publication lock.
    files = {context.root / name for name in FILE_OWNERS}
    for pattern in FILE_GLOBS:
        files.update(context.root.glob(pattern))
    files.update(Path(path) for path in baseline['files'] if _in_scope(context.root, path))
    observations = []
    for path in sorted(files):
        locator = str(path)
        try:
            if path.is_file():
                # Reuse capture's credential/membership boundary before reading bytes.
                source = state._source_file(context, locator)
                revision = hashlib.sha256(source.read_bytes()).hexdigest()
                result['files_observed'] += 1
                observations.append((locator, revision, False))
            elif locator in baseline['files']:
                observations.append((locator, 'missing', True))
        except (MemoryProtocolError, OSError) as exc:
            error(locator, exc)
    for locator, revision, removed in observations:
        payload = dict(source_instance='file', object_id=locator,
                       source_revision=revision, locator=locator, kind='file',
                       observed_at=observed_at)
        try:
            receive('files', locator, revision, payload, removed=removed)
        except (MemoryProtocolError, OSError) as exc:
            error(locator, exc)

    for tool in tools:
        try:
            sessions = list_sessions(Path(workspace), tool, home=(provider_roots or {}).get(tool))
        except (MemoryProtocolError, OSError) as exc:
            error(tool, exc)
            continue
        result['sessions_observed'] += len(sessions)
        for session in sessions:
            session_id = session['session_id']
            locator = f'{tool}://session/{quote(session_id, safe="")}'
            try:
                if is_internal_session(context, tool, session_id):
                    result['internal_sessions_skipped'] += 1
                    continue
                if tool in {'zcode', 'opencode'}:
                    version = str(session['observed_at'])
                elif tool == 'workbuddy':
                    source_state = session['source_state']
                    if source_state == 'deleted':
                        version = 'deleted:' + str(session['deleted_at'])
                    elif Path(session['locator']).suffix == '.jsonl':
                        stat = Path(session['locator']).stat()
                        version = f'{source_state}:{stat.st_size}:{stat.st_mtime_ns}'
                    else:
                        version = 'index-only:' + str(session['observed_at'])
                else:
                    stat = Path(session['locator']).stat()
                    version = f'{stat.st_size}:{stat.st_mtime_ns}'
                # Only the source identity and event version are persisted.
                metadata = {'native_tool': tool, 'session_id': session_id, 'activity_version': version}
                if tool == 'workbuddy':
                    metadata['source_state'] = session['source_state']
                payload = dict(source_instance='native-session.' + tool, object_id=session_id,
                               source_revision=version, locator=locator, kind='session_activity',
                               observed_at=observed_at,
                               excerpt=json.dumps(metadata, sort_keys=True))
                receive('sessions', tool + ':' + session_id, version, payload)
            except (MemoryProtocolError, OSError) as exc:
                error(locator, exc)
    from .native_notes import TOOLS as NOTE_TOOLS, list_notes, source_snapshot
    note_tools = tuple(tool for tool in tools if tool in NOTE_TOOLS)
    result['native_notes_observed'] = 0
    result['coverage']['native_project_notes'] = {'tools': list(note_tools)}
    if note_tools:
        try:
            catalog = list_notes(Path(workspace), tools=note_tools)
        except (MemoryProtocolError, OSError) as exc:
            error('native-project-notes', exc)
        else:
            suppressed_note_sources = state.source_suppressions(context)
            result['coverage']['native_project_notes']['routes'] = catalog['coverage']
            for item in catalog['errors']:
                error(item.get('locator', item.get('tool', 'native-project-notes')),
                      MemoryProtocolError(item['code'], 'Native note discovery did not complete.'))
            observed_locators = set()
            for note in catalog['notes']:
                locator = note['locator']
                observed_locators.add(locator)
                result['native_notes_observed'] += 1
                metadata = {key: note[key] for key in ('tool', 'workspace', 'repo', 'title', 'description',
                            'note_type', 'origin_session_id', 'sensitive_metadata_omitted') if key in note}
                payload = dict(source_instance='native-memory.' + note['tool'], object_id=locator,
                               locator=locator, source_revision=note['source_revision'], kind='native_memory_note',
                               observed_at=observed_at, excerpt=json.dumps(metadata, ensure_ascii=False, sort_keys=True))
                try:
                    # A matching cursor is not permission to reuse a now-withheld
                    # native source; keep the reader's current safety boundary.
                    source_snapshot(Path(workspace), locator)
                    receive('native_notes', locator, note['source_revision'], payload)
                except (MemoryProtocolError, OSError) as exc:
                    error(locator, exc)
            # A missing topic is an observation of the same source, not a new
            # source or a forgetting decision. An unavailable root is not absence.
            for locator, previous in baseline['native_notes'].items():
                if locator in observed_locators or not any(locator.startswith('native-memory://' + tool + '/') for tool in note_tools):
                    continue
                if any('native-memory.' + tool + ':' + locator in suppressed_note_sources for tool in note_tools):
                    continue
                try:
                    snapshot = source_snapshot(Path(workspace), locator, allow_missing=True)
                    if snapshot['source_revision'] != 'missing':
                        continue
                    receive('native_notes', locator, 'missing', dict(snapshot, kind='native_memory_note',
                            observed_at=observed_at, excerpt='Native topic is absent; shared facts were not automatically forgotten.'), removed=True)
                except (MemoryProtocolError, OSError) as exc:
                    error(locator, exc)
    return result
