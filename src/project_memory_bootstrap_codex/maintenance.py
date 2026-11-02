"""Receipts for semantic runs; an unfinished run is never reported as success."""
from __future__ import annotations

import json
from pathlib import Path
import uuid

from .errors import MemoryProtocolError
from .facts import reject_sensitive
from .memory_state import _context, _locked, _now, _recover


def _exists(connection):
    return connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='maintenance_runs'").fetchone() is not None


def begin(path: Path, tool: str, session_id: str) -> dict:
    if tool not in {"codex", "claude", "opencode", "zcode", "workbuddy"} or not isinstance(session_id, str) or not session_id.strip():
        raise MemoryProtocolError("RUN_INPUT_INVALID", "A supported tool and actual native session identifier are required.")
    reject_sensitive(session_id)
    context = _context(path, writing=True)
    receipt = {"run_id": str(uuid.uuid4()), "project_id": context.project_id, "tool": tool,
               "session_id": session_id, "status": "running", "started_at": _now(),
               "scope": "semantic_maintenance_only", "operation_ids": []}
    with _locked(context) as connection:
        _recover(connection, context)
        with connection:
            connection.execute("CREATE TABLE IF NOT EXISTS maintenance_runs (id TEXT PRIMARY KEY, tool TEXT NOT NULL, session_id TEXT NOT NULL, receipt TEXT NOT NULL)")
            connection.execute("INSERT INTO maintenance_runs VALUES (?,?,?,?)",
                               (receipt["run_id"], tool, session_id, json.dumps(receipt, ensure_ascii=False)))
    return receipt


def finish(path: Path, run_id: str, payload: dict) -> dict:
    if not isinstance(payload, dict) or payload.get("status") not in {"succeeded", "failed", "interrupted"}:
        raise MemoryProtocolError("RUN_INPUT_INVALID", "A terminal run status is required.")
    operations = payload.get("operation_ids", [])
    if not isinstance(operations, list) or any(not isinstance(item, str) for item in operations):
        raise MemoryProtocolError("RUN_INPUT_INVALID", "operation_ids must contain committed operation identifiers.")
    if not isinstance(payload.get("coverage"), str) or not payload["coverage"].strip():
        raise MemoryProtocolError("RUN_INPUT_INVALID", "A concrete coverage statement is required.")
    if payload["status"] != "succeeded" and not isinstance(payload.get("error_code"), str):
        raise MemoryProtocolError("RUN_INPUT_INVALID", "Failed/interrupted runs require a sanitized error code.")
    accepted = {key: payload[key] for key in ("status", "operation_ids", "coverage", "error_code") if key in payload}
    accepted["operation_ids"] = operations
    reject_sensitive(accepted)
    context = _context(path, writing=True)
    with _locked(context) as connection:
        _recover(connection, context)
        row = connection.execute("SELECT receipt FROM maintenance_runs WHERE id=?", (run_id,)).fetchone() if _exists(connection) else None
        if row is None:
            raise MemoryProtocolError("RUN_NOT_FOUND", "No started run has this identifier.")
        receipt = json.loads(row[0])
        if receipt["status"] != "running":
            if all(receipt.get(k) == v for k, v in accepted.items()):
                return receipt
            raise MemoryProtocolError("RUN_ALREADY_FINISHED", "A terminal receipt cannot be rewritten.")
        for operation in operations:
            if connection.execute("SELECT 1 FROM operations WHERE id=? AND status='committed'", (operation,)).fetchone() is None:
                raise MemoryProtocolError("RUN_OPERATION_UNCOMMITTED", "A claimed operation is not committed in this project.")
        receipt.update(accepted, finished_at=_now(),
                       pending_candidates=connection.execute("SELECT count(*) FROM candidates WHERE status='pending'").fetchone()[0])
        with connection:
            connection.execute("UPDATE maintenance_runs SET receipt=? WHERE id=?", (json.dumps(receipt, ensure_ascii=False), run_id))
    return receipt


def runs(path: Path) -> list[dict]:
    with _locked(_context(path)) as connection:
        return [json.loads(row[0]) for row in connection.execute("SELECT receipt FROM maintenance_runs ORDER BY rowid DESC")] if _exists(connection) else []


def is_internal_session(context, tool: str, session_id: str) -> bool:
    with _locked(context) as connection:
        return _exists(connection) and connection.execute("SELECT 1 FROM maintenance_runs WHERE tool=? AND session_id=? LIMIT 1", (tool, session_id)).fetchone() is not None
