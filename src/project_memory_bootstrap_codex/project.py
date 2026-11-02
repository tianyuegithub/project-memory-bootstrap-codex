"""Explicit project membership and local workspace identity."""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path

from .errors import MemoryProtocolError


@dataclass(frozen=True)
class ProjectContext:
    root: Path
    project_id: str
    repository_id: str
    workspace_root: Path
    state_instance_id: str
    maintenance_enabled: bool
    registered: bool = False

    @property
    def memory_file(self) -> Path:
        return self.root / '.codex/memory/MEMORY.md'

    @property
    def state_dir(self) -> Path:
        return self.root / '.codex/memory-state'


def _error(message: str) -> MemoryProtocolError:
    return MemoryProtocolError('PROJECT_CONFIG_INVALID', message)


def _read(path: Path):
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        raise _error(f'无法读取项目登记文件 {path}: {exc}') from exc


def _write(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    name = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent, delete=False) as stream:
            name = stream.name
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if name and os.path.exists(name):
            os.unlink(name)


def _config(root: Path) -> dict:
    target = (root / '.project-memory.json').resolve()
    if root not in target.parents:
        raise _error('项目配置链接不得逃出登记根目录')
    value = _read(root / '.project-memory.json')
    if not isinstance(value, dict) or type(value.get('schema_version')) is not int or value['schema_version'] != 1:
        raise _error(f'无效项目配置 schema: {root}')
    if not isinstance(value.get('project_id'), str) or not value['project_id'].strip():
        raise _error(f'无效 project_id: {root}')
    try:
        uuid.UUID(value['state_instance_id'])
    except (KeyError, ValueError, TypeError, AttributeError) as exc:
        raise _error(f'无效 state_instance_id: {root}') from exc
    maintenance = value.get('maintenance')
    if not isinstance(maintenance, dict) or type(maintenance.get('enabled')) is not bool:
        raise _error(f'无效 maintenance.enabled: {root}')
    return value


def _members(root: Path, members) -> dict[str, Path]:
    if not isinstance(members, dict) or not members:
        raise _error(f'项目 members 必须是非空对象: {root}')
    result = {}
    for key, relative in members.items():
        if not isinstance(key, str) or not key.strip() or not isinstance(relative, str) or not relative or Path(relative).is_absolute():
            raise _error(f'无效项目成员: {root}')
        target = (root / relative).resolve()
        if target != root and root not in target.parents:
            raise _error(f'项目成员路径逃逸: {key}')
        result[key] = target
    return result


def _registry_path(path: Path | None) -> Path:
    selected = path if path is not None else os.environ.get('PROJECT_MEMORY_REGISTRY')
    return Path(selected).expanduser().resolve() if selected is not None else Path.home() / '.config/project-memory-bootstrap/projects.json'


def _registry(path: Path) -> dict:
    value = _read(path) if path.exists() else {'schema_version': 1, 'projects': []}
    if not isinstance(value, dict) or type(value.get('schema_version')) is not int or value['schema_version'] != 1 or not isinstance(value.get('projects'), list):
        raise _error(f'无效本机注册表: {path}')
    if any(not isinstance(item, str) or not Path(item).is_absolute() for item in value['projects']):
        raise _error(f'本机注册表必须记录绝对根路径: {path}')
    return value


def _aliases(root: Path, members: dict) -> list:
    path = root / '.codex/memory-state/workspaces.json'
    if root not in path.resolve().parents:
        raise _error('工作区别名存储不得逃出项目根目录')
    values = _read(path) if path.exists() else []
    if not isinstance(values, list):
        raise _error(f'无效工作区别名: {path}')
    for item in values:
        if not isinstance(item, dict) or not isinstance(item.get('workspace_root'), str) or not Path(item['workspace_root']).is_absolute() or not isinstance(item.get('repository_id'), str) or item['repository_id'] not in members:
            raise _error(f'无效工作区别名条目: {path}')
    return values


def _git(path: Path, option: str) -> Path | None:
    result = subprocess.run(['git', '-C', str(path), 'rev-parse', option], capture_output=True, text=True)
    if result.returncode:
        return None
    value = Path(result.stdout.strip())
    return (path / value).resolve() if not value.is_absolute() else value.resolve()


def _context(root: Path, config: dict, repository_id: str, workspace: Path, *, registered: bool = True) -> ProjectContext:
    return ProjectContext(root, config['project_id'], repository_id, workspace, config['state_instance_id'], config['maintenance']['enabled'] and registered, registered)


def resolve_context(path: Path, *, registry_path: Path | None = None) -> ProjectContext | None:
    path = Path(path).expanduser().resolve()
    if path.is_file():
        path = path.parent
    registered_roots = {Path(item).resolve() for item in _registry(_registry_path(registry_path))['projects']}
    roots = set(registered_roots)
    ancestors = [path, *path.parents]
    roots.update(parent for parent in ancestors if (parent / '.project-memory.json').exists())
    git_root = _git(path, '--show-toplevel')
    common = _git(path, '--git-common-dir') if git_root else None
    matches = []
    parent_project = False
    for root in sorted(roots):
        related = root in ancestors or (common is not None and root in common.parents)
        try:
            config = _config(root)
            members = _members(root, config.get('members'))
        except MemoryProtocolError:
            if related:
                raise
            # A retired or damaged unrelated registration must not disable this project.
            continue
        parent_project |= root in ancestors
        # Canonical members already have an exact identity. Only a different Git
        # root requires probing common-dir relationships for worktree discovery.
        options = [(key, git_root) for key, member in members.items() if git_root == member]
        for repository_id, member in (() if options else members.items()):
            if git_root:
                if git_root == member:
                    options.append((repository_id, git_root))
                elif common is not None and _git(member, '--show-toplevel') == member and _git(member, '--git-common-dir') == common:
                    options.append((repository_id, git_root))
            elif path == member or member in path.parents:
                options.append((repository_id, member))
        try:
            aliases = _aliases(root, members)
        except MemoryProtocolError:
            if related or options:
                raise
            continue
        for item in aliases:
            workspace = Path(item['workspace_root']).resolve()
            parent_project |= workspace in ancestors
            if (git_root == workspace) if git_root else (path == workspace or workspace in path.parents):
                options.append((item['repository_id'], workspace))
        matches.extend(_context(root, config, repo, workspace, registered=root in registered_roots) for repo, workspace in options)
    if not git_root and matches:
        longest = max(len(item.workspace_root.parts) for item in matches)
        matches = [item for item in matches if len(item.workspace_root.parts) == longest]
    unique = {(item.root, item.repository_id, item.workspace_root): item for item in matches}
    if len(unique) > 1:
        raise MemoryProtocolError('PROJECT_AMBIGUOUS', f'工作区存在多个登记归属: {path}')
    if unique:
        return next(iter(unique.values()))
    if parent_project:
        raise MemoryProtocolError('PROJECT_UNREGISTERED', f'工作区未登记为项目成员: {path}')
    return None


def register_project(root: Path, project_id: str, members: dict[str, str], *, maintenance_enabled: bool | None = None, registry_path: Path | None = None) -> ProjectContext:
    root = Path(root).expanduser().resolve()
    path = root / '.project-memory.json'
    if not isinstance(project_id, str) or not project_id.strip():
        raise _error('project_id 必须是非空字符串')
    if maintenance_enabled is not None and type(maintenance_enabled) is not bool:
        raise _error('maintenance_enabled 必须是布尔值')
    if not isinstance(members, dict):
        raise _error('members 必须是对象')
    config = _config(root) if path.exists() else {'schema_version': 1, 'project_id': project_id, 'state_instance_id': str(uuid.uuid4()), 'members': {}, 'maintenance': {'enabled': False}}
    if config['project_id'] != project_id:
        raise MemoryProtocolError('PROJECT_ID_MISMATCH', f'不能覆盖不同 project_id: {root}')
    if not isinstance(config.get('members'), dict):
        raise _error('已有项目members必须是对象')
    config['members'] = {**config['members'], **members}
    validated = _members(root, config['members'])
    if maintenance_enabled is not None:
        config['maintenance']['enabled'] = maintenance_enabled
    registry_file = _registry_path(registry_path)
    registry = _registry(registry_file)
    if str(root) not in registry['projects']:
        registry['projects'].append(str(root))
    # Root registration returns the root member when present, otherwise the first explicit member.
    repository_id = next((key for key, member in validated.items() if member == root), next(iter(validated)))
    _write(path, config)
    _write(registry_file, registry)
    return _context(root, config, repository_id, validated[repository_id])


def register_alias(root: Path, workspace: Path, repository_id: str, *, registry_path: Path | None = None) -> ProjectContext:
    root = Path(root).expanduser().resolve()
    workspace = Path(workspace).expanduser().resolve()
    config = _config(root)
    members = _members(root, config.get('members'))
    if repository_id not in members:
        raise MemoryProtocolError('PROJECT_UNREGISTERED', f'未知 repository_id: {repository_id}')
    aliases = _aliases(root, members)
    for item in aliases:
        if Path(item['workspace_root']).resolve() == workspace:
            if item['repository_id'] != repository_id:
                raise MemoryProtocolError('PROJECT_AMBIGUOUS', f'工作区已有不同归属: {workspace}')
            break
    else:
        aliases.append({'workspace_root': str(workspace), 'repository_id': repository_id})
    registry_file = _registry_path(registry_path)
    registry = _registry(registry_file)
    if str(root) not in registry['projects']:
        registry['projects'].append(str(root))
    _write(root / '.codex/memory-state/workspaces.json', aliases)
    _write(registry_file, registry)
    return _context(root, config, repository_id, workspace)
