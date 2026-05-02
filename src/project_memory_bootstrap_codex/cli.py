"""Command line interface for project-memory-bootstrap-codex."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .bootstrap import doctor, find_project_root, init_project, scan_shell_scripts
from .memory_consolidation import (
    apply_consolidation_report,
    format_report_list,
    generate_consolidation_report,
    latest_report,
    list_reports,
    render_report_json,
)
from .memory_index import (
    compact_text,
    context_from_ids,
    context_from_results,
    format_search_table,
    get_memory_by_id,
    index_status,
    rebuild_index,
    search_memory,
    search_results_to_json,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="project-memory-bootstrap-codex",
        description="Initialize and validate project-level memory governance files for Codex.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    init = subparsers.add_parser("init", help="Initialize project memory files.")
    init.add_argument("path", nargs="?", default=".", help="Project path. Defaults to current directory.")
    init.add_argument("--today", help="Override date for the day memory file, format YYYY-MM-DD.")
    init.add_argument("--force", action="store_true", help="Overwrite existing managed files.")
    init.add_argument("--with-agents", action="store_true", help="Also create AGENTS.md when missing.")

    check = subparsers.add_parser("doctor", help="Validate project memory files.")
    check.add_argument("path", nargs="?", default=".", help="Project path. Defaults to current directory.")

    scan = subparsers.add_parser("scan-scripts", help="List key shell scripts considered by the bootstrap.")
    scan.add_argument("path", nargs="?", default=".", help="Project path. Defaults to current directory.")

    index = subparsers.add_parser("index-memory", help="Build a local SQLite index for project memory files.")
    index.add_argument("path", nargs="?", default=".", help="Project path. Defaults to current directory.")
    index.add_argument("--db", help="Override index database path. Defaults to .codex/cache/memory-index.sqlite.")
    index.add_argument(
        "--no-legacy",
        action="store_true",
        help="Only index CODEX.md and .codex/memory/*.md; skip legacy MEMORY.md/memory/*.md files.",
    )

    search = subparsers.add_parser("search-memory", help="Search indexed project memory without loading full files.")
    search.add_argument("path", help="Project path.")
    search.add_argument("query", help="Search query.")
    search.add_argument("--db", help="Override index database path.")
    search.add_argument("--limit", type=int, default=10, help="Maximum results. Defaults to 10.")
    search.add_argument("--json", action="store_true", help="Emit JSON instead of a Markdown table.")

    get = subparsers.add_parser("get-memory", help="Fetch one indexed memory chunk by ID.")
    get.add_argument("path", help="Project path.")
    get.add_argument("id", help="Memory chunk ID from search-memory.")
    get.add_argument("--db", help="Override index database path.")
    get.add_argument("--json", action="store_true", help="Emit JSON including the chunk text.")

    status = subparsers.add_parser("memory-status", help="Check whether the local memory index is current.")
    status.add_argument("path", nargs="?", default=".", help="Project path. Defaults to current directory.")
    status.add_argument("--db", help="Override index database path.")
    status.add_argument(
        "--no-legacy",
        action="store_true",
        help="Only compare CODEX.md and .codex/memory/*.md; skip legacy MEMORY.md/memory/*.md files.",
    )

    context = subparsers.add_parser("context-memory", help="Render compact memory context for a query.")
    context.add_argument("path", help="Project path.")
    context.add_argument("query", nargs="?", help="Search query. Optional when --id is used.")
    context.add_argument("--id", dest="ids", action="append", help="Render context from a specific chunk ID. Repeatable.")
    context.add_argument("--db", help="Override index database path.")
    context.add_argument("--limit", type=int, default=5, help="Maximum indexed chunks to consider. Defaults to 5.")
    context.add_argument("--max-tokens", type=int, default=1000, help="Approximate output token budget. Defaults to 1000.")

    consolidate = subparsers.add_parser("consolidate-memory", help="Create or apply a memory consolidation report.")
    consolidate.add_argument("path", nargs="?", default=".", help="Project path. Defaults to current directory.")
    consolidate_mode = consolidate.add_mutually_exclusive_group(required=True)
    consolidate_mode.add_argument("--dry-run", action="store_true", help="Generate a report without editing memory files.")
    consolidate_mode.add_argument("--apply", action="store_true", help="Apply selected safe items from a report.")
    consolidate.add_argument("--from-report", default="latest", help="Report JSON path or 'latest' when applying.")
    consolidate.add_argument("--only", nargs="*", default=(), help="Report item IDs to apply. Accepts repeated or comma-separated IDs.")
    consolidate.add_argument("--safe", action="store_true", help="Only apply items marked safe_apply.")
    consolidate.add_argument("--json", action="store_true", help="Emit report/result as JSON.")

    reports = subparsers.add_parser("consolidation-reports", help="List memory consolidation reports.")
    reports.add_argument("path", nargs="?", default=".", help="Project path. Defaults to current directory.")

    report = subparsers.add_parser("consolidation-report", help="Show one memory consolidation report.")
    report.add_argument("path", nargs="?", default=".", help="Project path. Defaults to current directory.")
    report.add_argument("--latest", action="store_true", help="Show the latest report.")
    report.add_argument("--json", action="store_true", help="Emit JSON report instead of Markdown.")
    report.add_argument("--file", help="Specific report JSON or Markdown path.")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.command == "init":
        results = init_project(
            Path(args.path),
            today=args.today,
            force=args.force,
            with_agents=args.with_agents,
        )
        for result in results:
            print(f"{result.status}: {result.path}")
        return 0

    if args.command == "doctor":
        result = doctor(Path(args.path))
        for message in result.messages:
            print(message)
        return 0 if result.ok else 1

    if args.command == "scan-scripts":
        root = find_project_root(Path(args.path))
        scripts = scan_shell_scripts(root)
        if not scripts:
            print("no shell scripts found")
            return 0
        for script in scripts:
            print(script)
        return 0

    if args.command == "index-memory":
        result = rebuild_index(
            Path(args.path),
            db_path=Path(args.db).expanduser().resolve() if args.db else None,
            include_legacy=not args.no_legacy,
        )
        print(f"indexed: {result.chunks} chunks from {result.files} files")
        print(f"root: {result.root}")
        print(f"db: {result.db_path}")
        return 0

    if args.command == "search-memory":
        results = search_memory(
            Path(args.path),
            args.query,
            db_path=Path(args.db).expanduser().resolve() if args.db else None,
            limit=max(1, args.limit),
        )
        print(search_results_to_json(results) if args.json else format_search_table(results))
        return 0

    if args.command == "get-memory":
        result = get_memory_by_id(
            Path(args.path),
            args.id,
            db_path=Path(args.db).expanduser().resolve() if args.db else None,
        )
        if result is None:
            print(f"memory id not found: {args.id}", file=sys.stderr)
            return 1
        if args.json:
            print(
                json.dumps(
                    {
                        "id": result.id,
                        "path": result.path,
                        "kind": result.kind,
                        "title": result.title,
                        "tokens": result.tokens,
                        "text": result.text,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
        else:
            print(f"# {result.id} | {result.kind} | {result.path} | {result.title}")
            print()
            print(compact_text(result.text))
        return 0

    if args.command == "memory-status":
        result = index_status(
            Path(args.path),
            db_path=Path(args.db).expanduser().resolve() if args.db else None,
            include_legacy=not args.no_legacy,
        )
        state = "missing" if result.missing_db else "stale" if result.stale_files else "ok"
        print(f"status: {state}")
        print(f"root: {result.root}")
        print(f"db: {result.db_path}")
        print(f"source files: {result.source_files}")
        print(f"indexed chunks: {result.indexed_chunks}")
        if result.stale_files:
            print("stale files:")
            for stale_file in result.stale_files:
                print(f"- {stale_file}")
        return 0 if result.is_current else 1

    if args.command == "context-memory":
        db_path = Path(args.db).expanduser().resolve() if args.db else None
        if args.ids:
            print(context_from_ids(Path(args.path), args.ids, db_path=db_path, max_tokens=args.max_tokens))
            return 0
        if not args.query:
            print("context-memory requires a query unless --id is provided", file=sys.stderr)
            return 2
        results = search_memory(Path(args.path), args.query, db_path=db_path, limit=max(1, args.limit))
        print(context_from_results(results, max_tokens=args.max_tokens))
        return 0

    if args.command == "consolidate-memory":
        if args.dry_run:
            report = generate_consolidation_report(Path(args.path), write=True)
            if args.json:
                print(render_report_json(report))
            else:
                print(f"markdown: {report.markdown_path}")
                print(f"json: {report.json_path}")
                print(f"items: {len(report.items)}")
                for item in report.items:
                    safe = "safe" if item.safe_apply else "review"
                    print(f"- {item.id} [{item.kind}/{item.risk}/{safe}] {item.title}")
            return 0
        result = apply_consolidation_report(
            Path(args.path),
            from_report=args.from_report,
            only=args.only,
            safe=args.safe,
        )
        if args.json:
            print(
                json.dumps(
                    {
                        "applied": list(result.applied),
                        "skipped": list(result.skipped),
                        "messages": list(result.messages),
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
        else:
            for message in result.messages:
                print(message)
        return 0 if not result.skipped else 1

    if args.command == "consolidation-reports":
        print(format_report_list(list_reports(Path(args.path))))
        return 0

    if args.command == "consolidation-report":
        if args.file:
            report_path = Path(args.file).expanduser()
            if report_path.suffix == ".json":
                print(report_path.read_text(encoding="utf-8") if args.json else report_path.with_suffix(".md").read_text(encoding="utf-8"))
                return 0
            print(report_path.read_text(encoding="utf-8"))
            return 0
        if not args.latest:
            print("consolidation-report requires --latest or --file", file=sys.stderr)
            return 2
        report = latest_report(Path(args.path))
        print(render_report_json(report) if args.json else report.markdown_path.read_text(encoding="utf-8"))
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
