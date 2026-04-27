"""Core logic for initializing project-level Codex memory files."""

from __future__ import annotations

import datetime as _dt
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .templates import AGENTS_TEMPLATE, CODEX_TEMPLATE, DAY_TEMPLATE, MEMORY_TEMPLATE, SCRIPT_EMPTY

CODEX_SECTIONS = [
    "项目定位",
    "模块结构",
    "关键链路",
    "运行入口与端口",
    "脚本索引",
    "上下文来源优先级",
    "记忆治理规则",
]

SCRIPT_DIRS = ["scripts", "bin", "tools", "deploy"]


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


def write_if_needed(path: Path, content: str, force: bool = False) -> WriteResult:
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
    with_agents: bool = False,
) -> list[WriteResult]:
    root = find_project_root(path)
    date = today_string(today)
    project_name = root.name
    script_index = render_script_index(root)

    results = [
        write_if_needed(
            root / "CODEX.md",
            CODEX_TEMPLATE.format(project_name=project_name, script_index=script_index),
            force=force,
        ),
        write_if_needed(
            root / ".codex" / "memory" / "MEMORY.md",
            MEMORY_TEMPLATE.format(today=date),
            force=force,
        ),
        write_if_needed(
            root / ".codex" / "memory" / f"{date}.md",
            DAY_TEMPLATE.format(today=date),
            force=force,
        ),
    ]

    if with_agents:
        results.append(write_if_needed(root / "AGENTS.md", AGENTS_TEMPLATE, force=force))

    return results


def doctor(path: Path) -> DoctorResult:
    root = find_project_root(path)
    messages: list[str] = []
    ok = True

    required = [
        root / "CODEX.md",
        root / ".codex" / "memory" / "MEMORY.md",
    ]
    for item in required:
        if item.exists():
            messages.append(f"ok: {item.relative_to(root)} exists")
        else:
            ok = False
            messages.append(f"missing: {item.relative_to(root)}")

    codex_path = root / "CODEX.md"
    if codex_path.exists():
        text = codex_path.read_text(encoding="utf-8")
        for section in CODEX_SECTIONS:
            marker = f"## {section}"
            if marker in text:
                messages.append(f"ok: CODEX.md has section {section}")
            else:
                ok = False
                messages.append(f"missing: CODEX.md section {section}")

    memory_path = root / ".codex" / "memory" / "MEMORY.md"
    if memory_path.exists():
        text = memory_path.read_text(encoding="utf-8")
        for field in ["类型", "范围", "来源", "最近核验", "稳定性", "失效条件", "替代关系"]:
            if f"- {field}:" in text:
                messages.append(f"ok: MEMORY.md contains metadata field {field}")
            else:
                ok = False
                messages.append(f"missing: MEMORY.md metadata field {field}")

    day_files = sorted((root / ".codex" / "memory").glob("20??-??-??.md"))
    if day_files:
        messages.append(f"ok: found {len(day_files)} day memory file(s)")
    else:
        ok = False
        messages.append("missing: .codex/memory/YYYY-MM-DD.md")

    return DoctorResult(ok=ok, messages=messages)
