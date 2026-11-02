"""Read public native messages without importing native memory or executing content.

``home`` selects a provider root, not a user home. Only locally registered
projects are eligible; sessions from other members of that project are allowed.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import shutil
from subprocess import run as run_export
import sqlite3
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

from .errors import MemoryProtocolError
from .facts import SENSITIVE_RE, digest, native_credential_context
from .project import resolve_context

TOOLS = {'codex', 'claude', 'workbuddy', 'zcode', 'opencode'}


def _error(code, message):
    return MemoryProtocolError(code, message)


def _context(workspace):
    context = resolve_context(Path(workspace))
    if context is None or not context.registered:
        raise _error('PROJECT_UNREGISTERED', '原生来源只支持本机已登记项目')
    return context


def _belongs(cwd, context):
    if not isinstance(cwd, str) or not Path(cwd).is_absolute():
        return None
    try:
        actual = resolve_context(Path(cwd))
    except MemoryProtocolError as exc:
        if exc.code == 'PROJECT_UNREGISTERED':
            return None
        raise
    if actual and actual.registered and actual.root == context.root and actual.project_id == context.project_id:
        return actual
    return None


def _root(tool, home):
    if tool not in TOOLS:
        raise _error('NATIVE_SOURCE_UNSUPPORTED', '该工具的公开消息格式尚未支持')
    if home is not None:
        return Path(home).expanduser().resolve()
    return Path.home() / ('.local/share/opencode' if tool == 'opencode' else '.' + tool)


def _records(path):
    with path.open('rb') as stream:
        for ordinal, line in enumerate(stream, 1):
            # A writer may still be appending the last record.
            if not line.endswith(b'\n'):
                break
            try:
                value = json.loads(line)
            except (ValueError, UnicodeError):
                raise _error('NATIVE_SOURCE_FORMAT_INVALID', '原生来源包含无效完整 JSON 记录') from None
            if not isinstance(value, dict):
                raise _error('NATIVE_SOURCE_FORMAT_INVALID', '原生来源记录必须是对象')
            yield ordinal, value


def _text(content, tool):
    if tool == 'claude' and isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ''
    allowed = {'text'} if tool == 'claude' else {'input_text', 'output_text'}
    return '\n'.join(part['text'] for part in content
                     if isinstance(part, dict) and part.get('type') in allowed
                     and isinstance(part.get('text'), str))


def _message(tool, session, message_id, role, text, locator, observed_at, cwd):
    if not text.strip():
        return None
    result = dict(message_id=message_id, session_id=session, role=role,
                  source_revision=digest(json.dumps({'role': role, 'text': text}, ensure_ascii=False, sort_keys=True)),
                  locator=f'native-message://{tool}/{quote(session, safe="")}/{quote(message_id, safe="")}',
                  record_locator=locator, observed_at=observed_at, native_tool=tool, cwd=cwd)
    if SENSITIVE_RE.search(text) or native_credential_context(text):
        result['status'] = 'sensitive_text_omitted'
        result['reason'] = 'credential_context_requires_curated_evidence'
    else:
        result['text'] = text
    return result


def _file_sessions(context, tool, root, session_id=None, message_id=None, *, metadata_only=False, ownership, workbuddy_index=None):
    pattern = 'sessions/**/*.jsonl' if tool == 'codex' else 'projects/*/*.jsonl'
    sessions = {}
    paths = sorted(root.glob(pattern))
    if tool == 'codex':
        paths += sorted(root.glob('archived_sessions/**/*.jsonl'))
    seen_codex = set()
    for path in paths:
        if session_id is not None and not (path.stem == session_id or
                (tool == 'codex' and path.stem.endswith('-' + session_id))):
            continue
        if tool == 'workbuddy':
            indexed = workbuddy_index.get(path.stem)
            if indexed and (indexed['actual'] is None or indexed['deleted_at'] is not None):
                continue
        records = _records(path)
        try:
            codex_meta = None
            if tool == 'codex':
                first = next(records, None)
                if first is None:
                    continue
                first = first[1]
                codex_meta = first.get('payload') if first.get('type') == 'session_meta' else None
                if not isinstance(codex_meta, dict) or not isinstance(codex_meta.get('id'), str):
                    raise _error('NATIVE_SOURCE_FORMAT_INVALID', 'Codex 来源缺少首条 session_meta')
                if session_id is not None and codex_meta['id'] != session_id:
                    continue
                session, cwd = codex_meta['id'], codex_meta.get('cwd')
                if session in seen_codex:
                    continue
                seen_codex.add(session)
                actual = ownership(cwd)
                if actual is None:
                    continue
                summary = _summary(session, cwd, actual, tool, path)
                summary['archived'] = path.is_relative_to(root / 'archived_sessions')
                sessions.setdefault((session, cwd), (summary, []))
                if metadata_only:
                    continue
            for ordinal, record in records:
                if tool == 'codex':
                    session, cwd = codex_meta['id'], codex_meta['cwd']
                else:
                    session, cwd = record.get('sessionId'), record.get('cwd')
                    # Encoded directories and titles never establish ownership.
                    if session != path.stem or (session_id is not None and session != session_id):
                        continue
                    actual = ownership(cwd)
                    if actual is None:
                        if metadata_only and isinstance(cwd, str) and Path(cwd).is_absolute():
                            break
                        continue
                    if tool == 'workbuddy' and session in workbuddy_index:
                        if Path(cwd).resolve() != Path(workbuddy_index[session]['cwd']).resolve():
                            raise _error('NATIVE_SOURCE_FORMAT_INVALID', 'WorkBuddy 索引与记录的 cwd 不匹配')
                    if metadata_only:
                        sessions.setdefault((session, cwd), (_summary(session, cwd, actual, tool, path), []))
                        break
                if record.get('isMeta') or record.get('isSidechain'):
                    continue
                if tool == 'codex':
                    payload = record.get('payload')
                    if record.get('type') != 'response_item' or not isinstance(payload, dict) or payload.get('type') != 'message':
                        continue
                    native_id = payload.get('id') or f"{record.get('timestamp', '')}:{ordinal}"
                elif tool == 'claude':
                    payload = record.get('message')
                    if record.get('type') not in {'user', 'assistant'} or not isinstance(payload, dict):
                        continue
                    native_id = record.get('uuid')
                else:
                    if record.get('type') != 'message':
                        continue
                    payload = record
                    native_id = record.get('id')
                if message_id is not None and native_id != message_id:
                    continue
                if tool == 'codex' and any(value in {'analysis', 'reasoning'}
                        for value in (record.get('phase'), record.get('channel'), payload.get('phase'), payload.get('channel'))):
                    continue
                role = payload.get('role')
                if role not in {'user', 'assistant'} or payload.get('isMeta') or payload.get('isSidechain'):
                    continue
                if tool == 'claude' and role != record['type']:
                    continue
                if not isinstance(native_id, str):
                    raise _error('NATIVE_SOURCE_FORMAT_INVALID', '公开消息缺少原生标识')
                key = (session, cwd)
                sessions.setdefault(key, (_summary(session, cwd, actual, tool, path), []))
                text = _text(payload.get('content'), tool)
                projection_type = None
                provider = payload.get('providerData')
                if (tool == 'workbuddy' and role == 'user' and isinstance(provider, dict)
                        and provider.get('startsNewUserRequest') is True and provider.get('isAdditionalData') is not True):
                    # WorkBuddy's wrapHiddenContextXml + wrapUserQueryXml carrier,
                    # verified in the installed host. Only its current-user field
                    # is public prose; quoted/auxiliary or ambiguous envelopes
                    # retain the original content and its existing safety guard.
                    query = re.fullmatch(r'(?:\s*<system-reminder\b[^>]*>[\s\S]*?</system-reminder>\s*)+'
                                         r'<user_query>([\s\S]*?)</user_query>\s*', text)
                    if (query and text.count('<user_query>') == text.count('</user_query>') == 1
                            and not re.search(r'</?auxiliary\b', query[1])):
                        text = query[1]
                        projection_type = 'workbuddy_user_query'
                value = _message(tool, session, native_id, role, text,
                                 f'{path}#line={ordinal}', record.get('timestamp'), cwd)
                if value:
                    if projection_type:
                        value['projection_type'] = projection_type
                    sessions[key][1].append(value)
                if message_id is not None:
                    break
        finally:
            records.close()
    if tool == 'workbuddy':
        for summary, _ in sessions.values():
            indexed = workbuddy_index.get(summary['session_id'])
            summary['source_state'] = 'active' if indexed else 'unindexed'
            if indexed:
                summary.update(deleted_at=indexed['deleted_at'], observed_at=indexed['updated_at'])
    return list(sessions.values())


def _summary(session, cwd, actual, tool, path):
    return dict(session_id=session, cwd=cwd, actual_workspace=str(actual.workspace_root),
                repository_id=actual.repository_id, project_id=actual.project_id,
                native_tool=tool, locator=str(path))


def _workbuddy_sessions(context, root, session_id=None, message_id=None, *, metadata_only=False, ownership):
    path = root / 'workbuddy.db'
    if not path.is_file():
        raise _error('NATIVE_SOURCE_UNAVAILABLE', 'WorkBuddy GUI 索引数据库不存在')
    try:
        connection = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
        try:
            query = 'SELECT id,cwd,deleted_at,updated_at FROM sessions'
            parameters = ()
            if session_id is not None:
                query += ' WHERE id=?'
                parameters = (session_id,)
            rows = connection.execute(query + ' ORDER BY id', parameters).fetchall()
        finally:
            connection.close()
    except sqlite3.Error:
        raise _error('NATIVE_SOURCE_FORMAT_INVALID', 'WorkBuddy GUI 索引不是已知 sessions schema') from None
    index = {}
    sessions = {}
    for native_id, cwd, deleted_at, updated_at in rows:
        actual = ownership(cwd)
        index[native_id] = dict(cwd=cwd, deleted_at=deleted_at, updated_at=updated_at, actual=actual)
        if actual is None:
            continue
        if deleted_at is not None and not metadata_only:
            raise _error('NATIVE_SOURCE_INACTIVE', 'WorkBuddy 会话已删除，仅可查看最小元数据')
        if not metadata_only and not any(candidate.stem == native_id and candidate.is_file()
                                         for candidate in root.glob('projects/*/*.jsonl')):
            raise _error('NATIVE_SOURCE_UNAVAILABLE', 'WorkBuddy 已索引会话的 JSONL 不存在')
        if metadata_only:
            summary = _summary(native_id, cwd, actual, 'workbuddy', path)
            summary.update(source_state='deleted' if deleted_at is not None else 'active',
                           deleted_at=deleted_at, observed_at=updated_at)
            sessions[native_id] = (summary, [])
    files = _file_sessions(context, 'workbuddy', root, session_id, message_id,
                           metadata_only=metadata_only, ownership=ownership, workbuddy_index=index)
    for summary, messages in files:
        sessions[summary['session_id']] = (summary, messages)
    return list(sessions.values())


def _json_data(raw):
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        raise _error('NATIVE_SOURCE_FORMAT_INVALID', '原生数据库包含无效 JSON') from None
    if not isinstance(value, dict):
        raise _error('NATIVE_SOURCE_FORMAT_INVALID', '原生数据库 data 必须是对象')
    return value


def _zcode_public(data):
    # ZCode.app glm/zcode.cjs: getConversationMessageProjectionPolicy (hT).
    # Missing visibility is normal for real user input and assistant responses.
    # Accept a conservative subset, rather than reconstruct legacy projections.
    if (data.get('role') not in {'user', 'assistant'} or data.get('visibility') not in {None, 'user-visible'}
            or data.get('synthetic') is True or data.get('isMeta') or data.get('summary') is not None):
        return False
    metadata = data.get('metadata') or {}
    if not isinstance(metadata, dict) or metadata.get('visibility') == 'model-only':
        return False
    semantics = data.get('semantics')
    if semantics is not None:
        if not isinstance(semantics, dict):
            return False
        if semantics.get('uiVisibility') == 'hidden' or semantics.get('transcriptVisibility') == 'hidden':
            return False
        if data['role'] == 'user':
            return semantics.get('origin') == 'real_user'
        return (semantics.get('kind') == 'assistant_response' and semantics.get('uiVisibility') == 'visible'
                and semantics.get('transcriptVisibility') == 'visible')
    return not data.get('source') and not metadata.get('source')


def _zcode_sessions(context, root, session_id=None, message_id=None, *, metadata_only=False, ownership):
    path = root / 'cli/db/db.sqlite'
    if not path.exists():
        raise _error('NATIVE_SOURCE_UNAVAILABLE', 'ZCode provider 数据库不存在')
    sessions = []
    # mode=ro includes a live WAL; immutable would silently miss recent records.
    try:
        connection = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
        try:
            query = 'SELECT id,directory,time_updated FROM session WHERE time_deleted IS NULL'
            parameters = ()
            if session_id is not None:
                query += ' AND id=?'
                parameters = (session_id,)
            for session_id, cwd, updated in connection.execute(query, parameters):
                actual = ownership(cwd)
                if actual is None:
                    continue
                summary = dict(session_id=session_id, cwd=cwd, actual_workspace=str(actual.workspace_root),
                               repository_id=actual.repository_id, project_id=actual.project_id,
                               native_tool='zcode', locator=str(path), observed_at=updated)
                messages = []
                if metadata_only:
                    sessions.append((summary, messages))
                    continue
                message_query = 'SELECT id,time_updated,data FROM message WHERE session_id=?'
                message_parameters = (session_id,)
                if message_id is not None:
                    message_query += ' AND id=?'
                    message_parameters += (message_id,)
                message_query += ' ORDER BY sequence,id'
                for native_id, stamp, raw in connection.execute(message_query, message_parameters):
                    data = _json_data(raw)
                    role = data.get('role')
                    if not _zcode_public(data):
                        continue
                    texts = []
                    model_only_parts = False
                    for (part_raw,) in connection.execute('SELECT data FROM part WHERE session_id=? AND message_id=? ORDER BY sequence,id', (session_id, native_id)):
                        part = _json_data(part_raw)
                        metadata = part.get('metadata') or {}
                        if isinstance(metadata, dict) and metadata.get('visibility') == 'model-only':
                            model_only_parts = True
                        if (part.get('type') == 'text' and isinstance(part.get('text'), str)
                                and not part.get('ignored') and not part.get('synthetic')
                                and isinstance(metadata, dict) and metadata.get('visibility') != 'model-only'
                                and not metadata.get('source')):
                            texts.append(part['text'])
                    # hT treats any model-only part as provider context unless
                    # explicit visible semantics already established the projection.
                    if model_only_parts and data.get('semantics') is None:
                        continue
                    message = _message('zcode', session_id, native_id, role, '\n'.join(texts),
                                       f'{path}#session={session_id}&message={native_id}', stamp, cwd)
                    if message:
                        messages.append(message)
                sessions.append((summary, messages))
        finally:
            connection.close()
    except sqlite3.Error:
        raise _error('NATIVE_SOURCE_FORMAT_INVALID', '无法读取已知 ZCode SQLite schema') from None
    return sessions


def _opencode_export(summary, session_id, message_id):
    # Official complete projected transcript schema, verified with OpenCode 2.2.5.
    executable = shutil.which('opencode')
    if executable is None:
        raise _error('NATIVE_SOURCE_UNAVAILABLE', '未安装 OpenCode 导出命令')
    try:
        completed = run_export([executable, 'session', 'export', session_id],
                                   cwd=summary['actual_workspace'], capture_output=True, text=True)
    except (OSError, UnicodeError):
        raise _error('NATIVE_SOURCE_UNAVAILABLE', 'OpenCode 公开会话导出不可用') from None
    if completed.returncode != 0:
        raise _error('NATIVE_SOURCE_UNAVAILABLE', 'OpenCode 公开会话导出失败')
    try:
        exported = json.loads(completed.stdout)
    except (ValueError, TypeError):
        raise _error('NATIVE_SOURCE_FORMAT_INVALID', 'OpenCode 导出不是已知 JSON 投影') from None
    info = exported.get('info') if isinstance(exported, dict) else None
    location = info.get('location') if isinstance(info, dict) else None
    if (not isinstance(info, dict) or info.get('id') != session_id or not isinstance(location, dict)
            or not isinstance(location.get('directory'), str) or not Path(location['directory']).is_absolute()
            or Path(location['directory']).resolve() != Path(summary['cwd']).resolve()
            or not isinstance(exported.get('messages'), list)):
        raise _error('NATIVE_SOURCE_FORMAT_INVALID', 'OpenCode 导出身份或投影格式不匹配')
    messages = []
    for item in exported['messages']:
        if not isinstance(item, dict):
            raise _error('NATIVE_SOURCE_FORMAT_INVALID', 'OpenCode 消息必须是投影对象')
        role = item.get('type')
        if role not in {'user', 'assistant'}:
            if role not in {'agent-switched', 'model-switched', 'location-switched', 'synthetic', 'system', 'skill', 'shell', 'compaction', 'idle'}:
                raise _error('NATIVE_SOURCE_FORMAT_INVALID', 'OpenCode 消息不是已知投影类型')
            continue
        native_id = item.get('id')
        if message_id is not None and native_id != message_id:
            continue
        if not isinstance(native_id, str) or not native_id.startswith('msg_'):
            raise _error('NATIVE_SOURCE_FORMAT_INVALID', 'OpenCode 消息缺少原生标识')
        if role == 'user':
            text = item.get('text')
            if not isinstance(text, str):
                raise _error('NATIVE_SOURCE_FORMAT_INVALID', 'OpenCode user 投影缺少公开 text')
        else:
            content = item.get('content')
            if not isinstance(content, list):
                raise _error('NATIVE_SOURCE_FORMAT_INVALID', 'OpenCode assistant 投影缺少 content 数组')
            texts = []
            for part in content:
                if not isinstance(part, dict):
                    raise _error('NATIVE_SOURCE_FORMAT_INVALID', 'OpenCode content 必须是投影对象')
                if part.get('type') not in {'text', 'reasoning', 'tool'}:
                    raise _error('NATIVE_SOURCE_FORMAT_INVALID', 'OpenCode content 不是已知投影类型')
                if part.get('type') == 'text':
                    if not isinstance(part.get('text'), str):
                        raise _error('NATIVE_SOURCE_FORMAT_INVALID', 'OpenCode 公开 text 格式无效')
                    texts.append(part['text'])
            text = '\n'.join(texts)
        time = item.get('time')
        stamp = (time.get('updated', time.get('created')) if isinstance(time, dict) else None)
        if stamp is None:
            stamp = summary['observed_at']
        if type(stamp) not in {int, float}:
            raise _error('NATIVE_SOURCE_FORMAT_INVALID', 'OpenCode 时间必须是毫秒时间戳')
        try:
            observed = dt.datetime.fromtimestamp(stamp / 1000, dt.timezone.utc).isoformat()
        except (ValueError, OverflowError, OSError):
            raise _error('NATIVE_SOURCE_FORMAT_INVALID', 'OpenCode 毫秒时间戳无效') from None
        locator = f'opencode://session/{quote(session_id, safe="")}/message/{quote(native_id, safe="")}'
        value = _message('opencode', session_id, native_id, role, text, locator, observed, summary['cwd'])
        if value:
            messages.append(value)
    return messages


def _opencode_sessions(root, session_id=None, message_id=None, *, metadata_only=False, ownership):
    path = root / 'opencode.db'
    if not path.is_file():
        raise _error('NATIVE_SOURCE_UNAVAILABLE', 'OpenCode provider 数据库不存在')
    try:
        connection = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
        try:
            query = 'SELECT id,directory,time_updated FROM session_v2'
            parameters = ()
            if session_id is not None:
                query += ' WHERE id=?'
                parameters = (session_id,)
            rows = connection.execute(query + ' ORDER BY id', parameters).fetchall()
        finally:
            connection.close()
    except sqlite3.Error:
        raise _error('NATIVE_SOURCE_FORMAT_INVALID', 'OpenCode 数据库不是已知 session_v2 schema') from None
    sessions = []
    for native_id, cwd, updated in rows:
        actual = ownership(cwd)
        if actual is None:
            continue
        summary = _summary(native_id, cwd, actual, 'opencode', path)
        summary['observed_at'] = updated
        messages = [] if metadata_only else _opencode_export(summary, native_id, message_id)
        sessions.append((summary, messages))
    return sessions


def _sessions(workspace, tool, home, session_id=None, message_id=None, *, metadata_only=False):
    root = _root(tool, home)
    context = _context(workspace)
    cache = {}

    def ownership(cwd):
        if not isinstance(cwd, str):
            return None
        if cwd not in cache:
            cache[cwd] = _belongs(cwd, context)
        return cache[cwd]

    if tool == 'workbuddy':
        return _workbuddy_sessions(context, root, session_id, message_id, metadata_only=metadata_only, ownership=ownership)
    if tool == 'opencode':
        return _opencode_sessions(root, session_id, message_id, metadata_only=metadata_only, ownership=ownership)
    if tool == 'zcode':
        return _zcode_sessions(context, root, session_id, message_id, metadata_only=metadata_only, ownership=ownership)
    return _file_sessions(context, tool, root, session_id, message_id, metadata_only=metadata_only, ownership=ownership)


def list_sessions(workspace: Path, tool: str, *, home: Path | None = None) -> list[dict]:
    """List session metadata without extracting message bodies."""
    return [summary for summary, _ in _sessions(workspace, tool, home, metadata_only=True)]


def read_messages(workspace: Path, tool: str, session_id: str, *, message_id: str | None = None, home: Path | None = None) -> list[dict]:
    """Return only public text; sensitive messages retain metadata without text."""
    messages = _read_provider_messages(workspace, tool, session_id, message_id, home)
    from .memory_state import source_suppressions
    suppressed = source_suppressions(_context(workspace))
    if message_id is not None and any('native-message.' + tool + ':' + message['locator'] in suppressed for message in messages):
        raise _error('SOURCE_SUPPRESSED', '该原生消息已被共享来源遗忘决定抑制')
    return [message for message in messages if 'native-message.' + tool + ':' + message['locator'] not in suppressed]


def _read_provider_messages(workspace, tool, session_id, message_id=None, home=None):
    # Internal source verification may run while the publication lock is held.
    # Public exports apply the durable suppression boundary after provider IO.
    return [message for _, messages in _sessions(workspace, tool, home, session_id, message_id) for message in messages]


def source_snapshot(workspace: Path, locator: str) -> dict:
    """Resolve canonical native evidence identity and revision without its body."""
    if not isinstance(locator, str) or '?' in locator or '#' in locator or '\x00' in locator:
        raise _error('NATIVE_MESSAGE_INVALID', '原生消息 locator 格式无效')
    try:
        parsed = urlsplit(locator)
        parts = parsed.path.split('/')
        if (parsed.scheme != 'native-message' or parsed.netloc not in TOOLS
                or len(parts) != 3 or parts[0] != ''):
            raise ValueError()
        session_id, message_id = (unquote(part, errors='strict') for part in parts[1:])
        if not session_id or not message_id or '\x00' in session_id or '\x00' in message_id:
            raise ValueError()
    except (ValueError, UnicodeError):
        raise _error('NATIVE_MESSAGE_INVALID', '原生消息 locator 格式无效') from None
    tool = parsed.netloc
    try:
        messages = _read_provider_messages(workspace, tool, session_id, message_id)
    except MemoryProtocolError as exc:
        if exc.code == 'NATIVE_SOURCE_INACTIVE':
            raise _error('NATIVE_MESSAGE_UNAVAILABLE', '原生消息不存在或不可见') from None
        raise
    if len(messages) != 1:
        raise _error('NATIVE_MESSAGE_UNAVAILABLE', '原生消息不存在或不唯一')
    message = messages[0]
    if message.get('status') == 'sensitive_text_omitted':
        raise _error('NATIVE_MESSAGE_SENSITIVE', '原生消息含敏感内容，不可作为来源')
    canonical = f'native-message://{tool}/{quote(session_id, safe="")}/{quote(message_id, safe="")}'
    return dict(source_instance='native-message.' + tool, object_id=canonical,
                locator=canonical, source_revision=message['source_revision'], role=message['role'])
