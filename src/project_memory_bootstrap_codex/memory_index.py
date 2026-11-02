"""Lightweight runtime index for project memory files.

The governance files remain the source of truth. This module builds a small
SQLite index so runtime usage can search and fetch compact snippets instead of
loading every memory markdown file into the model context.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .bootstrap import find_project_root
from .errors import MemoryProtocolError
from .facts import START, parse_facts
from .memory_state import published_document

DEFAULT_DB_RELATIVE_PATH = Path(".codex") / "cache" / "memory-index.sqlite"
DEFAULT_MAX_CHARS = 2400
TOKEN_CHARS = 4

EXCLUDED_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "node_modules",
    "dist",
    "build",
    "__pycache__",
}
RUNTIME_CONTEXT_DIRS = {".runtime", ".worktrees"}

ACTIVE_CONTEXT_FILES = {"README.md", "AGENTS.md"}
LEGACY_CONTEXT_FILES = {"CODEX.md", "CLAUDE.md", "CURSOR.md"}
LEGACY_KINDS = {"agent_legacy", "legacy_memory", "legacy_manual"}


@dataclass(frozen=True)
class MemoryChunk:
    id: str
    path: str
    kind: str
    title: str
    text: str
    tokens: int
    mtime: float
    content_hash: str


@dataclass(frozen=True)
class IndexResult:
    root: Path
    db_path: Path
    chunks: int
    files: int


@dataclass(frozen=True)
class SearchResult:
    id: str
    path: str
    kind: str
    title: str
    tokens: int
    score: int
    text: str


@dataclass(frozen=True)
class IndexStatus:
    root: Path
    db_path: Path
    indexed_chunks: int
    source_files: int
    stale_files: tuple[str, ...]
    missing_db: bool = False

    @property
    def is_current(self) -> bool:
        return not self.missing_db and not self.stale_files


def default_db_path(root: Path) -> Path:
    return root / DEFAULT_DB_RELATIVE_PATH


def estimate_tokens(text: str) -> int:
    return max(1, (len(text) + TOKEN_CHARS - 1) // TOKEN_CHARS)


def is_excluded(path: Path, root: Path, *, include_runtime: bool = False) -> bool:
    try:
        rel_parts = path.relative_to(root).parts
    except ValueError:
        return True
    if len(rel_parts) >= 2 and rel_parts[0] == ".codex" and rel_parts[1] == "cache":
        return True
    if not include_runtime and any(part in RUNTIME_CONTEXT_DIRS for part in rel_parts):
        return True
    return any(part in EXCLUDED_DIRS for part in rel_parts)


def classify_memory_file(path: Path, root: Path) -> str | None:
    try:
        rel = path.relative_to(root)
    except ValueError:
        return None

    rel_posix = rel.as_posix()
    name = path.name

    if name == "README.md" and path.parent == root:
        return "project_manual"
    if name == "CODEX.md":
        return "legacy_manual"
    if name == "AGENTS.md":
        return "agent_instructions"
    if name in {"CLAUDE.md", "CURSOR.md"}:
        return "agent_legacy"
    if rel_posix == ".codex/memory/MEMORY.md":
        return "long_memory"
    if rel_posix.startswith(".codex/memory/") and re.fullmatch(r"\d{4}-\d{2}-\d{2}\.md", name):
        return "day_memory"
    if name == "MEMORY.md":
        return "legacy_memory"
    if "/memory/" in f"/{rel_posix}" and name.endswith(".md"):
        return "legacy_memory"
    return None


def discover_memory_files(root: Path, *, include_legacy: bool = True, include_runtime: bool = False, governed: bool = False) -> list[Path]:
    if governed:
        source = root / ".codex/memory/MEMORY.md"
        return [source] if source.is_file() else []
    files: list[Path] = []

    managed_context_files = set(ACTIVE_CONTEXT_FILES)
    if include_legacy:
        managed_context_files.update(LEGACY_CONTEXT_FILES)

    for name in managed_context_files:
        candidate = root / name
        if candidate.is_file():
            files.append(candidate)

    codex_memory = root / ".codex" / "memory"
    if codex_memory.is_dir():
        files.extend(path for path in codex_memory.glob("*.md") if path.is_file())

    if include_legacy:
        for path in root.rglob("*.md"):
            if is_excluded(path, root, include_runtime=include_runtime):
                continue
            kind = classify_memory_file(path, root)
            if kind == "legacy_memory":
                files.append(path)

    return sorted({path.resolve() for path in files})


def split_markdown(text: str, *, max_chars: int = DEFAULT_MAX_CHARS) -> list[tuple[str, str]]:
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

    chunks: list[tuple[str, str]] = []
    for title, lines in sections:
        section_text = "\n".join(lines).strip()
        if not section_text:
            continue
        chunks.extend(split_large_section(title, section_text, max_chars=max_chars))
    return chunks


def split_large_section(title: str, text: str, *, max_chars: int) -> list[tuple[str, str]]:
    if len(text) <= max_chars:
        return [(title, text)]

    paragraphs = re.split(r"\n\s*\n", text)
    chunks: list[tuple[str, str]] = []
    current: list[str] = []
    current_len = 0
    part = 1

    for paragraph in paragraphs:
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        if current and current_len + len(paragraph) + 2 > max_chars:
            chunks.append((f"{title} / part {part}", "\n\n".join(current)))
            current = []
            current_len = 0
            part += 1
        current.append(paragraph)
        current_len += len(paragraph) + 2

    if current:
        chunks.append((f"{title} / part {part}" if part > 1 else title, "\n\n".join(current)))
    return chunks


def load_chunks(root: Path, *, include_legacy: bool = True, include_runtime: bool = False, governed: bool = False) -> list[MemoryChunk]:
    chunks: list[MemoryChunk] = []
    for path in discover_memory_files(root, include_legacy=include_legacy, include_runtime=include_runtime, governed=governed):
        kind = classify_memory_file(path, root)
        if kind is None:
            continue
        rel = path.relative_to(root).as_posix()
        text = path.read_text(encoding="utf-8", errors="replace")
        mtime = path.stat().st_mtime
        content_hash = hashlib.sha1(text.encode("utf-8", errors="replace")).hexdigest()
        if governed:
            for fact in parse_facts(text):
                if fact.metadata["status"] == "forgotten":
                    continue
                chunks.append(MemoryChunk(fact.fact_id, rel, "fact:" + fact.metadata["status"], fact.title,
                                          fact.raw, estimate_tokens(fact.raw), mtime, content_hash))
            continue
        if START in text:
            raise MemoryProtocolError("PROJECT_UNREGISTERED", "Managed facts require their registered project and persistent state.")
        for title, chunk_text in split_markdown(text):
            raw_id = f"{rel}\0{title}\0{chunk_text}".encode("utf-8")
            chunk_id = hashlib.sha1(raw_id).hexdigest()[:12]
            chunks.append(
                MemoryChunk(
                    id=chunk_id,
                    path=rel,
                    kind=kind,
                    title=title,
                    text=chunk_text,
                    tokens=estimate_tokens(chunk_text),
                    mtime=mtime,
                    content_hash=content_hash,
                )
            )
    return chunks


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    return connection


def _rebuild_index(
    path: Path,
    *,
    db_path: Path | None = None,
    include_legacy: bool = True,
    include_runtime: bool = False,
    governed: bool = False,
) -> IndexResult:
    root = find_project_root(path)
    target_db = db_path or default_db_path(root)
    chunks = load_chunks(root, include_legacy=include_legacy, include_runtime=include_runtime, governed=governed)
    sources = discover_memory_files(root, include_legacy=include_legacy, include_runtime=include_runtime, governed=governed)
    files = {source.relative_to(root).as_posix() for source in sources}

    with connect(target_db) as connection:
        connection.executescript(
            """
            BEGIN IMMEDIATE;
            DROP TABLE IF EXISTS memory_sources;
            CREATE TABLE memory_sources (path TEXT PRIMARY KEY, kind TEXT NOT NULL, mtime REAL NOT NULL, content_hash TEXT NOT NULL);
            DROP TABLE IF EXISTS memory_index_meta;
            CREATE TABLE memory_index_meta (root TEXT NOT NULL, governed INTEGER NOT NULL);
            DROP TABLE IF EXISTS memory_chunks;
            CREATE TABLE memory_chunks (
                id TEXT PRIMARY KEY,
                path TEXT NOT NULL,
                kind TEXT NOT NULL,
                title TEXT NOT NULL,
                text TEXT NOT NULL,
                tokens INTEGER NOT NULL,
                mtime REAL NOT NULL,
                content_hash TEXT NOT NULL,
                indexed_at REAL NOT NULL
            );
            CREATE INDEX idx_memory_chunks_path ON memory_chunks(path);
            CREATE INDEX idx_memory_chunks_kind ON memory_chunks(kind);
            """
        )
        connection.execute("INSERT INTO memory_index_meta VALUES (?,?)", (str(root), int(governed)))
        connection.executemany("INSERT INTO memory_sources VALUES (?,?,?,?)", [
            (source.relative_to(root).as_posix(), classify_memory_file(source, root) or "unknown", *_source_fingerprint(source))
            for source in sources
        ])
        connection.executemany(
            """
            INSERT INTO memory_chunks (id, path, kind, title, text, tokens, mtime, content_hash, indexed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    chunk.id,
                    chunk.path,
                    chunk.kind,
                    chunk.title,
                    chunk.text,
                    chunk.tokens,
                    chunk.mtime,
                    chunk.content_hash,
                    time.time(),
                )
                for chunk in chunks
            ],
        )

    return IndexResult(root=root, db_path=target_db, chunks=len(chunks), files=len(files))


def _normalize_query(query: str) -> list[str]:
    raw_terms = [term.strip().lower() for term in re.split(r"[\s,，。；;:：/\\|]+", query) if term.strip()]
    terms: list[str] = []
    for term in raw_terms or [query.strip().lower()]:
        if not term:
            continue
        terms.append(term)
        if re.search(r"[\u4e00-\u9fff]", term) and len(term) > 2:
            terms.extend(term[index : index + 2] for index in range(0, len(term) - 1))
    return list(dict.fromkeys(terms))


def _score_chunk(row: sqlite3.Row, terms: Iterable[str]) -> int:
    title = str(row["title"]).lower()
    path = str(row["path"]).lower()
    kind = str(row["kind"]).lower()
    text = str(row["text"]).lower()
    if kind.startswith("fact:"):
        fact = parse_facts(str(row["text"]))[0]
        meta = fact.metadata
        text = "\n".join([
            fact.fact_id, fact.body, *meta["scope"], meta["verification"],
            meta["expires_when"], *meta.get("supersedes", []),
            *(source["locator"] for source in meta["sources"]),
        ]).lower()
        # Fact retrieval matches semantic content and source locations, not raw JSON/hash metadata.
        path = kind = ""
    score = 0
    for term in terms:
        if not term:
            continue
        if term in title:
            score += 12
        if term in path:
            score += 8
        if term in kind:
            score += 6
        score += min(text.count(term), 8) * 2
    if score > 0 and str(row["kind"]) in {"project_manual", "long_memory"}:
        score += 2
    return score


def ensure_index(root: Path, db_path: Path, *, include_legacy: bool = False, governed: bool = False) -> None:
    if _index_status(root, db_path=db_path, include_legacy=include_legacy, governed=governed).is_current:
        return
    _rebuild_index(root, db_path=db_path, include_legacy=include_legacy, governed=governed)


def _row_to_search_result(row: sqlite3.Row, *, score: int = 0) -> SearchResult:
    text = str(row["text"])
    tokens = int(row["tokens"])
    if str(row["kind"]).startswith("fact:"):
        fact = parse_facts(text)[0]
        meta = fact.metadata
        text = "\n".join([
            f"## {fact.fact_id}：{fact.title}",
            f"- status: {meta['status']}",
            f"- revision: {meta['revision']}",
            f"- scope: {', '.join(meta['scope'])}",
            f"- verified_at: {meta['verified_at']}",
            "", fact.body, "",
            f"- verification: {meta['verification']}",
            f"- expires_when: {meta['expires_when']}",
            f"- supersedes: {', '.join(meta.get('supersedes', [])) or '无'}",
            f"- 证据条数: {len(meta['sources'])}；完整来源/版本用inspect-memory或memory_inspect",
        ])
        tokens = estimate_tokens(text)
    return SearchResult(
        id=str(row["id"]),
        path=str(row["path"]),
        kind=str(row["kind"]),
        title=str(row["title"]),
        tokens=tokens,
        score=score,
        text=text,
    )


def _selected_rows(path: Path, db_path: Path | None, include_legacy: bool, history: bool) -> list[sqlite3.Row]:
    with published_document(path) as (context, _, suppressed, _):
        root = context.root if context else find_project_root(path)
        target_db = _target_database(root, db_path, context is not None)
        if context and include_legacy:
            raise MemoryProtocolError("LEGACY_REVIEW_REQUIRED", "Unstructured legacy text is a recovery source, not governed recall.")
        ensure_index(root, target_db, include_legacy=include_legacy, governed=context is not None)
        with connect(target_db) as connection:
            where = "" if include_legacy else _non_legacy_where_clause()
            rows = connection.execute(f"SELECT id,path,kind,title,text,tokens FROM memory_chunks {where}").fetchall()
        if context:
            visible = {"fact:active", "fact:needs_review", "fact:conflict"}
            rows = [r for r in rows if r["id"] not in suppressed and (history or r["kind"] in visible)]
        return rows


def search_memory(
    path: Path, query: str, *, db_path: Path | None = None, limit: int = 10,
    include_legacy: bool = False, history: bool = False,
) -> list[SearchResult]:
    rows = _selected_rows(path, db_path, include_legacy, history)
    terms = _normalize_query(query)
    scored = [_row_to_search_result(row, score=score) for row in rows if (score := _score_chunk(row, terms)) > 0]
    scored.sort(key=lambda item: (-item.score, item.tokens, item.path, item.title))
    return scored[:limit]


def get_memory_by_id(
    path: Path, chunk_id: str, *, db_path: Path | None = None,
    include_legacy: bool = False, history: bool = False,
) -> SearchResult | None:
    rows = _selected_rows(path, db_path, include_legacy, True)
    row = _resolve_row(rows, chunk_id, history)
    return _row_to_search_result(row) if row is not None else None


def _resolve_row(rows: list[sqlite3.Row], chunk_id: str, history: bool) -> sqlite3.Row | None:
    by_id = {row["id"]: row for row in rows}
    row = by_id.get(chunk_id)
    if row is None or history or not row["kind"].startswith("fact:"):
        return row
    if row["kind"] in {"fact:active", "fact:needs_review", "fact:conflict"}:
        return row
    if row["kind"] != "fact:superseded":
        return None
    facts = {r["id"]: parse_facts(r["text"])[0] for r in rows if r["kind"].startswith("fact:")}
    waiting, seen, successors = [chunk_id], set(), set()
    while waiting:
        current = waiting.pop()
        if current in seen:
            continue
        seen.add(current)
        for fact_id, fact in facts.items():
            if current not in fact.metadata.get("supersedes", []):
                continue
            if fact.metadata["status"] in {"active", "needs_review", "conflict"}:
                successors.add(fact_id)
            elif fact.metadata["status"] == "superseded":
                waiting.append(fact_id)
    if len(successors) > 1:
        raise MemoryProtocolError("SUCCESSOR_AMBIGUOUS", "Several current facts replace this fact; query the topic explicitly.")
    return by_id[next(iter(successors))] if successors else None


def _target_database(root: Path, db_path: Path | None, governed: bool) -> Path:
    target = (db_path or default_db_path(root)).resolve()
    cache = (root / ".codex/cache").resolve()
    if governed and (root not in cache.parents or (target != cache and cache not in target.parents)):
        raise MemoryProtocolError("INDEX_OUTSIDE_CACHE", "Governed indexes must remain in the project's cache directory.")
    return target


def rebuild_index(path: Path, *, db_path: Path | None = None, include_legacy: bool = False, include_runtime: bool = False) -> IndexResult:
    with published_document(path) as (context, _, _, _):
        root = context.root if context else find_project_root(path)
        if context and include_legacy:
            raise MemoryProtocolError("LEGACY_REVIEW_REQUIRED", "Governed indexes contain published facts, not unstructured legacy text.")
        target = _target_database(root, db_path, context is not None)
        return _rebuild_index(root, db_path=target, include_legacy=include_legacy, include_runtime=include_runtime, governed=context is not None)


def index_status(path: Path, *, db_path: Path | None = None, include_legacy: bool = False, include_runtime: bool = False) -> IndexStatus:
    with published_document(path) as (context, _, _, _):
        root = context.root if context else find_project_root(path)
        target = _target_database(root, db_path, context is not None)
        return _index_status(root, db_path=target, include_legacy=include_legacy, include_runtime=include_runtime, governed=context is not None)


def _source_fingerprint(path: Path) -> tuple[float, str]:
    text = path.read_text(encoding="utf-8", errors="replace")
    return path.stat().st_mtime, hashlib.sha1(text.encode("utf-8", errors="replace")).hexdigest()


def _index_status(
    path: Path,
    *,
    db_path: Path | None = None,
    include_legacy: bool = True,
    include_runtime: bool = False,
    governed: bool = False,
) -> IndexStatus:
    root = find_project_root(path)
    target_db = db_path or default_db_path(root)
    source_files = discover_memory_files(root, include_legacy=include_legacy, include_runtime=include_runtime, governed=governed)

    if not target_db.exists():
        return IndexStatus(
            root=root,
            db_path=target_db,
            indexed_chunks=0,
            source_files=len(source_files),
            stale_files=tuple(source.relative_to(root).as_posix() for source in source_files),
            missing_db=True,
        )

    try:
        with connect(target_db) as connection:
            where = "" if include_legacy else _non_legacy_where_clause()
            policy = connection.execute("SELECT root,governed FROM memory_index_meta").fetchone()
            if policy is None or policy["root"] != str(root) or bool(policy["governed"]) != governed:
                return IndexStatus(root, target_db, 0, len(source_files), tuple(str(x.relative_to(root)) for x in source_files), True)
            rows = connection.execute(f"SELECT path,mtime,content_hash FROM memory_sources {where}").fetchall()
            chunk_count = int(connection.execute(f"SELECT COUNT(*) FROM memory_chunks {where}").fetchone()[0])
    except sqlite3.Error:
        return IndexStatus(
            root=root,
            db_path=target_db,
            indexed_chunks=0,
            source_files=len(source_files),
            stale_files=tuple(source.relative_to(root).as_posix() for source in source_files),
            missing_db=True,
        )

    indexed = {
        str(row["path"]): (float(row["mtime"]), str(row["content_hash"]))
        for row in rows
    }
    discovered = {source.relative_to(root).as_posix(): source for source in source_files}
    stale: list[str] = []

    for rel, source in discovered.items():
        indexed_state = indexed.get(rel)
        if indexed_state is None:
            stale.append(rel)
            continue
        _, indexed_hash = indexed_state
        _, current_hash = _source_fingerprint(source)
        if indexed_hash != current_hash:
            stale.append(rel)

    for rel in indexed:
        if rel not in discovered:
            stale.append(rel)

    return IndexStatus(
        root=root,
        db_path=target_db,
        indexed_chunks=chunk_count,
        source_files=len(source_files),
        stale_files=tuple(sorted(set(stale))),
    )


def _non_legacy_where_clause() -> str:
    quoted = ", ".join(f"'{kind}'" for kind in sorted(LEGACY_KINDS))
    return f"WHERE kind NOT IN ({quoted})"


def context_from_ids(
    path: Path,
    chunk_ids: Iterable[str],
    *,
    db_path: Path | None = None,
    max_tokens: int = 1000,
    include_legacy: bool = False,
    history: bool = False,
) -> str:
    results: list[SearchResult] = []
    rows = _selected_rows(path, db_path, include_legacy, True)
    seen: set[str] = set()
    for chunk_id in chunk_ids:
        row = _resolve_row(rows, chunk_id, history)
        if row is not None and row["id"] not in seen:
            results.append(_row_to_search_result(row))
            seen.add(row["id"])
    if not results:
        return "No indexed memory matched the requested IDs."
    return context_from_results(results, max_tokens=max_tokens)


def context_from_results(results: list[SearchResult], *, max_tokens: int = 1000) -> str:
    if not results:
        return "No indexed memory matched the query."

    budget = max(120, max_tokens)
    used = 0
    lines = [
        "# Memory Context",
        "",
        "Use these entries as leads. Re-open the source files before treating them as confirmed current facts.",
        "",
    ]

    for result in results:
        header = f"## {result.id} | {result.kind} | {result.path} | {result.title}"
        snippet = compact_text(result.text)
        block = f"{header}\n{snippet}".strip()
        block_tokens = estimate_tokens(block)
        if used and used + block_tokens > budget:
            break
        if block_tokens > budget and not used:
            block = f"{header}\n[完整条目超出本次预算；请使用 get-memory 按该 ID 展开，未截断事实正文。]"
            block_tokens = estimate_tokens(block)
        lines.append(block)
        lines.append("")
        used += block_tokens

    lines.append(f"_Approx read tokens: {used} / {budget} (4 characters/unit; not measured model tokens)_")
    return "\n".join(lines).rstrip()


def compact_text(text: str) -> str:
    lines = [line.rstrip() for line in text.strip().splitlines()]
    compacted: list[str] = []
    blank = False
    for line in lines:
        if not line:
            if not blank:
                compacted.append("")
            blank = True
            continue
        compacted.append(line)
        blank = False
    return "\n".join(compacted).strip()


def search_results_to_json(results: list[SearchResult]) -> str:
    return json.dumps(
        [
            {
                "id": result.id,
                "path": result.path,
                "kind": result.kind,
                "title": result.title,
                "tokens": result.tokens,
                "score": result.score,
            }
            for result in results
        ],
        ensure_ascii=False,
        indent=2,
    )


def format_search_table(results: list[SearchResult]) -> str:
    if not results:
        return "no matching memory entries"
    lines = [
        "| ID | Kind | Tokens | Score | Source | Title |",
        "|----|------|--------|-------|--------|-------|",
    ]
    for result in results:
        lines.append(
            f"| {result.id} | {result.kind} | ~{result.tokens} | {result.score} | {result.path} | {result.title} |"
        )
    return "\n".join(lines)
