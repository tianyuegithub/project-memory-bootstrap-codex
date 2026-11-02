"""Consolidation reports and automated cleanup for project memory governance."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .bootstrap import find_project_root
from .memory_index import (
    IndexStatus,
    MemoryChunk,
    index_status,
    load_chunks,
    rebuild_index,
)

REPORTS_RELATIVE_DIR = Path(".codex") / "cache" / "memory-reports"
ARCHIVE_RELATIVE_DIR = Path(".codex") / "cache" / "memory-archive"
REPORT_DIR_ENV = "PROJECT_MEMORY_BOOTSTRAP_REPORT_DIR"
STALE_DAYS = 30
ARCHIVE_DAY_MEMORY_OLDER_THAN_DAYS = 180


@dataclass(frozen=True)
class ConsolidationItem:
    id: str
    kind: str
    risk: str
    title: str
    evidence: tuple[str, ...]
    suggested_action: str
    safe_apply: bool
    affected_files: tuple[str, ...]
    preview: str


@dataclass(frozen=True)
class ConsolidationReport:
    root: Path
    timestamp: str
    markdown_path: Path
    json_path: Path
    items: tuple[ConsolidationItem, ...]


@dataclass(frozen=True)
class ApplyResult:
    applied: tuple[str, ...]
    skipped: tuple[str, ...]
    messages: tuple[str, ...]


@dataclass(frozen=True)
class AgentApplyResult:
    report: ConsolidationReport
    applied: tuple[str, ...]
    skipped: tuple[str, ...]
    archived: tuple[str, ...]
    cleaned: tuple[str, ...]
    messages: tuple[str, ...]


@dataclass(frozen=True)
class SectionCleanup:
    rel_path: str
    removed_titles: tuple[str, ...]
    archive_path: Path
    deleted_file: bool = False


def reports_dir(root: Path) -> Path:
    return root / REPORTS_RELATIVE_DIR


def fallback_reports_dir(root: Path) -> Path:
    digest = hashlib.sha1(str(root.resolve()).encode("utf-8")).hexdigest()[:12]
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", root.name).strip("-") or "project"
    return Path(tempfile.gettempdir()) / "project-memory-bootstrap-codex" / f"{slug}-{digest}" / "memory-reports"


def archive_dir(root: Path) -> Path:
    return root / ARCHIVE_RELATIVE_DIR


def _resolve_report_dir(root: Path, report_dir: Path | None) -> Path:
    if report_dir is not None:
        return _normalize_report_dir(report_dir)
    env_report_dir = os.environ.get(REPORT_DIR_ENV)
    if env_report_dir:
        return _normalize_report_dir(Path(env_report_dir))
    return reports_dir(root)


def _report_fallback_allowed(report_dir: Path | None) -> bool:
    return report_dir is None and not os.environ.get(REPORT_DIR_ENV)


def _normalize_report_dir(report_dir: Path) -> Path:
    expanded = report_dir.expanduser()
    return expanded if expanded.is_absolute() else expanded.resolve()


def _candidate_report_dirs(root: Path) -> list[Path]:
    candidates = [reports_dir(root)]
    env_report_dir = os.environ.get(REPORT_DIR_ENV)
    if env_report_dir:
        candidates.append(_normalize_report_dir(Path(env_report_dir)))
    candidates.append(fallback_reports_dir(root))

    unique: list[Path] = []
    seen: set[Path] = set()
    for candidate in candidates:
        key = candidate.resolve() if candidate.exists() else candidate.absolute()
        if key in seen:
            continue
        seen.add(key)
        unique.append(candidate)
    return unique


def _build_report(
    root: Path,
    timestamp: str,
    items: tuple[ConsolidationItem, ...],
    target_dir: Path,
) -> ConsolidationReport:
    return ConsolidationReport(
        root=root,
        timestamp=timestamp,
        markdown_path=target_dir / f"{timestamp}-consolidation.md",
        json_path=target_dir / f"{timestamp}-consolidation.json",
        items=items,
    )


def _write_report_files(report: ConsolidationReport) -> None:
    report.markdown_path.parent.mkdir(parents=True, exist_ok=True)
    _best_effort_chmod(report.markdown_path.parent, 0o700)
    report.markdown_path.write_text(render_report_markdown(report), encoding="utf-8")
    _best_effort_chmod(report.markdown_path, 0o600)
    report.json_path.write_text(render_report_json(report), encoding="utf-8")
    _best_effort_chmod(report.json_path, 0o600)


def _best_effort_chmod(path: Path, mode: int) -> None:
    try:
        path.chmod(mode)
    except OSError:
        pass


def generate_consolidation_report(
    path: Path,
    *,
    now: dt.datetime | None = None,
    write: bool = True,
    report_dir: Path | None = None,
) -> ConsolidationReport:
    root = find_project_root(path)
    current_time = now or dt.datetime.now().replace(microsecond=0)
    timestamp = current_time.strftime("%Y%m%d-%H%M%S")
    items = tuple(_collect_items(root, current_time.date()))
    target_dir = _resolve_report_dir(root, report_dir)
    report = _build_report(root, timestamp, items, target_dir)

    if write:
        try:
            _write_report_files(report)
        except PermissionError:
            if not _report_fallback_allowed(report_dir):
                raise
            fallback_report = _build_report(root, timestamp, items, fallback_reports_dir(root))
            if fallback_report.markdown_path == report.markdown_path:
                raise
            _write_report_files(fallback_report)
            report = fallback_report

    return report


def list_reports(path: Path) -> list[ConsolidationReport]:
    root = find_project_root(path)
    reports: list[ConsolidationReport] = []
    seen: set[Path] = set()
    for target_dir in _candidate_report_dirs(root):
        for json_path in sorted(target_dir.glob("*-consolidation.json")):
            resolved_path = json_path.resolve()
            if resolved_path in seen:
                continue
            seen.add(resolved_path)
            report = _load_report_json(root, json_path)
            if report is not None:
                reports.append(report)
    return sorted(reports, key=lambda report: (report.timestamp, str(report.json_path)))


def latest_report(path: Path) -> ConsolidationReport:
    reports = list_reports(path)
    if not reports:
        raise FileNotFoundError("no consolidation reports found")
    return reports[-1]


def apply_consolidation_report(
    path: Path,
    *,
    from_report: str = "latest",
    only: Iterable[str] = (),
    safe: bool = True,
) -> ApplyResult:
    root = find_project_root(path)
    report = latest_report(root) if from_report == "latest" else _load_report_json(root, Path(from_report))
    if report is None:
        raise FileNotFoundError(f"consolidation report not found: {from_report}")

    requested = _normalize_ids(only)
    by_id = {item.id: item for item in report.items}
    selected = [by_id[item_id] for item_id in requested if item_id in by_id] if requested else list(report.items)
    applied: list[str] = []
    skipped: list[str] = []
    messages: list[str] = []

    for item in selected:
        if safe and not item.safe_apply:
            skipped.append(item.id)
            messages.append(f"skipped {item.id}: not marked safe_apply")
            continue
        if item.kind == "index":
            rebuild_index(root)
            applied.append(item.id)
            messages.append(f"applied {item.id}: rebuilt memory index")
            continue
        skipped.append(item.id)
        messages.append(f"skipped {item.id}: no automatic apply handler")

    for item_id in requested:
        if item_id not in by_id:
            skipped.append(item_id)
            messages.append(f"skipped {item_id}: not found in report")

    return ApplyResult(applied=tuple(applied), skipped=tuple(skipped), messages=tuple(messages))


def agent_apply_consolidation(
    path: Path,
    *,
    now: dt.datetime | None = None,
    archive_day_memory_older_than_days: int = ARCHIVE_DAY_MEMORY_OLDER_THAN_DAYS,
    report_dir: Path | None = None,
) -> AgentApplyResult:
    """Generate a fresh report and let the deterministic cleanup agent apply it.

    The agent only performs actions that can be proven from local files:
    rebuilding the local index, retiring duplicate legacy/worktree memory files,
    and retiring old day-memory files that have no promotable or conflict events.
    Retired files are moved into .codex/cache/memory-archive so they disappear
    from the active memory index but remain recoverable.
    """

    root = find_project_root(path)
    current_time = now or dt.datetime.now().replace(microsecond=0)
    report = generate_consolidation_report(root, now=current_time, write=True, report_dir=report_dir)
    timestamp = report.timestamp
    applied: list[str] = []
    skipped: list[str] = []
    archived: list[str] = []
    cleaned: list[str] = []
    messages: list[str] = [
        f"agent report: {report.markdown_path}",
        f"agent json: {report.json_path}",
    ]

    for item in report.items:
        if item.kind == "index":
            rebuild_index(root)
            applied.append(item.id)
            messages.append(f"applied {item.id}: rebuilt memory index")
            continue
        skipped.append(item.id)
        messages.append(f"agent reviewed {item.id}: no direct item handler")

    duplicate_paths = _auto_duplicate_archive_paths(root)
    expired_day_paths = _expired_day_memory_archive_paths(
        root,
        current_time.date(),
        archive_day_memory_older_than_days,
    )
    for rel_path in sorted(set(duplicate_paths + expired_day_paths)):
        archived_path = _archive_active_memory_file(root, rel_path, timestamp)
        archived.append(rel_path)
        messages.append(f"archived {rel_path}: {archived_path}")

    section_cleanups = _auto_duplicate_section_cleanups(root, timestamp, excluded_paths=set(archived))
    for cleanup in section_cleanups:
        cleaned.extend(f"{cleanup.rel_path}#{title}" for title in cleanup.removed_titles)
        action = "deleted empty file after section cleanup" if cleanup.deleted_file else "removed duplicate sections"
        messages.append(
            f"cleaned {cleanup.rel_path}: {action}; archive: {cleanup.archive_path}; "
            f"sections: {', '.join(cleanup.removed_titles)}"
        )

    if archived or cleaned:
        rebuild_index(root)
        messages.append("applied archive cleanup: rebuilt memory index")

    return AgentApplyResult(
        report=report,
        applied=tuple(applied),
        skipped=tuple(skipped),
        archived=tuple(archived),
        cleaned=tuple(cleaned),
        messages=tuple(messages),
    )


def render_report_markdown(report: ConsolidationReport) -> str:
    counts: dict[str, int] = {}
    for item in report.items:
        counts[item.kind] = counts.get(item.kind, 0) + 1
    summary = ", ".join(f"{kind}: {count}" for kind, count in sorted(counts.items())) or "no findings"
    lines = [
        "# Memory Consolidation Report",
        "",
        f"- Root: `{report.root}`",
        f"- Timestamp: `{report.timestamp}`",
        f"- Items: {len(report.items)}",
        f"- Summary: {summary}",
        "",
    ]
    for item in report.items:
        lines.extend(
            [
                f"## {item.id}: {item.title}",
                f"- Type: {item.kind}",
                f"- Risk: {item.risk}",
                f"- Safe apply: {'yes' if item.safe_apply else 'no'}",
                f"- Evidence: {_join_inline(item.evidence)}",
                f"- Affected files: {_join_inline(item.affected_files)}",
                f"- Suggested action: {item.suggested_action}",
                "- Preview:",
                "```text",
                item.preview,
                "```",
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def render_report_json(report: ConsolidationReport) -> str:
    return json.dumps(
        {
            "root": str(report.root),
            "timestamp": report.timestamp,
            "markdown_path": str(report.markdown_path),
            "json_path": str(report.json_path),
            "items": [
                {
                    "id": item.id,
                    "kind": item.kind,
                    "risk": item.risk,
                    "title": item.title,
                    "evidence": list(item.evidence),
                    "suggested_action": item.suggested_action,
                    "safe_apply": item.safe_apply,
                    "affected_files": list(item.affected_files),
                    "preview": item.preview,
                }
                for item in report.items
            ],
        },
        ensure_ascii=False,
        indent=2,
    )


def format_report_list(reports: list[ConsolidationReport]) -> str:
    if not reports:
        return "no consolidation reports found"
    lines = [
        "| Timestamp | Items | Markdown | JSON |",
        "|-----------|-------|----------|------|",
    ]
    for report in reports:
        lines.append(
            f"| {report.timestamp} | {len(report.items)} | {report.markdown_path} | {report.json_path} |"
        )
    return "\n".join(lines)


def _collect_items(root: Path, today: dt.date) -> list[ConsolidationItem]:
    items: list[ConsolidationItem] = []
    status = index_status(root, include_runtime=True)
    if not status.is_current:
        items.append(_index_item(status))

    chunks = load_chunks(root, include_runtime=True)
    items.extend(_duplicate_items(chunks))
    items.extend(_stale_items(chunks, today))
    items.extend(_promote_items(chunks))
    items.extend(_conflict_items(chunks))
    return _renumber_items(items)


def _index_item(status: IndexStatus) -> ConsolidationItem:
    state = "missing" if status.missing_db else "stale"
    stale_preview = "\n".join(status.stale_files[:20])
    if len(status.stale_files) > 20:
        stale_preview += f"\n... {len(status.stale_files) - 20} more"
    return ConsolidationItem(
        id="I000",
        kind="index",
        risk="low",
        title=f"Memory index is {state}",
        evidence=(f"db: {status.db_path}", f"source files: {status.source_files}", f"indexed chunks: {status.indexed_chunks}"),
        suggested_action="Run index-memory to rebuild the local cache index.",
        safe_apply=True,
        affected_files=(str(status.db_path),),
        preview=stale_preview or state,
    )


def _duplicate_items(chunks: list[MemoryChunk]) -> list[ConsolidationItem]:
    groups: dict[str, list[MemoryChunk]] = {}
    for chunk in chunks:
        if chunk.kind not in {"long_memory", "day_memory", "legacy_memory", "project_manual"}:
            continue
        normalized = _normalize_chunk_text(chunk.text)
        if len(normalized) < 30:
            continue
        digest = hashlib.sha1(normalized.encode("utf-8")).hexdigest()
        groups.setdefault(digest, []).append(chunk)

    items: list[ConsolidationItem] = []
    for duplicates in groups.values():
        paths = tuple(f"{chunk.path}#{chunk.title}" for chunk in duplicates)
        if len(paths) < 2:
            continue
        items.append(
            ConsolidationItem(
                id="D000",
                kind="duplicate",
                risk="medium",
                title="Duplicate memory candidate",
                evidence=paths,
                suggested_action="Review the duplicate sections and merge manually if they carry the same durable fact.",
                safe_apply=False,
                affected_files=tuple(sorted({chunk.path for chunk in duplicates})),
                preview="\n".join(paths),
            )
        )
    return items


def _stale_items(chunks: list[MemoryChunk], today: dt.date) -> list[ConsolidationItem]:
    items: list[ConsolidationItem] = []
    for chunk in chunks:
        if chunk.kind != "long_memory":
            continue
        match = re.search(r"^\s*-\s*最近核验\s*[:：]\s*(\d{4}-\d{2}-\d{2})\s*$", chunk.text, flags=re.MULTILINE)
        if not match:
            continue
        verified = dt.date.fromisoformat(match.group(1))
        age_days = (today - verified).days
        if age_days <= STALE_DAYS:
            continue
        items.append(
            ConsolidationItem(
                id="S000",
                kind="stale",
                risk="medium",
                title=f"Memory entry not verified for {age_days} days",
                evidence=(f"{chunk.path}#{chunk.title}", f"最近核验: {verified.isoformat()}"),
                suggested_action="Re-open the cited source files or scripts before relying on this entry; refresh 最近核验 only after verification.",
                safe_apply=False,
                affected_files=(chunk.path,),
                preview=chunk.text[:600].rstrip(),
            )
        )
    return items


def _promote_items(chunks: list[MemoryChunk]) -> list[ConsolidationItem]:
    items: list[ConsolidationItem] = []
    for chunk in chunks:
        if chunk.kind != "day_memory":
            continue
        if not _metadata_marker(chunk.text, "是否可晋升", "yes|是"):
            continue
        items.append(
            ConsolidationItem(
                id="P000",
                kind="promote",
                risk="medium",
                title="Day memory marked as promotable",
                evidence=(f"{chunk.path}#{chunk.title}",),
                suggested_action="Review evidence and promote to MEMORY.md only if the fact is still verified and durable.",
                safe_apply=False,
                affected_files=(chunk.path, ".codex/memory/MEMORY.md"),
                preview=chunk.text[:600].rstrip(),
            )
        )
    return items


def _conflict_items(chunks: list[MemoryChunk]) -> list[ConsolidationItem]:
    items: list[ConsolidationItem] = []
    for chunk in chunks:
        if not _metadata_marker(chunk.text, "类别", "conflict|冲突"):
            continue
        items.append(
            ConsolidationItem(
                id="C000",
                kind="conflict",
                risk="high",
                title="Conflict event requires human review",
                evidence=(f"{chunk.path}#{chunk.title}",),
                suggested_action="Resolve with source-of-truth priority; do not automatically rewrite durable memory.",
                safe_apply=False,
                affected_files=(chunk.path,),
                preview=chunk.text[:600].rstrip(),
            )
        )
    return items


def _renumber_items(items: list[ConsolidationItem]) -> list[ConsolidationItem]:
    prefixes = {
        "index": "I",
        "duplicate": "D",
        "stale": "S",
        "promote": "P",
        "conflict": "C",
    }
    counters: dict[str, int] = {}
    numbered: list[ConsolidationItem] = []
    for item in items:
        counters[item.kind] = counters.get(item.kind, 0) + 1
        prefix = prefixes.get(item.kind, "X")
        numbered.append(
            ConsolidationItem(
                id=f"{prefix}{counters[item.kind]:03d}",
                kind=item.kind,
                risk=item.risk,
                title=item.title,
                evidence=item.evidence,
                suggested_action=item.suggested_action,
                safe_apply=item.safe_apply,
                affected_files=item.affected_files,
                preview=item.preview,
            )
        )
    return numbered


def _load_report_json(root: Path, json_path: Path) -> ConsolidationReport | None:
    path = json_path if json_path.is_absolute() else root / json_path
    if not path.is_file():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    items = tuple(
        ConsolidationItem(
            id=str(item["id"]),
            kind=str(item["kind"]),
            risk=str(item["risk"]),
            title=str(item["title"]),
            evidence=tuple(str(value) for value in item.get("evidence", [])),
            suggested_action=str(item["suggested_action"]),
            safe_apply=bool(item["safe_apply"]),
            affected_files=tuple(str(value) for value in item.get("affected_files", [])),
            preview=str(item.get("preview", "")),
        )
        for item in data.get("items", [])
    )
    return ConsolidationReport(
        root=Path(data.get("root", root)).resolve(),
        timestamp=str(data.get("timestamp", path.name.removesuffix("-consolidation.json"))),
        markdown_path=Path(data.get("markdown_path", path.with_suffix(".md"))),
        json_path=path,
        items=items,
    )


def _normalize_chunk_text(text: str) -> str:
    lines = text.strip().splitlines()
    if lines and lines[0].lstrip().startswith("#"):
        lines = lines[1:]
    normalized = "\n".join(line.strip() for line in lines if line.strip())
    normalized = re.sub(r"\s+", " ", normalized).strip().lower()
    return normalized


def _auto_duplicate_archive_paths(root: Path) -> list[str]:
    chunks = load_chunks(root, include_runtime=True)
    chunks_by_path: dict[str, list[MemoryChunk]] = {}
    paths_by_digest: dict[str, set[str]] = {}

    for chunk in chunks:
        if chunk.kind not in {"long_memory", "day_memory", "legacy_memory", "project_manual"}:
            continue
        normalized = _normalize_chunk_text(chunk.text)
        if len(normalized) < 30:
            continue
        digest = hashlib.sha1(normalized.encode("utf-8")).hexdigest()
        chunks_by_path.setdefault(chunk.path, []).append(chunk)
        paths_by_digest.setdefault(digest, set()).add(chunk.path)

    candidates: list[str] = []
    for rel_path, path_chunks in chunks_by_path.items():
        if not _is_auto_retirable_legacy_path(rel_path):
            continue

        digests: list[str] = []
        for chunk in path_chunks:
            normalized = _normalize_chunk_text(chunk.text)
            if len(normalized) < 30:
                continue
            digests.append(hashlib.sha1(normalized.encode("utf-8")).hexdigest())
        if not digests:
            continue

        if all(_has_active_duplicate(rel_path, paths_by_digest[digest]) for digest in digests):
            candidates.append(rel_path)

    return sorted(set(candidates))


def _auto_duplicate_section_cleanups(root: Path, timestamp: str, *, excluded_paths: set[str]) -> list[SectionCleanup]:
    chunks = load_chunks(root, include_runtime=True)
    paths_by_digest: dict[str, set[str]] = {}
    for chunk in chunks:
        if chunk.kind not in {"long_memory", "day_memory", "legacy_memory", "project_manual"}:
            continue
        normalized = _normalize_chunk_text(chunk.text)
        if len(normalized) < 30:
            continue
        digest = hashlib.sha1(normalized.encode("utf-8")).hexdigest()
        paths_by_digest.setdefault(digest, set()).add(chunk.path)

    cleanups: list[SectionCleanup] = []
    candidate_paths = sorted(
        {
            chunk.path
            for chunk in chunks
            if _is_auto_retirable_legacy_path(chunk.path) and chunk.path not in excluded_paths
        }
    )
    for rel_path in candidate_paths:
        source = root / rel_path
        if not source.is_file():
            continue
        text = source.read_text(encoding="utf-8", errors="replace")
        sections = _split_markdown_sections(text)
        kept_sections: list[str] = []
        removed_titles: list[str] = []

        for title, section_text in sections:
            normalized = _normalize_chunk_text(section_text)
            if len(normalized) < 30:
                kept_sections.append(section_text)
                continue
            digest = hashlib.sha1(normalized.encode("utf-8")).hexdigest()
            if _has_active_duplicate(rel_path, paths_by_digest.get(digest, set())):
                removed_titles.append(title)
                continue
            kept_sections.append(section_text)

        if not removed_titles:
            continue

        archive_path = _copy_active_memory_file_to_archive(root, rel_path, timestamp)
        new_text = "\n\n".join(section.strip() for section in kept_sections if section.strip()).strip()
        deleted_file = _is_effectively_empty_markdown(new_text)
        if deleted_file:
            source.unlink()
        else:
            source.write_text(new_text + "\n", encoding="utf-8")
        cleanups.append(
            SectionCleanup(
                rel_path=rel_path,
                removed_titles=tuple(removed_titles),
                archive_path=archive_path,
                deleted_file=deleted_file,
            )
        )

    return cleanups


def _split_markdown_sections(text: str) -> list[tuple[str, str]]:
    sections: list[tuple[str, list[str]]] = []
    current_title = "Document"
    current_lines: list[str] = []

    for line in text.splitlines():
        heading = re.match(r"^(#{1,6})\s+(.+?)\s*$", line)
        if heading:
            if current_lines:
                sections.append((current_title, current_lines))
            current_title = heading.group(2).strip()
            current_lines = [line]
        else:
            current_lines.append(line)

    if current_lines:
        sections.append((current_title, current_lines))

    return [(title, "\n".join(lines).strip()) for title, lines in sections if "\n".join(lines).strip()]


def _is_effectively_empty_markdown(text: str) -> bool:
    body_lines = [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not re.match(r"^#{1,6}\s+.+$", line.strip()) and line.strip() != "---"
    ]
    return not body_lines


def _is_auto_retirable_legacy_path(rel_path: str) -> bool:
    if rel_path.startswith(".worktrees/") and "/.workbuddy/" in rel_path:
        return True
    if rel_path.startswith(".worktrees/") and "/memory/" in f"/{rel_path}":
        return True
    return rel_path.startswith((".workbuddy/", ".claude/", ".cursor/"))


def _has_active_duplicate(rel_path: str, duplicate_paths: set[str]) -> bool:
    others = duplicate_paths - {rel_path}
    if not others:
        return False
    if rel_path.startswith(".worktrees/"):
        return True
    return any(_is_canonical_memory_path(path) for path in others)


def _is_canonical_memory_path(rel_path: str) -> bool:
    return (
        rel_path == "CODEX.md"
        or rel_path == ".codex/memory/MEMORY.md"
        or bool(re.fullmatch(r"\.codex/memory/\d{4}-\d{2}-\d{2}\.md", rel_path))
    )


def _expired_day_memory_archive_paths(root: Path, today: dt.date, older_than_days: int) -> list[str]:
    if older_than_days <= 0:
        return []
    memory_dir = root / ".codex" / "memory"
    if not memory_dir.is_dir():
        return []

    expired: list[str] = []
    for path in sorted(memory_dir.glob("*.md")):
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}\.md", path.name):
            continue
        event_date = dt.date.fromisoformat(path.stem)
        if (today - event_date).days <= older_than_days:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if _has_promotable_or_conflict_event(text):
            continue
        expired.append(path.relative_to(root).as_posix())
    return expired


def _has_promotable_or_conflict_event(text: str) -> bool:
    return bool(
        _metadata_marker(text, "是否可晋升", "yes|是")
        or _metadata_marker(text, "类别", "conflict|冲突")
    )


def _metadata_marker(text: str, field: str, values: str) -> bool:
    return bool(re.search(rf"^\s*-\s*{re.escape(field)}\s*[:：]\s*(?:{values})(?:\s*$|\s*[（(，,；;])", text, flags=re.MULTILINE | re.IGNORECASE))


def _archive_active_memory_file(root: Path, rel_path: str, timestamp: str) -> Path:
    source = (root / rel_path).resolve()
    resolved_root = root.resolve()
    try:
        source.relative_to(resolved_root)
    except ValueError as exc:
        raise ValueError(f"refusing to archive outside project root: {rel_path}") from exc
    if not source.is_file():
        raise FileNotFoundError(f"memory file not found: {rel_path}")

    target = archive_dir(root) / timestamp / rel_path
    target.parent.mkdir(parents=True, exist_ok=True)
    unique_target = _unique_archive_path(target)
    shutil.move(str(source), str(unique_target))
    return unique_target


def _copy_active_memory_file_to_archive(root: Path, rel_path: str, timestamp: str) -> Path:
    source = (root / rel_path).resolve()
    resolved_root = root.resolve()
    try:
        source.relative_to(resolved_root)
    except ValueError as exc:
        raise ValueError(f"refusing to archive outside project root: {rel_path}") from exc
    if not source.is_file():
        raise FileNotFoundError(f"memory file not found: {rel_path}")

    target = archive_dir(root) / timestamp / rel_path
    target.parent.mkdir(parents=True, exist_ok=True)
    unique_target = _unique_archive_path(target)
    shutil.copy2(source, unique_target)
    return unique_target


def _unique_archive_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix
    parent = path.parent
    counter = 2
    while True:
        candidate = parent / f"{stem}-{counter}{suffix}"
        if not candidate.exists():
            return candidate
        counter += 1


def _normalize_ids(ids: Iterable[str]) -> list[str]:
    normalized: list[str] = []
    for value in ids:
        for part in str(value).split(","):
            part = part.strip()
            if part:
                normalized.append(part)
    return list(dict.fromkeys(normalized))


def _join_inline(values: tuple[str, ...]) -> str:
    if not values:
        return "none"
    return "; ".join(f"`{value}`" for value in values)
