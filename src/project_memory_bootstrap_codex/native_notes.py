"""Read registered projects' native topic notes through proven default routes only."""
from __future__ import annotations

import hashlib
import re
import subprocess
from pathlib import Path, PurePosixPath
from urllib.parse import quote, unquote, urlsplit

from .errors import MemoryProtocolError
from .facts import SENSITIVE_RE, reject_sensitive, native_credential_context
from .project import ProjectContext, _aliases, _config, _members, resolve_context

TOOLS = ("zcode", "claude", "workbuddy")
NOTE_TYPES = {"user", "project", "feedback", "reference"}


def _provider_base(tool: str) -> Path:
    home = Path.home()
    return {"zcode": home / ".zcode/cli/memories", "claude": home / ".claude", "workbuddy": home / ".workbuddy"}[tool]


def _registered(workspace: Path) -> ProjectContext:
    context = resolve_context(workspace)
    if context is None or not context.registered:
        raise MemoryProtocolError("PROJECT_UNREGISTERED", "原生记忆读取要求本机已登记项目。")
    return context


def _same_project(context: ProjectContext, other: ProjectContext) -> bool:
    return (context.root, context.project_id, context.state_instance_id) == (other.root, other.project_id, other.state_instance_id)


def _workspaces(context: ProjectContext) -> list[ProjectContext]:
    config = _config(context.root)
    members = _members(context.root, config.get("members"))
    paths = set(members.values()) | {context.workspace_root}
    paths.update(Path(item["workspace_root"]).resolve() for item in _aliases(context.root, members))
    for member in members.values():
        result = subprocess.run(["git", "-C", str(member), "worktree", "list", "--porcelain", "-z"], capture_output=True, text=True)
        if result.returncode == 0:
            paths.update(Path(line[len("worktree "):]).resolve() for line in result.stdout.split("\0") if line.startswith("worktree "))
    resolved = {}
    for path in sorted(paths):
        if not path.is_dir():
            continue
        try:
            other = resolve_context(path)
        except MemoryProtocolError as exc:
            if exc.code == "PROJECT_UNREGISTERED":
                continue
            raise
        if other is not None and other.registered and _same_project(context, other):
            resolved[other.workspace_root] = other
    return list(resolved.values())


def _memory_root(tool: str, workspace: Path) -> tuple[Path, str]:
    if tool not in TOOLS:
        raise MemoryProtocolError("NATIVE_MEMORY_TOOL_UNSUPPORTED", "不支持的原生记忆工具。")
    base = _provider_base(tool).resolve()
    route = "default"
    if tool == "zcode":
        suffix = hashlib.sha256(str(workspace).encode("utf-8")).hexdigest()[:16]
        candidates = sorted((base / "projects").glob(f"*-{suffix}/memory"))
        if len(candidates) > 1:
            raise MemoryProtocolError("NATIVE_MEMORY_ROOT_AMBIGUOUS", "同一工作区哈希对应多个原生记忆目录，不能猜测。")
        if not candidates:
            raise MemoryProtocolError("NATIVE_MEMORY_ROOT_UNAVAILABLE", "未发现已验证默认路由的原生记忆目录；自定义身份或启动覆盖未覆盖。")
        root = candidates[0]
    elif tool == "claude":
        root = base / "projects" / re.sub(r"[^a-zA-Z0-9]", "-", str(workspace)) / "memory"
    else:
        identity = re.sub(r"[^a-zA-Z0-9]", "-", str(workspace)).lstrip("-")
        root = base / "projects" / identity / "memory"
        if not root.exists():
            legacy = base / "memories" / str(workspace).lstrip("/")
            if legacy.is_dir():
                root, route = legacy, "legacy-existing"
    if not root.is_dir():
        raise MemoryProtocolError("NATIVE_MEMORY_ROOT_UNAVAILABLE", "未发现已验证默认路由的原生记忆目录；自定义身份或启动覆盖未覆盖。")
    if root.resolve() != root.absolute() or base not in root.resolve().parents:
        raise MemoryProtocolError("NATIVE_MEMORY_PATH_ESCAPE", "原生记忆根目录不得通过符号链接改向其他目录。")
    return root, route


def _locator(tool: str, workspace: Path, topic: str) -> str:
    return f"native-memory://{tool}/{quote(str(workspace), safe='')}/{quote(topic, safe='')}"


def _topic_path(root: Path, topic: str) -> Path:
    relative = PurePosixPath(topic)
    if not topic or "\0" in topic or relative.is_absolute() or ".." in relative.parts or "\\" in topic or relative.as_posix() != topic or relative.suffix != ".md" or relative.name == "MEMORY.md":
        raise MemoryProtocolError("NATIVE_MEMORY_LOCATOR_INVALID", "主题必须是受限相对路径的 .md 文件，不能指向索引。")
    path = root / topic
    if root not in path.resolve().parents:
        raise MemoryProtocolError("NATIVE_MEMORY_PATH_ESCAPE", "主题符号链接不得逃出对应原生记忆根目录。")
    return path


def _resolve_locator(workspace: Path, locator: str) -> tuple[str, ProjectContext, Path, str]:
    context = _registered(workspace)
    parsed = urlsplit(locator)
    parts = parsed.path.split("/")
    if parsed.scheme != "native-memory" or parsed.netloc not in TOOLS or parsed.query or parsed.fragment or len(parts) != 3 or parts[0]:
        raise MemoryProtocolError("NATIVE_MEMORY_LOCATOR_INVALID", "原生记忆 locator 格式无效。")
    workspace_text, topic = unquote(parts[1]), unquote(parts[2])
    if "\0" in workspace_text:
        raise MemoryProtocolError("NATIVE_MEMORY_LOCATOR_INVALID", "locator 工作区不得包含空字节。")
    source_workspace = Path(workspace_text)
    if not source_workspace.is_absolute() or str(source_workspace.resolve()) != workspace_text:
        raise MemoryProtocolError("NATIVE_MEMORY_LOCATOR_INVALID", "locator 工作区必须是规范绝对路径。")
    other = _registered(source_workspace)
    if other.workspace_root != source_workspace or not _same_project(context, other):
        raise MemoryProtocolError("NATIVE_MEMORY_SCOPE_MISMATCH", "locator 工作区必须精确属于当前登记项目。")
    root, _ = _memory_root(parsed.netloc, source_workspace)
    path = _topic_path(root, topic)
    return parsed.netloc, other, path, _locator(parsed.netloc, source_workspace, topic)


def _parse(raw: bytes, tool: str) -> tuple[dict, str]:
    try:
        import yaml
    except ImportError as exc:
        raise MemoryProtocolError("NATIVE_MEMORY_PARSER_UNAVAILABLE", "解析原生记忆需要可用的 PyYAML。") from exc
    try:
        text = raw.decode("utf-8")
        lines = text.splitlines(keepends=True)
        if not lines or lines[0].strip() != "---":
            raise ValueError("frontmatter missing")
        end = next(index for index in range(1, len(lines)) if lines[index].strip() == "---")
        data = yaml.safe_load("".join(lines[1:end]))
    except (UnicodeError, ValueError, StopIteration, yaml.YAMLError):
        raise MemoryProtocolError("FORMAT_INVALID", "原生记忆 frontmatter 无效，不能把未知正文作为已解析主题。") from None
    if not isinstance(data, dict) or not isinstance(data.get("name"), str) or not isinstance(data.get("description"), str):
        raise MemoryProtocolError("FORMAT_INVALID", "主题必须具有字符串 name 和 description。")
    origin = None
    if tool == "zcode":
        meta = data.get("metadata")
        if not isinstance(meta, dict) or meta.get("node_type") != "memory" or not isinstance(meta.get("type"), str) or meta["type"] not in NOTE_TYPES:
            raise MemoryProtocolError("FORMAT_INVALID", "ZCode 主题必须声明 memory node_type 和已知 type。")
        note_type = meta["type"]
        origin = meta.get("originSessionId", data.get("originSessionId"))
        if origin is not None and not isinstance(origin, str):
            raise MemoryProtocolError("FORMAT_INVALID", "originSessionId 必须是字符串。")
    else:
        note_type = data.get("type")
        if not isinstance(note_type, str) or note_type not in NOTE_TYPES:
            raise MemoryProtocolError("FORMAT_INVALID", "主题 type 必须符合公开的 name/description/type schema。")
    metadata = {"title": data["name"], "description": data["description"], "note_type": note_type}
    if origin is not None:
        metadata["origin_session_id"] = origin
    try:
        reject_sensitive(metadata)
        if any(native_credential_context(value) for value in metadata.values()):
            raise MemoryProtocolError('NATIVE_MEMORY_SENSITIVE', '原生元数据需要整理后的安全证据。')
    except MemoryProtocolError:
        metadata = {"sensitive_metadata_omitted": True, "reason": "credential_context_requires_curated_evidence"}
    return metadata, "".join(lines[end + 1:])


def _read(tool: str, context: ProjectContext, path: Path, locator: str) -> tuple[dict, str]:
    if not path.is_file():
        raise MemoryProtocolError("NATIVE_MEMORY_TOPIC_MISSING", "原生记忆主题不存在。")
    raw = path.read_bytes()
    metadata, body = _parse(raw, tool)
    metadata.update(tool=tool, workspace=str(context.workspace_root), repo=context.repository_id,
                    source_revision=hashlib.sha256(raw).hexdigest(), mtime=path.stat().st_mtime, locator=locator)
    return metadata, body


def list_notes(workspace: Path, *, tools: tuple = TOOLS, query: str | None = None) -> dict:
    context = _registered(workspace)
    workspaces = _workspaces(context)
    notes, coverage, errors = [], {}, []
    for tool in dict.fromkeys(tools):
        coverage[tool] = {"workspaces": [], "not_covered": ["custom workspaceIdentity", "启动时路径或存储覆盖"]}
        for source in workspaces:
            status = {"workspace": str(source.workspace_root), "repo": source.repository_id}
            try:
                root, route = _memory_root(tool, source.workspace_root)
                status.update(status="default_root_found", route=route)
                for path in sorted(root.rglob("*")):
                    topic = path.relative_to(root).as_posix()
                    locator = _locator(tool, source.workspace_root, topic)
                    try:
                        if path.is_symlink() and root not in path.resolve().parents:
                            raise MemoryProtocolError("NATIVE_MEMORY_PATH_ESCAPE", "主题或目录符号链接不得逃出对应原生记忆根目录。")
                        if path.suffix != ".md" or path.name == "MEMORY.md" or path.is_dir():
                            continue
                        metadata, _ = _read(tool, source, _topic_path(root, topic), locator)
                    except MemoryProtocolError as exc:
                        errors.append({**exc.as_dict(), "tool": tool, "workspace": str(source.workspace_root), "locator": locator})
                        continue
                    searchable = " ".join([topic, *(str(metadata.get(key, "")) for key in ("title", "description", "note_type", "origin_session_id"))])
                    if query is None or query.casefold() in searchable.casefold():
                        notes.append(metadata)
            except MemoryProtocolError as exc:
                status.update(status=exc.code)
                if exc.code != "NATIVE_MEMORY_ROOT_UNAVAILABLE":
                    errors.append({**exc.as_dict(), "tool": tool, "workspace": str(source.workspace_root)})
            coverage[tool]["workspaces"].append(status)
    from .memory_state import source_suppressions
    suppressed = source_suppressions(context)
    notes = [note for note in notes if 'native-memory.' + note['tool'] + ':' + note['locator'] not in suppressed]
    errors = [error for error in errors if 'locator' not in error or 'native-memory.' + error['tool'] + ':' + error['locator'] not in suppressed]
    return {"notes": notes, "coverage": coverage, "errors": errors}


def read_note(workspace: Path, locator: str) -> dict:
    tool, context, path, canonical = _resolve_locator(workspace, locator)
    metadata, body = _read(tool, context, path, canonical)
    from .memory_state import source_suppressions
    if 'native-memory.' + tool + ':' + canonical in source_suppressions(context):
        raise MemoryProtocolError('SOURCE_SUPPRESSED', '该原生笔记已被共享来源遗忘决定抑制。')
    if metadata.get('sensitive_metadata_omitted') or SENSITIVE_RE.search(body) or native_credential_context(body):
        metadata["sensitive_text_omitted"] = True
        metadata["reason"] = "credential_context_requires_curated_evidence"
    else:
        metadata["text"] = body
    return metadata


def source_snapshot(workspace: Path, locator: str, *, allow_missing: bool = False) -> dict:
    tool, _, path, canonical = _resolve_locator(workspace, locator)
    if not path.is_file():
        if not allow_missing or path.exists():
            raise MemoryProtocolError("NATIVE_MEMORY_TOPIC_MISSING", "原生记忆主题不存在。")
        revision = "missing"
    else:
        raw = path.read_bytes()
        try:
            text = raw.decode('utf-8')
        except UnicodeError:
            raise MemoryProtocolError('FORMAT_INVALID', '原生记忆不是 UTF-8 文本。') from None
        if SENSITIVE_RE.search(text) or native_credential_context(text):
            raise MemoryProtocolError('NATIVE_MEMORY_SENSITIVE', '原生记忆上下文需要整理后的安全证据。')
        revision = hashlib.sha256(raw).hexdigest()
    return {"source_instance": "native-memory." + tool, "object_id": canonical, "locator": canonical, "source_revision": revision}
