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

MANAGED_CONTEXT_FILES = {"CODEX.md", "AGENTS.md", "CLAUDE.md", "CURSOR.md"}


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


def is_excluded(path: Path, root: Path) -> bool:
    try:
        rel_parts = path.relative_to(root).parts
    except ValueError:
        return True
    return any(part in EXCLUDED_DIRS for part in rel_parts)


def classify_memory_file(path: Path, root: Path) -> str | None:
    try:
        rel = path.relative_to(root)
    except ValueError:
        return None

    rel_posix = rel.as_posix()
    name = path.name

    if name == "CODEX.md":
        return "project_manual"
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


def discover_memory_files(root: Path, *, include_legacy: bool = True) -> list[Path]:
    files: list[Path] = []

    for name in MANAGED_CONTEXT_FILES:
        candidate = root / name
        if candidate.is_file():
            files.append(candidate)

    codex_memory = root / ".codex" / "memory"
    if codex_memory.is_dir():
        files.extend(path for path in codex_memory.glob("*.md") if path.is_file())

    if include_legacy:
        for path in root.rglob("*.md"):
            if is_excluded(path, root):
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


def load_chunks(root: Path, *, include_legacy: bool = True) -> list[MemoryChunk]:
    chunks: list[MemoryChunk] = []
    for path in discover_memory_files(root, include_legacy=include_legacy):
        kind = classify_memory_file(path, root)
        if kind is None:
            continue
        rel = path.relative_to(root).as_posix()
        text = path.read_text(encoding="utf-8", errors="replace")
        mtime = path.stat().st_mtime
        content_hash = hashlib.sha1(text.encode("utf-8", errors="replace")).hexdigest()
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


def rebuild_index(path: Path, *, db_path: Path | None = None, include_legacy: bool = True) -> IndexResult:
    root = find_project_root(path)
    target_db = db_path or default_db_path(root)
    chunks = load_chunks(root, include_legacy=include_legacy)
    files = {chunk.path for chunk in chunks}

    with connect(target_db) as connection:
        connection.executescript(
            """
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


def ensure_index(root: Path, db_path: Path) -> None:
    if db_path.exists():
        return
    rebuild_index(root, db_path=db_path)


def _row_to_search_result(row: sqlite3.Row, *, score: int = 0) -> SearchResult:
    return SearchResult(
        id=str(row["id"]),
        path=str(row["path"]),
        kind=str(row["kind"]),
        title=str(row["title"]),
        tokens=int(row["tokens"]),
        score=score,
        text=str(row["text"]),
    )


def search_memory(
    path: Path,
    query: str,
    *,
    db_path: Path | None = None,
    limit: int = 10,
) -> list[SearchResult]:
    root = find_project_root(path)
    target_db = db_path or default_db_path(root)
    ensure_index(root, target_db)
    terms = _normalize_query(query)

    with connect(target_db) as connection:
        rows = connection.execute(
            "SELECT id, path, kind, title, text, tokens FROM memory_chunks"
        ).fetchall()

    scored: list[SearchResult] = []
    for row in rows:
        score = _score_chunk(row, terms)
        if score <= 0:
            continue
        scored.append(_row_to_search_result(row, score=score))

    scored.sort(key=lambda item: (-item.score, item.tokens, item.path, item.title))
    return scored[:limit]


def get_memory_by_id(path: Path, chunk_id: str, *, db_path: Path | None = None) -> SearchResult | None:
    root = find_project_root(path)
    target_db = db_path or default_db_path(root)
    ensure_index(root, target_db)

    with connect(target_db) as connection:
        row = connection.execute(
            "SELECT id, path, kind, title, text, tokens FROM memory_chunks WHERE id = ?",
            (chunk_id,),
        ).fetchone()

    if row is None:
        return None
    return _row_to_search_result(row)


def _source_fingerprint(path: Path) -> tuple[float, str]:
    text = path.read_text(encoding="utf-8", errors="replace")
    return path.stat().st_mtime, hashlib.sha1(text.encode("utf-8", errors="replace")).hexdigest()


def index_status(path: Path, *, db_path: Path | None = None, include_legacy: bool = True) -> IndexStatus:
    root = find_project_root(path)
    target_db = db_path or default_db_path(root)
    source_files = discover_memory_files(root, include_legacy=include_legacy)

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
            rows = connection.execute(
                """
                SELECT path, MAX(mtime) AS mtime, MAX(content_hash) AS content_hash, COUNT(*) AS chunks
                FROM memory_chunks
                GROUP BY path
                """
            ).fetchall()
            chunk_count = int(connection.execute("SELECT COUNT(*) FROM memory_chunks").fetchone()[0])
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


def context_from_ids(
    path: Path,
    chunk_ids: Iterable[str],
    *,
    db_path: Path | None = None,
    max_tokens: int = 1000,
) -> str:
    results: list[SearchResult] = []
    for chunk_id in chunk_ids:
        result = get_memory_by_id(path, chunk_id, db_path=db_path)
        if result is not None:
            results.append(result)
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
            allowed_chars = max(320, budget * TOKEN_CHARS - len(header) - 32)
            block = f"{header}\n{snippet[:allowed_chars].rstrip()}..."
            block_tokens = estimate_tokens(block)
        lines.append(block)
        lines.append("")
        used += block_tokens

    lines.append(f"_Approx read tokens: {used} / {budget}_")
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
