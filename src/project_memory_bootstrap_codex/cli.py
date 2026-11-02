"""Command line interface for project-memory-bootstrap-codex."""

from __future__ import annotations

import argparse
import dataclasses
from importlib import resources
import json
import sys
from pathlib import Path

from .bootstrap import doctor, find_project_root, init_project, scan_shell_scripts
from . import __version__, PROTOCOL_VERSION
from .errors import MemoryProtocolError
from .project import register_alias, register_project, resolve_context
from . import memory_state
from .memory_consolidation import (
    apply_consolidation_report,
    agent_apply_consolidation,
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
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__} (protocol {PROTOCOL_VERSION})")
    subparsers = parser.add_subparsers(dest="command", required=True)

    protocol = subparsers.add_parser("protocol", help="Read the same protocol resource shipped with this CLI.")
    protocol.add_argument("--brief", action="store_true", help="Only show the short startup protocol.")

    hook = subparsers.add_parser("hook-memory", help="Handle a non-blocking native host callback from stdin.")
    hook.add_argument("--tool", required=True)
    hook_status = subparsers.add_parser("hook-status", help="Inspect recorded callbacks, without claiming host acceptance.")
    hook_status.add_argument("path", nargs="?", default=".")
    adapter = subparsers.add_parser("install-adapter", help="Install a shared-memory callback in one explicit host configuration.")
    adapter.add_argument("--tool", required=True, choices=("codex", "claude", "opencode", "zcode", "workbuddy"))
    adapter.add_argument("--config", required=True)
    adapter.add_argument("--ownership")
    adapter.add_argument("--project-root", help="Required for workspace-scoped ZCode configuration.")
    native_sessions = subparsers.add_parser("native-sessions", help="List matching native session metadata without reading public text.")
    native_sessions.add_argument("path")
    native_sessions.add_argument("--tool", required=True)
    notes = subparsers.add_parser("native-notes", help="Discover safe native project-memory topic metadata in registered workspaces.")
    notes.add_argument("path")
    notes.add_argument("--tool", action="append", choices=("zcode", "claude", "workbuddy"))
    notes.add_argument("--query", help="Match a literal topic substring in metadata, without exporting note bodies.")
    note = subparsers.add_parser("native-note", help="Read one scoped native note; suppressed or sensitive content is not exported.")
    note.add_argument("path")
    note.add_argument("locator")
    begin_run = subparsers.add_parser("begin-maintenance", help="Record the actual dedicated semantic-maintenance session before work.")
    begin_run.add_argument("path")
    begin_run.add_argument("--tool", required=True)
    begin_run.add_argument("--session", required=True)
    finish_run = subparsers.add_parser("finish-maintenance", help="Record run coverage, committed operations, or a failure without clearing candidates.")
    finish_run.add_argument("path")
    finish_run.add_argument("run_id")
    finish_run.add_argument("--input", default="-")
    runs = subparsers.add_parser("maintenance-runs", help="Inspect semantic-run receipts, including unfinished attempts.")
    runs.add_argument("path")
    scan_sources = subparsers.add_parser("scan-sources", help="Capture owner-file changes and native session activity without semantic publication.")
    scan_sources.add_argument("path")
    scan_sources.add_argument("--tool", action="append", help="Native providers to scan; repeatable. Defaults to the five supported native readers.")
    scan_sources.add_argument("--files-only", action="store_true")
    for name in ("native-messages", "native-message"):
        native = subparsers.add_parser(name, help="Inspect native public-message metadata or one selected public message.")
        native.add_argument("path")
        native.add_argument("--tool", required=True)
        native.add_argument("--session", required=True)
        if name == "native-message":
            native.add_argument("--message", required=True)
        else:
            native.add_argument("--query", help="Match a literal substring in public text before returning metadata.")
            native.add_argument("--role", choices=("user", "assistant"))

    init = subparsers.add_parser("init", help="Initialize project memory files.")
    init.add_argument("path", nargs="?", default=".", help="Project path. Defaults to current directory.")
    init.add_argument("--today", help="Override date for the day memory file, format YYYY-MM-DD.")
    init.add_argument("--force", action="store_true", help="Overwrite existing managed files.")
    init.add_argument("--with-agents", action="store_true", default=True, help="Create AGENTS.md when missing (enabled by default).")

    register = subparsers.add_parser("register-project", help="Register a shared project and its exact repository members.")
    register.add_argument("path")
    register.add_argument("--id", dest="project_id", required=True)
    register.add_argument("--member", action="append", default=[], metavar="ID=RELATIVE_PATH")
    maintenance = register.add_mutually_exclusive_group()
    maintenance.add_argument("--enable-maintenance", dest="maintenance_enabled", action="store_true")
    maintenance.add_argument("--disable-maintenance", dest="maintenance_enabled", action="store_false")
    register.set_defaults(maintenance_enabled=None)
    alias = subparsers.add_parser("register-alias", help="Explicitly map another workspace to a registered repository.")
    alias.add_argument("path", help="Canonical project root.")
    alias.add_argument("workspace")
    alias.add_argument("--repository", required=True)
    for name, description in (
        ("project-context", "Resolve shared project identity."),
        ("pending-memory", "List pending candidates without their private text."),
        ("memory-health", "Inspect persistent publication and intake status."),
        ("recover-memory", "Recover an interrupted publication without repeating semantic work."),
    ):
        command = subparsers.add_parser(name, help=description)
        command.add_argument("path", nargs="?", default=".")
    for name in ("capture-memory", "propose-memory"):
        command = subparsers.add_parser(name, help="Accept a structured source candidate or verified proposal.")
        command.add_argument("path")
        command.add_argument("--input", default="-", help="JSON input file, or - for stdin. Do not put content in argv.")
    for name, argument in (("commit-memory", "proposal_id"), ("inspect-memory", "fact_id"), ("candidate-memory", "candidate_id")):
        command = subparsers.add_parser(name)
        command.add_argument("path")
        command.add_argument(argument)
        if name == "inspect-memory":
            command.add_argument("--metadata-only", action="store_true", help="Return identity and version without historical text.")

    check = subparsers.add_parser("doctor", help="Validate project memory files.")
    check.add_argument("path", nargs="?", default=".", help="Project path. Defaults to current directory.")

    scan = subparsers.add_parser("scan-scripts", help="List key shell scripts considered by the bootstrap.")
    scan.add_argument("path", nargs="?", default=".", help="Project path. Defaults to current directory.")

    index = subparsers.add_parser("index-memory", help="Build a local SQLite index for project memory files.")
    index.add_argument("path", nargs="?", default=".", help="Project path. Defaults to current directory.")
    index.add_argument("--db", help="Override index database path. Defaults to .codex/cache/memory-index.sqlite.")

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

    context = subparsers.add_parser("context-memory", help="Render compact memory context for a query.")
    context.add_argument("path", help="Project path.")
    context.add_argument("query", nargs="?", help="Search query. Optional when --id is used.")
    context.add_argument("--id", dest="ids", action="append", help="Render context from a specific chunk ID. Repeatable.")
    context.add_argument("--db", help="Override index database path.")
    context.add_argument("--limit", type=int, default=5, help="Maximum indexed chunks to consider. Defaults to 5.")
    context.add_argument("--max-tokens", type=int, default=1000, help="Approximate output token budget. Defaults to 1000.")

    for command in (index, search, get, status, context):
        scope = command.add_mutually_exclusive_group()
        scope.add_argument("--include-legacy", action="store_true", help="Explicitly include legacy memory and agent files.")
        scope.add_argument("--no-legacy", dest="include_legacy", action="store_false", help="Use only current project memory sources (the default).")
        command.set_defaults(include_legacy=False)
    for command in (search, get, context):
        command.add_argument("--history", action="store_true", help="Include retired governed facts; forgotten content remains excluded.")

    consolidate = subparsers.add_parser("consolidate-memory", help="Create or apply a memory consolidation report.")
    consolidate.add_argument("path", nargs="?", default=".", help="Project path. Defaults to current directory.")
    consolidate_mode = consolidate.add_mutually_exclusive_group(required=True)
    consolidate_mode.add_argument("--dry-run", action="store_true", help="Generate a report without editing memory files.")
    consolidate_mode.add_argument("--apply", action="store_true", help="Apply selected safe items from a report.")
    consolidate_mode.add_argument("--agent-apply", action="store_true", help="Generate a fresh report, then let the cleanup agent apply deterministic actions.")
    consolidate.add_argument("--from-report", default="latest", help="Report JSON path or 'latest' when applying.")
    consolidate.add_argument("--only", nargs="*", default=(), help="Report item IDs to apply. Accepts repeated or comma-separated IDs.")
    consolidate.add_argument("--safe", action="store_true", help="Only apply items marked safe_apply.")
    consolidate.add_argument(
        "--report-dir",
        help="Override the consolidation report directory. Also available via PROJECT_MEMORY_BOOTSTRAP_REPORT_DIR.",
    )
    consolidate.add_argument(
        "--archive-day-memories-older-than-days",
        type=int,
        default=180,
        help="With --agent-apply, archive old day-memory files older than this many days when they have no promotable or conflict event. Defaults to 180.",
    )
    consolidate.add_argument("--json", action="store_true", help="Emit report/result as JSON.")

    reports = subparsers.add_parser("consolidation-reports", help="List memory consolidation reports.")
    reports.add_argument("path", nargs="?", default=".", help="Project path. Defaults to current directory.")

    report = subparsers.add_parser("consolidation-report", help="Show one memory consolidation report.")
    report.add_argument("path", nargs="?", default=".", help="Project path. Defaults to current directory.")
    report.add_argument("--latest", action="store_true", help="Show the latest report.")
    report.add_argument("--json", action="store_true", help="Emit JSON report instead of Markdown.")
    report.add_argument("--file", help="Specific report JSON or Markdown path.")

    return parser


def _main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "hook-memory":
        from .hooks import handle_hook
        try:
            payload = json.load(sys.stdin)
        except (ValueError, UnicodeError):
            payload = None
        # Native hosts interpret exit 2 as a blocking rejection. Callback failures
        # are surfaced in diagnostics/context and must not block the user's task.
        print(json.dumps(handle_hook(args.tool, payload), ensure_ascii=False))
        return 0
    if args.command == "hook-status":
        from .hooks import hook_status
        print(json.dumps(hook_status(Path(args.path)), ensure_ascii=False, indent=2))
        return 0
    if args.command == "install-adapter":
        from .adapters import install_adapter
        result = install_adapter(args.tool, Path(args.config),
            ownership_path=Path(args.ownership) if args.ownership else None,
            project_root=Path(args.project_root) if args.project_root else None)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if args.command in {"native-sessions", "native-messages", "native-message"}:
        from .native_sources import list_sessions, read_messages
        if args.command == "native-sessions":
            result = list_sessions(Path(args.path), args.tool)
        else:
            result = read_messages(Path(args.path), args.tool, args.session,
                                   message_id=getattr(args, "message", None))
            if args.command == "native-messages":
                if args.role:
                    result = [row for row in result if row["role"] == args.role]
                if args.query:
                    result = [row for row in result if args.query.casefold() in row.get("text", "").casefold()]
                result = [{k: v for k, v in row.items() if k != "text"} for row in result]
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if args.command in {"begin-maintenance", "finish-maintenance", "maintenance-runs"}:
        from . import maintenance
        if args.command == "begin-maintenance":
            result = maintenance.begin(Path(args.path), args.tool, args.session)
        elif args.command == "finish-maintenance":
            payload = json.load(sys.stdin) if args.input == "-" else json.loads(Path(args.input).read_text(encoding="utf-8"))
            result = maintenance.finish(Path(args.path), args.run_id, payload)
        else:
            result = maintenance.runs(Path(args.path))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if args.command in {"native-notes", "native-note"}:
        from .native_notes import list_notes, read_note
        result = (list_notes(Path(args.path), tools=tuple(args.tool or ("zcode", "claude", "workbuddy")), query=args.query)
                  if args.command == "native-notes" else read_note(Path(args.path), args.locator))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if args.command == "scan-sources":
        from .discovery import scan_sources
        selected = () if args.files_only else tuple(args.tool or ("codex", "claude", "zcode", "workbuddy", "opencode"))
        result = scan_sources(Path(args.path), tools=selected)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1 if result["errors"] else 0
    if args.command == "protocol":
        text = resources.files("project_memory_bootstrap_codex").joinpath("resources/shared-memory-protocol.md").read_text(encoding="utf-8")
        if args.brief:
            text = text.split("## 快速协议\n", 1)[1].split("\n## 1.", 1)[0].strip()
        print(text)
        return 0
    if args.command in {"consolidate-memory", "consolidation-report", "consolidation-reports"} and resolve_context(Path(args.path)) is not None:
        raise MemoryProtocolError("LEGACY_CONSOLIDATION_DISABLED", "Use governed candidates and publication; legacy cleanup and reports are not governed recall.")

    if args.command == "register-project":
        root = Path(args.path).expanduser().resolve()
        new_registration = not (root / ".project-memory.json").exists()
        if not (root / ".codex/memory/MEMORY.md").is_file():
            raise MemoryProtocolError("MEMORY_MISSING", "Run init before registering shared memory.")
        members = {"root": "."}
        for item in args.member:
            if "=" not in item:
                raise MemoryProtocolError("PROJECT_CONFIG_INVALID", "Members must use ID=RELATIVE_PATH.")
            key, value = item.split("=", 1)
            members[key] = value
        context = register_project(root, args.project_id, members, maintenance_enabled=args.maintenance_enabled)
        result = memory_state.initialize_state(context, new_registration=new_registration)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if args.command == "register-alias":
        result = register_alias(Path(args.path), Path(args.workspace), args.repository)
        print(json.dumps(dataclasses.asdict(result), default=str, ensure_ascii=False, indent=2))
        return 0
    if args.command == "project-context":
        context = resolve_context(Path(args.path))
        if context is None:
            raise MemoryProtocolError("PROJECT_UNREGISTERED", "No shared project is registered for this workspace.")
        print(json.dumps(dataclasses.asdict(context), default=str, ensure_ascii=False, indent=2))
        return 0
    if args.command in {"capture-memory", "propose-memory"}:
        payload = json.load(sys.stdin) if args.input == "-" else json.loads(Path(args.input).read_text(encoding="utf-8"))
        action = memory_state.capture if args.command == "capture-memory" else memory_state.propose
        print(json.dumps(action(Path(args.path), payload), ensure_ascii=False, indent=2))
        return 0
    actions = {
        "pending-memory": lambda: memory_state.pending(Path(args.path)),
        "memory-health": lambda: memory_state.health(Path(args.path)),
        "recover-memory": lambda: memory_state.recover(Path(args.path)),
        "commit-memory": lambda: memory_state.commit(Path(args.path), args.proposal_id),
        "inspect-memory": lambda: memory_state.inspect(Path(args.path), args.fact_id, metadata_only=args.metadata_only),
        "candidate-memory": lambda: memory_state.candidate(Path(args.path), args.candidate_id),
    }
    if args.command in actions:
        print(json.dumps(actions[args.command](), ensure_ascii=False, indent=2))
        return 0

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
            include_legacy=args.include_legacy,
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
            include_legacy=args.include_legacy,
            history=args.history,
        )
        print(search_results_to_json(results) if args.json else format_search_table(results))
        return 0

    if args.command == "get-memory":
        result = get_memory_by_id(
            Path(args.path),
            args.id,
            db_path=Path(args.db).expanduser().resolve() if args.db else None,
            include_legacy=args.include_legacy,
            history=args.history,
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
            include_legacy=args.include_legacy,
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
            print(context_from_ids(Path(args.path), args.ids, db_path=db_path, max_tokens=args.max_tokens, include_legacy=args.include_legacy, history=args.history))
            return 0
        if not args.query:
            print("context-memory requires a query unless --id is provided", file=sys.stderr)
            return 2
        results = search_memory(Path(args.path), args.query, db_path=db_path, limit=max(1, args.limit), include_legacy=args.include_legacy, history=args.history)
        print(context_from_results(results, max_tokens=args.max_tokens))
        return 0

    if args.command == "consolidate-memory":
        report_dir = Path(args.report_dir).expanduser().resolve() if args.report_dir else None
        if args.dry_run:
            report = generate_consolidation_report(Path(args.path), write=True, report_dir=report_dir)
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
        if args.agent_apply:
            result = agent_apply_consolidation(
                Path(args.path),
                archive_day_memory_older_than_days=max(0, args.archive_day_memories_older_than_days),
                report_dir=report_dir,
            )
            if args.json:
                print(
                    json.dumps(
                        {
                            "report": {
                                "markdown_path": str(result.report.markdown_path),
                                "json_path": str(result.report.json_path),
                                "items": len(result.report.items),
                            },
                            "applied": list(result.applied),
                            "skipped": list(result.skipped),
                            "archived": list(result.archived),
                            "cleaned": list(result.cleaned),
                            "messages": list(result.messages),
                        },
                        ensure_ascii=False,
                        indent=2,
                    )
                )
            else:
                for message in result.messages:
                    print(message)
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


def main(argv: list[str] | None = None) -> int:
    try:
        return _main(argv)
    except MemoryProtocolError as exc:
        print(json.dumps({"error": exc.as_dict()}, ensure_ascii=False), file=sys.stderr)
        return 2
    except json.JSONDecodeError:
        print(json.dumps({"error": {"code": "INPUT_INVALID", "message": "Input is not valid JSON."}}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
