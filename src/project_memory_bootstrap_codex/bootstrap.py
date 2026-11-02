"""Core logic for initializing project-level Codex memory files."""

from __future__ import annotations

import datetime as _dt
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .errors import MemoryProtocolError
from .facts import parse_facts
from .project import resolve_context
from .templates import AGENTS_TEMPLATE, README_TEMPLATE, DAY_TEMPLATE, MEMORY_TEMPLATE, SCRIPT_EMPTY

SCRIPT_DIRS = ["scripts", "bin", "tools", "deploy"]
SENSITIVE_VALUE_PATTERNS = [
    re.compile(r"(?i)\b(password|secret|token|api[_-]?key)\s*[:=]\s*(?!\.\.\.|<|\$|\*)[^\s`'\"<>]{8,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{20,}"),
    re.compile(r"(?i)\b[a-z][a-z0-9+.-]*://[^/\s:@]+:[^/\s:@]+@"),
]


@dataclass(frozen=True)
class WriteResult:
    path: Path
    status: str


@dataclass(frozen=True)
class DoctorResult:
    ok: bool
    messages: list[str]


def find_project_root(path: Path) -> Path:
    """Return the Git root for path, or the path itself when it is not in Git."""

    context = resolve_context(path)
    if context is not None:
        return context.root

    resolved = path.expanduser().resolve()
    base = resolved if resolved.is_dir() else resolved.parent
    try:
        output = subprocess.check_output(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=base,
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
    except (FileNotFoundError, subprocess.CalledProcessError):
        return base
    return Path(output).resolve()


def today_string(value: str | None = None) -> str:
    if value:
        _dt.date.fromisoformat(value)
        return value
    return _dt.date.today().isoformat()


def scan_shell_scripts(root: Path) -> list[Path]:
    candidates: list[Path] = []
    candidates.extend(root.glob("*.sh"))
    for dirname in SCRIPT_DIRS:
        directory = root / dirname
        if directory.exists():
            candidates.extend(directory.rglob("*.sh"))
    return sorted({path.resolve() for path in candidates if path.is_file()})


def render_script_index(root: Path) -> str:
    scripts = scan_shell_scripts(root)
    if not scripts:
        return SCRIPT_EMPTY

    lines = []
    for script in scripts:
        rel = script.relative_to(root)
        lines.append(
            f"- `{rel}`: 待补充。请基于脚本正文核实用途、依赖服务、关键端口、主要输入输出或用法。"
        )
    return "\n".join(lines)


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _ensure_safe_write_target(path: Path, root: Path) -> None:
    root_resolved = root.resolve()
    parent_resolved = path.parent.resolve(strict=False)
    target_resolved = path.resolve(strict=False)
    if not _is_relative_to(parent_resolved, root_resolved):
        raise ValueError(f"refusing to create directories outside project root: {path.parent}")
    if not _is_relative_to(target_resolved, root_resolved):
        raise ValueError(f"refusing to write outside project root: {path}")


def write_if_needed(path: Path, content: str, force: bool = False, *, root: Path | None = None) -> WriteResult:
    if root is not None:
        _ensure_safe_write_target(path, root)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and not force:
        return WriteResult(path=path, status="skipped")
    path.write_text(content.rstrip() + "\n", encoding="utf-8")
    return WriteResult(path=path, status="written")


def init_project(
    path: Path,
    *,
    today: str | None = None,
    force: bool = False,
    with_agents: bool = True,
) -> list[WriteResult]:
    root = find_project_root(path)
    if (root / ".project-memory.json").exists():
        if force:
            raise MemoryProtocolError("GOVERNED_INIT_FORBIDDEN", "Initialization cannot overwrite a registered project's managed files.")
        if (root / ".codex/memory-state/ledger.sqlite").exists() and not (root / ".codex/memory/MEMORY.md").exists():
            raise MemoryProtocolError("MEMORY_MISSING", "Published memory is missing; initialization must not replace it with an empty document.")
    date = today_string(today)
    project_name = root.name

    results = [
        write_if_needed(
            root / "README.md",
            README_TEMPLATE.format(project_name=project_name),
            force=force,
            root=root,
        ),
        write_if_needed(
            root / ".codex" / "memory" / "MEMORY.md",
            MEMORY_TEMPLATE,
            force=force,
            root=root,
        ),
    ]

    if today is not None:
        results.append(write_if_needed(
            root / ".codex" / "memory" / f"{date}.md",
            DAY_TEMPLATE.format(today=date), force=force, root=root,
        ))

    if with_agents:
        results.append(write_if_needed(root / "AGENTS.md", AGENTS_TEMPLATE, force=force, root=root))

    return results


def doctor(path: Path) -> DoctorResult:
    root = find_project_root(path)
    messages: list[str] = []
    ok = True

    required = [
        root / "AGENTS.md",
        root / "README.md",
        root / ".codex" / "memory" / "MEMORY.md",
    ]
    for item in required:
        if item.exists():
            messages.append(f"ok: {item.relative_to(root)} exists")
        else:
            ok = False
            messages.append(f"missing: {item.relative_to(root)}")

    memory_path = root / ".codex" / "memory" / "MEMORY.md"
    if memory_path.exists():
        text = memory_path.read_text(encoding="utf-8")
        if "<!-- project-memory-fact" in text or "<!-- /project-memory-fact" in text:
            try:
                facts = parse_facts(text)
            except MemoryProtocolError as exc:
                ok = False
                messages.append(f"invalid-facts: {exc}")
            else:
                messages.append(f"ok: MEMORY.md validated {len(facts)} managed fact(s)")
        elif text.strip() and text.strip() != MEMORY_TEMPLATE.strip():
            messages.append("legacy: MEMORY.md contains unstructured memory; facts need verification")
        else:
            messages.append("info: MEMORY.md has no managed facts")
        stale_dates = _stale_verification_dates(text, today=_dt.date.today())
        if stale_dates:
            preview = ", ".join(stale_dates[:10])
            messages.append(f"warn: legacy 最近核验 older than 30 days (age alone does not invalidate facts): {preview}")

    sensitive_hits = _sensitive_memory_hits(root)
    if sensitive_hits:
        preview = ", ".join(sensitive_hits[:10])
        suffix = "" if len(sensitive_hits) <= 10 else f", ... {len(sensitive_hits) - 10} more"
        messages.append(f"warn: possible sensitive values in memory files: {preview}{suffix}")

    tracked_cache = _tracked_codex_cache_files(root)
    if tracked_cache:
        ok = False
        preview = ", ".join(tracked_cache[:10])
        suffix = "" if len(tracked_cache) <= 10 else f", ... {len(tracked_cache) - 10} more"
        messages.append(f"tracked-cache: local cache/state files are tracked by git: {preview}{suffix}")

    if (root / ".project-memory.json").exists():
        from .memory_state import health
        try:
            state = health(root)
            if state["status"] != "ok":
                ok = False
            messages.append(f"state: {state['status']}")
        except MemoryProtocolError as exc:
            ok = False
            messages.append(f"state: {exc.code}: {exc}")

    return DoctorResult(ok=ok, messages=messages)


def _stale_verification_dates(text: str, *, today: _dt.date) -> list[str]:
    stale: list[str] = []
    for match in re.finditer(r"^\s*-\s*最近核验\s*[:：]\s*(\d{4}-\d{2}-\d{2})\b", text, flags=re.MULTILINE):
        try:
            verified_at = _dt.date.fromisoformat(match.group(1))
        except ValueError:
            continue
        if (today - verified_at).days > 30:
            stale.append(match.group(1))
    return stale


def _sensitive_memory_hits(root: Path) -> list[str]:
    memory_dir = root / ".codex" / "memory"
    if not memory_dir.is_dir():
        return []

    hits: list[str] = []
    for path in sorted(memory_dir.glob("*.md")):
        rel = path.relative_to(root).as_posix()
        for lineno, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), start=1):
            for pattern in SENSITIVE_VALUE_PATTERNS:
                if pattern.search(line):
                    hits.append(f"{rel}:{lineno}")
                    break
    return hits


def _tracked_codex_cache_files(root: Path) -> list[str]:
    try:
        output = subprocess.check_output(
            ["git", "ls-files", ".codex/cache", ".codex/memory-state", ".state"],
            cwd=root,
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        return []
    return [line.strip() for line in output.splitlines() if line.strip()]
