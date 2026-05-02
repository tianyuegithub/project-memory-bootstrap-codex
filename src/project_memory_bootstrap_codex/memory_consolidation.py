"""Dry-run consolidation reports for project memory governance."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
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
STALE_DAYS = 30


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


def reports_dir(root: Path) -> Path:
    return root / REPORTS_RELATIVE_DIR


def generate_consolidation_report(
    path: Path,
    *,
    now: dt.datetime | None = None,
    write: bool = True,
) -> ConsolidationReport:
    root = find_project_root(path)
    current_time = now or dt.datetime.now().replace(microsecond=0)
    timestamp = current_time.strftime("%Y%m%d-%H%M%S")
    items = tuple(_collect_items(root, current_time.date()))
    target_dir = reports_dir(root)
    markdown_path = target_dir / f"{timestamp}-consolidation.md"
    json_path = target_dir / f"{timestamp}-consolidation.json"
    report = ConsolidationReport(
        root=root,
        timestamp=timestamp,
        markdown_path=markdown_path,
        json_path=json_path,
        items=items,
    )

    if write:
        target_dir.mkdir(parents=True, exist_ok=True)
        markdown_path.write_text(render_report_markdown(report), encoding="utf-8")
        json_path.write_text(render_report_json(report), encoding="utf-8")

    return report


def list_reports(path: Path) -> list[ConsolidationReport]:
    root = find_project_root(path)
    reports: list[ConsolidationReport] = []
    for json_path in sorted(reports_dir(root).glob("*-consolidation.json")):
        report = _load_report_json(root, json_path)
        if report is not None:
            reports.append(report)
    return reports


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
    status = index_status(root)
    if not status.is_current:
        items.append(_index_item(status))

    chunks = load_chunks(root)
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
        match = re.search(r"^\s*-\s*最近核验:\s*(\d{4}-\d{2}-\d{2})\s*$", chunk.text, flags=re.MULTILINE)
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
        if not re.search(r"^\s*-\s*是否可晋升:\s*yes\s*$", chunk.text, flags=re.MULTILINE | re.IGNORECASE):
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
        if not re.search(r"^\s*-\s*类别:\s*conflict\s*$", chunk.text, flags=re.MULTILINE | re.IGNORECASE):
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
