"""Command line interface for project-memory-bootstrap-codex."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .bootstrap import doctor, find_project_root, init_project, scan_shell_scripts


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

    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
