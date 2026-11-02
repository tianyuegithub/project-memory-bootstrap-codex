"""Durable intake and single-document publication for registered projects."""

from __future__ import annotations

import contextlib
import datetime as dt
import json
import os
import re
import sqlite3
import tempfile
from pathlib import Path
from typing import Any, Iterator

from .bootstrap import _ensure_safe_write_target
from .errors import MemoryProtocolError
from .facts import apply_changes, digest, inspect_fact, parse_facts, reject_sensitive, validate_changes
from .project import ProjectContext, resolve_context


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _context(path: Path, *, writing: bool = False) -> ProjectContext:
    context = resolve_context(path)
    if context is None or (writing and not context.registered):
        raise MemoryProtocolError("PROJECT_UNREGISTERED", "Register this workspace before shared memory operations.")
    if writing and not context.maintenance_enabled:
        raise MemoryProtocolError("MAINTENANCE_DISABLED", "This project has not enabled shared-memory writes.")
    return context


def _atomic_text(path: Path, text: str, root: Path) -> None:
    _ensure_safe_write_target(path, root)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def initialize_state(context: ProjectContext, *, new_registration: bool = False) -> dict[str, Any]:
    database = context.state_dir / "ledger.sqlite"
    _ensure_safe_write_target(database, context.root)
    if database.exists():
        with _locked(context) as connection:
            return _health(connection, context)
    if not new_registration or (context.state_dir / "initialized.json").exists():
        raise MemoryProtocolError("STATE_MISSING", "The registered state is missing; do not recreate it from old Markdown.")
    if not context.memory_file.is_file():
        raise MemoryProtocolError("MEMORY_MISSING", "Initialize the project memory document first.")
    text = context.memory_file.read_text(encoding="utf-8")
    if parse_facts(text):
        raise MemoryProtocolError("STATE_MISSING", "Published facts exist without their matching state; inspected recovery is required.")
    context.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(context.state_dir, 0o700)
    with sqlite3.connect(database) as connection:
        connection.executescript("""
            CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE sources (
                id TEXT PRIMARY KEY, instance TEXT NOT NULL, object_id TEXT NOT NULL,
                revision TEXT NOT NULL, locator TEXT NOT NULL, kind TEXT NOT NULL, observed_at TEXT NOT NULL, epoch INTEGER NOT NULL);
            CREATE TABLE candidates (
                id TEXT PRIMARY KEY, source_id TEXT NOT NULL, source_revision TEXT NOT NULL,
                payload TEXT, status TEXT NOT NULL, received_at TEXT NOT NULL, source_epoch INTEGER NOT NULL,
                UNIQUE(source_id, source_revision, source_epoch));
            CREATE TABLE proposals (
                id TEXT PRIMARY KEY, candidate_id TEXT NOT NULL, payload TEXT,
                status TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE operations (
                id TEXT PRIMARY KEY, proposal_id TEXT UNIQUE NOT NULL, old_sha TEXT NOT NULL,
                new_sha TEXT NOT NULL, new_text TEXT, affected_ids TEXT NOT NULL,
                generation INTEGER NOT NULL, status TEXT NOT NULL, receipt TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE suppressions (fact_id TEXT PRIMARY KEY, generation INTEGER NOT NULL);
            CREATE TABLE suppressed_sources (source_id TEXT PRIMARY KEY);
        """)
        connection.executemany("INSERT INTO meta VALUES (?,?)", [
            ("schema_version", "1"), ("project_id", context.project_id),
            ("state_instance_id", context.state_instance_id), ("published_sha", digest(text)),
            ("suppression_generation", "0"), ("last_publication", ""),
        ])
    os.chmod(database, 0o600)
    _atomic_text(context.state_dir / "initialized.json", _json({"state_instance_id": context.state_instance_id}) + "\n", context.root)
    with _locked(context) as connection:
        return _health(connection, context)


@contextlib.contextmanager
def _locked(context: ProjectContext) -> Iterator[sqlite3.Connection]:
    try:
        import fcntl
    except ImportError:
        raise MemoryProtocolError("PLATFORM_UNSUPPORTED", "Shared publication currently requires POSIX file locks.") from None
    database = context.state_dir / "ledger.sqlite"
    for path in (database, context.state_dir / "publication.lock", context.memory_file):
        _ensure_safe_write_target(path, context.root)
    if not database.is_file():
        raise MemoryProtocolError("STATE_MISSING", "Persistent memory state is missing; shared recall is unavailable.")
    with (context.state_dir / "publication.lock").open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            connection = sqlite3.connect(database.as_uri() + "?mode=rw", uri=True)
            connection.row_factory = sqlite3.Row
            try:
                connection.execute("PRAGMA secure_delete=ON")
                meta = dict(connection.execute("SELECT key,value FROM meta"))
                if meta.get("schema_version") != "1" or meta.get("project_id") != context.project_id or meta.get("state_instance_id") != context.state_instance_id:
                    raise MemoryProtocolError("STATE_IDENTITY_MISMATCH", "Persistent state does not match the registered project instance.")
                yield connection
            except sqlite3.DatabaseError as exc:
                raise MemoryProtocolError("STATE_DATABASE_ERROR", "Persistent memory state could not be used safely.") from exc
            finally:
                connection.close()
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _source_file(context: ProjectContext, locator: str, *, allow_missing: bool = False) -> Path:
    candidate = Path(locator.split("#", 1)[0])
    path = (candidate if candidate.is_absolute() else context.workspace_root / candidate).resolve()
    if path.name.startswith(".env") or path.name in {"auth.json", "credentials.json"}:
        raise MemoryProtocolError("SENSITIVE_SOURCE", "Credential files cannot be memory evidence inputs.")
    if not path.is_file() and not (allow_missing and not path.exists()):
        raise MemoryProtocolError("SOURCE_UNAVAILABLE", "The cited file is unavailable.")
    identity_path = path
    while not identity_path.exists() and identity_path != identity_path.parent:
        identity_path = identity_path.parent
    source_context = resolve_context(identity_path)
    if source_context is None or source_context.project_id != context.project_id or source_context.root != context.root:
        raise MemoryProtocolError("SOURCE_OUTSIDE_PROJECT", "File evidence is not a registered project member.")
    return path


def _verify_source(context: ProjectContext, source: dict[str, Any]) -> None:
    if source["kind"] == "file":
        import hashlib
        if source["revision"] == "missing":
            observed = _source_file(context, source["locator"], allow_missing=True)
            if observed.exists():
                raise MemoryProtocolError("SOURCE_CHANGED", "The file has reappeared after its absence was observed.")
            return
        actual = hashlib.sha256(_source_file(context, source["locator"]).read_bytes()).hexdigest()
        if actual != source["revision"]:
            raise MemoryProtocolError("SOURCE_CHANGED", "File evidence changed after it was observed.")
    elif source["kind"] == "native_memory_note":
        from .native_notes import source_snapshot
        snapshot = source_snapshot(context.root, source["locator"], allow_missing=source["revision"] == "missing")
        if snapshot["source_revision"] != source["revision"]:
            raise MemoryProtocolError("SOURCE_CHANGED", "Native project memory changed after it was observed.")
    elif source["kind"] == "native_message":
        from .native_sources import source_snapshot
        snapshot = source_snapshot(context.root, source["locator"])
        if snapshot["source_revision"] != source["revision"]:
            raise MemoryProtocolError("SOURCE_CHANGED", "Native public message changed after it was observed.")


def source_suppressions(context: ProjectContext) -> set[str]:
    """The same durable source-forgetting boundary for native evidence exports."""
    with _locked(context) as connection:
        _recover(connection, context)
        return {row[0] for row in connection.execute("SELECT source_id FROM suppressed_sources")}


def capture(path: Path, payload: Any) -> dict[str, Any]:
    context = _context(path, writing=True)
    if not isinstance(payload, dict):
        raise MemoryProtocolError("CANDIDATE_INVALID", "Candidate input must be an object.")
    for key in ("source_instance", "object_id", "source_revision", "locator", "kind", "observed_at"):
        if not isinstance(payload.get(key), str) or not payload[key].strip():
            raise MemoryProtocolError("CANDIDATE_INVALID", f"Candidate requires {key}.")
    try:
        observed = dt.datetime.fromisoformat(payload["observed_at"].replace("Z", "+00:00"))
        if observed.tzinfo is None:
            raise ValueError()
    except ValueError:
        raise MemoryProtocolError("CANDIDATE_INVALID", "observed_at requires an ISO timestamp with timezone.") from None
    if "excerpt" in payload and not isinstance(payload["excerpt"], str):
        raise MemoryProtocolError("CANDIDATE_INVALID", "excerpt must be text.")
    if type(payload.get("enqueue", True)) is not bool:
        raise MemoryProtocolError("CANDIDATE_INVALID", "enqueue must be a boolean.")
    reject_sensitive(payload)
    accepted = {k: payload[k] for k in ("source_instance", "object_id", "source_revision", "locator", "kind", "observed_at", "excerpt") if k in payload}
    accepted["observed_at"] = observed.astimezone(dt.timezone.utc).isoformat()
    # Explicit native URI protocols own their carrier identity; two tools naming
    # the same original message differently must not create different sources.
    scheme = accepted["locator"].partition("://")[0].casefold()
    if scheme == "native-memory":
        accepted["kind"] = "native_memory_note"
    elif scheme == "native-message":
        accepted["kind"] = "native_message"
    if accepted["kind"] == "file":
        evidence = _source_file(context, accepted["locator"], allow_missing=accepted["source_revision"] == "missing")
        fragment = accepted["locator"].partition("#")[2]
        accepted["locator"] = str(evidence) + ("#" + fragment if fragment else "")
        accepted["object_id"] = accepted["locator"]
        accepted["source_instance"] = "file"
    elif accepted["kind"] == "native_memory_note":
        from .native_notes import source_snapshot
        snapshot = source_snapshot(context.root, accepted["locator"], allow_missing=accepted["source_revision"] == "missing")
        if snapshot["source_revision"] != accepted["source_revision"]:
            raise MemoryProtocolError("SOURCE_CHANGED", "Native project memory changed before intake.")
        accepted.update({key: snapshot[key] for key in ("source_instance", "object_id", "locator")})
    elif accepted["kind"] == "native_message":
        from .native_sources import source_snapshot
        snapshot = source_snapshot(context.root, accepted["locator"])
        if snapshot["source_revision"] != accepted["source_revision"]:
            raise MemoryProtocolError("SOURCE_CHANGED", "Native public message changed before intake.")
        accepted.update({key: snapshot[key] for key in ("source_instance", "object_id", "locator")})
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", accepted["source_instance"]):
        raise MemoryProtocolError("CANDIDATE_INVALID", "source_instance must be an unambiguous namespace.")
    if accepted["source_instance"] == "file" and accepted["kind"] != "file":
        raise MemoryProtocolError("CANDIDATE_INVALID", "The file namespace requires verified file evidence.")
    for prefix, kind in (("native-memory.", "native_memory_note"), ("native-message.", "native_message")):
        if accepted["source_instance"].startswith(prefix) and accepted["kind"] != kind:
            raise MemoryProtocolError("CANDIDATE_INVALID", "Native namespaces require their verified source carrier.")
    source_id = accepted["source_instance"] + ":" + accepted["object_id"]
    source = {"id": source_id, "instance": accepted["source_instance"], "object_id": accepted["object_id"],
              "revision": accepted["source_revision"], "locator": accepted["locator"], "kind": accepted["kind"], "observed_at": accepted["observed_at"]}
    if source["kind"] not in {"native_memory_note", "native_message"}:
        _verify_source(context, source)
    with _locked(context) as connection:
        _recover(connection, context)
        if connection.execute("SELECT 1 FROM suppressed_sources WHERE source_id=?", (source_id,)).fetchone():
            raise MemoryProtocolError("SOURCE_SUPPRESSED", "This source is covered by a forgetting decision.")
        previous = connection.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
        if previous and any(previous[key] != source[key] for key in ("instance", "object_id", "kind", "locator")):
            raise MemoryProtocolError("SOURCE_IDENTITY_CONFLICT", "Source type and locator cannot be changed through a content revision.")
        if previous and previous["observed_at"] == source["observed_at"] and previous["revision"] != source["revision"]:
            raise MemoryProtocolError("SOURCE_VERSION_CONFLICT", "Source versions have an ambiguous observation order.")
        epoch = previous["epoch"] if previous else 1
        if previous and source["observed_at"] > previous["observed_at"] and source["revision"] != previous["revision"]:
            epoch += 1
        candidate_id = digest(_json([context.project_id, source_id, accepted["source_revision"], epoch]))
        existing = connection.execute("SELECT * FROM candidates WHERE id=?", (candidate_id,)).fetchone()
        if existing:
            if existing["payload"] is not None:
                old_payload = json.loads(existing["payload"])
                annotation_keys = {"observed_at"}
                if source["kind"] in {"file", "native_message", "native_memory_note"}:
                    # Their content revision was checked against the real source.
                    # Different excerpts are annotations, not different sources.
                    annotation_keys.add("excerpt")
                if {k: v for k, v in old_payload.items() if k not in annotation_keys} != {k: v for k, v in accepted.items() if k not in annotation_keys}:
                    raise MemoryProtocolError("SOURCE_VERSION_CONFLICT", "The same source version arrived with different content.")
            if previous["revision"] == source["revision"] and source["observed_at"] > previous["observed_at"]:
                with connection:
                    connection.execute("UPDATE sources SET observed_at=? WHERE id=?", (source["observed_at"], source_id))
            status = existing["status"]
            if status == "evidence" and payload.get("enqueue", True):
                with connection:
                    connection.execute("UPDATE candidates SET status='pending' WHERE id=?", (candidate_id,))
                status = "pending"
            return {"candidate_id": candidate_id, "source_id": source_id, "source_revision": source["revision"],
                    "locator": source["locator"], "source_epoch": epoch, "status": status, "duplicate": True}
        status = "pending" if payload.get("enqueue", True) else "evidence"
        with connection:
            if not previous or previous["observed_at"] <= source["observed_at"]:
                connection.execute("INSERT OR REPLACE INTO sources VALUES (?,?,?,?,?,?,?,?)", tuple(source[k] for k in ("id", "instance", "object_id", "revision", "locator", "kind", "observed_at")) + (epoch,))
            connection.execute("INSERT INTO candidates VALUES (?,?,?,?,?,?,?)", (candidate_id, source_id, source["revision"], _json(accepted), status, _now(), epoch))
    return {"candidate_id": candidate_id, "source_id": source_id, "source_revision": source["revision"],
            "locator": source["locator"], "source_epoch": epoch, "status": status, "duplicate": False}


def propose(path: Path, payload: Any) -> dict[str, Any]:
    context = _context(path, writing=True)
    if not isinstance(payload, dict):
        raise MemoryProtocolError("PROPOSAL_INVALID", "Proposal input must be an object.")
    for key in ("candidate_id", "verified_by", "verification_scope"):
        if not isinstance(payload.get(key), str) or not payload[key].strip():
            raise MemoryProtocolError("PROPOSAL_INVALID", f"Proposal requires {key}.")
    validate_changes(payload.get("changes"), context.project_id)
    if not payload["changes"] and (not isinstance(payload.get("skip_reason"), str) or not payload["skip_reason"].strip()):
        raise MemoryProtocolError("PROPOSAL_INVALID", "A no-change decision requires skip_reason.")
    reject_sensitive(payload)
    proposal_id = digest(_json(payload))
    with _locked(context) as connection:
        _recover(connection, context)
        candidate = connection.execute("SELECT * FROM candidates WHERE id=?", (payload["candidate_id"],)).fetchone()
        if candidate is None:
            raise MemoryProtocolError("CANDIDATE_NOT_FOUND", "The proposal candidate was not captured.")
        if candidate["status"] == "suppressed":
            raise MemoryProtocolError("SOURCE_SUPPRESSED", "The candidate is suppressed.")
        existing = connection.execute("SELECT status FROM proposals WHERE id=?", (proposal_id,)).fetchone()
        if existing:
            return {"proposal_id": proposal_id, "status": existing["status"], "duplicate": True}
        if candidate["status"] == "committed":
            raise MemoryProtocolError("CANDIDATE_PROCESSED", "This source version already has a committed decision.")
        with connection:
            connection.execute("INSERT INTO proposals VALUES (?,?,?,?,?)", (proposal_id, payload["candidate_id"], _json(payload), "proposed", _now()))
    return {"proposal_id": proposal_id, "status": "proposed", "duplicate": False}


def _meta(connection: sqlite3.Connection, key: str) -> str:
    return connection.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()[0]


def _check_sources(connection: sqlite3.Connection, context: ProjectContext, proposal: dict[str, Any]) -> None:
    candidate = connection.execute("SELECT * FROM candidates WHERE id=?", (proposal["candidate_id"],)).fetchone()
    if candidate["status"] == "suppressed":
        raise MemoryProtocolError("SOURCE_SUPPRESSED", "The candidate is suppressed.")
    if candidate["status"] not in {"pending", "evidence"}:
        raise MemoryProtocolError("CANDIDATE_PROCESSED", "Another decision already processed this candidate.")
    if not proposal["changes"]:
        return
    read_set = [(candidate["source_id"], candidate["source_revision"])]
    current_epoch = connection.execute("SELECT epoch FROM sources WHERE id=?", (candidate["source_id"],)).fetchone()
    if current_epoch is None or current_epoch[0] != candidate["source_epoch"]:
        raise MemoryProtocolError("SOURCE_CHANGED", "The source passed through another observed revision.")
    for change in proposal["changes"]:
        if change["status"] != "forgotten" and connection.execute("SELECT 1 FROM suppressions WHERE fact_id=?", (change["fact_id"],)).fetchone():
            raise MemoryProtocolError("FACT_SUPPRESSED", "The fact is covered by a forgetting decision.")
        read_set.extend((s["source_id"], s["version"]) for s in change["sources"])
        for cited in change["sources"]:
            recorded = connection.execute("SELECT locator FROM sources WHERE id=?", (cited["source_id"],)).fetchone()
            if recorded is not None and cited["locator"] != recorded[0]:
                raise MemoryProtocolError("SOURCE_LOCATOR_MISMATCH", "The citation locator differs from its captured source.")
    for source_id, revision in read_set:
        source = connection.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
        if source is None:
            raise MemoryProtocolError("SOURCE_NOT_CAPTURED", "Capture every cited source before publication.")
        if source["revision"] != revision:
            raise MemoryProtocolError("SOURCE_CHANGED", "A newer source revision has been observed.")
        if connection.execute("SELECT 1 FROM suppressed_sources WHERE source_id=?", (source_id,)).fetchone():
            raise MemoryProtocolError("SOURCE_SUPPRESSED", "Cited evidence is suppressed.")
        _verify_source(context, dict(source))


def _read_published(connection: sqlite3.Connection, context: ProjectContext) -> str:
    if not context.memory_file.is_file():
        raise MemoryProtocolError("MEMORY_MISSING", "Published memory document is missing.")
    text = context.memory_file.read_text(encoding="utf-8")
    if digest(text) != _meta(connection, "published_sha"):
        raise MemoryProtocolError("EXTERNAL_EDIT_CONFLICT", "The memory document changed outside the publication protocol.")
    for fact in parse_facts(text):
        if fact.metadata["project_id"] != context.project_id:
            raise MemoryProtocolError("FACT_PROJECT_MISMATCH", "Published fact belongs to another project.")
    return text


def _finish(connection: sqlite3.Connection, operation: sqlite3.Row) -> None:
    proposal = connection.execute("SELECT candidate_id FROM proposals WHERE id=?", (operation["proposal_id"],)).fetchone()
    with connection:
        connection.execute("UPDATE meta SET value=? WHERE key='published_sha'", (operation["new_sha"],))
        if operation["old_sha"] != operation["new_sha"]:
            connection.execute("UPDATE meta SET value=? WHERE key='last_publication'", (_now(),))
        connection.execute("UPDATE operations SET status='committed',new_text=NULL WHERE id=?", (operation["id"],))
        connection.execute("UPDATE proposals SET status='committed' WHERE id=?", (operation["proposal_id"],))
        connection.execute("UPDATE candidates SET status='committed' WHERE id=? AND status!='suppressed'", (proposal["candidate_id"],))


def _recover(connection: sqlite3.Connection, context: ProjectContext) -> None:
    operations = connection.execute("SELECT * FROM operations WHERE status='prepared' ORDER BY created_at,id").fetchall()
    for operation in operations:
        generation = int(_meta(connection, "suppression_generation"))
        if generation != operation["generation"]:
            raise MemoryProtocolError("RECOVERY_SUPPRESSION_CONFLICT", "Suppression changed after this operation was prepared.")
        current = context.memory_file.read_text(encoding="utf-8") if context.memory_file.exists() else ""
        current_sha = digest(current)
        if current_sha == operation["old_sha"] and operation["old_sha"] != operation["new_sha"]:
            _atomic_text(context.memory_file, operation["new_text"], context.root)
        elif current_sha != operation["new_sha"]:
            raise MemoryProtocolError("EXTERNAL_EDIT_CONFLICT", "An interrupted publication conflicts with an external edit.")
        _finish(connection, operation)


def commit(path: Path, proposal_id: str) -> dict[str, Any]:
    context = _context(path, writing=True)
    with _locked(context) as connection:
        _recover(connection, context)
        done = connection.execute("SELECT receipt FROM operations WHERE proposal_id=? AND status='committed'", (proposal_id,)).fetchone()
        if done:
            return json.loads(done["receipt"])
        row = connection.execute("SELECT * FROM proposals WHERE id=?", (proposal_id,)).fetchone()
        if row is None or row["payload"] is None:
            raise MemoryProtocolError("PROPOSAL_NOT_FOUND", "No usable proposal is available.")
        proposal = json.loads(row["payload"])
        _check_sources(connection, context, proposal)
        current = _read_published(connection, context)
        forgotten = [change["fact_id"] for change in proposal["changes"] if change["status"] == "forgotten"]
        suppression_sources: set[str] = set()
        for change in proposal["changes"]:
            if change["status"] != "forgotten" or not change.get("forget_sources", False):
                continue
            old = inspect_fact(current, change["fact_id"])
            if old is None or not old.metadata.get("sources"):
                raise MemoryProtocolError("SOURCE_SCOPE_UNAVAILABLE", "The source scope is unavailable; do not report source forgetting as complete.")
            suppression_sources.update(s["source_id"] for s in old.metadata["sources"])
        if suppression_sources:
            explicit_changes = {change["fact_id"]: change for change in proposal["changes"]}
            forgotten_at = next(c["verified_at"] for c in proposal["changes"] if c["status"] == "forgotten")
            for related in parse_facts(current):
                if not any(s["source_id"] in suppression_sources for s in related.metadata["sources"]):
                    continue
                if related.fact_id in explicit_changes:
                    if explicit_changes[related.fact_id]["status"] != "forgotten":
                        raise MemoryProtocolError("FORGET_SCOPE_CONFLICT", "A proposal cannot publish and forget the same source-derived fact.")
                    continue
                change = dict(related.metadata)
                change.update(expected_version=related.version, body="", status="forgotten", sources=[],
                              verified_at=forgotten_at, verification="Apply the explicit source-level forgetting scope.")
                proposal["changes"].append(change)
                forgotten.append(related.fact_id)
        updated, facts = apply_changes(current, proposal["changes"], context.project_id)
        generation = int(_meta(connection, "suppression_generation")) + bool(forgotten)
        operation_id = proposal_id
        receipt = {"operation_id": operation_id, "proposal_id": proposal_id, "project_id": context.project_id,
                   "status": "committed", "facts": facts, "document_version": digest(updated),
                   "forgotten_fact_ids": forgotten, "suppressed_sources": sorted(suppression_sources)}
        with connection:
            for fact_id in forgotten:
                connection.execute("INSERT OR REPLACE INTO suppressions VALUES (?,?)", (fact_id, generation))
            for source_id in suppression_sources:
                connection.execute("INSERT OR IGNORE INTO suppressed_sources VALUES (?)", (source_id,))
                connection.execute("UPDATE candidates SET payload=NULL,status='suppressed' WHERE source_id=?", (source_id,))
            if forgotten:
                connection.execute("UPDATE meta SET value=? WHERE key='suppression_generation'", (str(generation),))
                # Old proposals can contain removed text even if their candidates have different source IDs.
                for prior in connection.execute("SELECT id,payload FROM proposals WHERE payload IS NOT NULL").fetchall():
                    prior_payload = json.loads(prior["payload"])
                    if any(c["fact_id"] in forgotten or any(s["source_id"] in suppression_sources for s in c["sources"]) for c in prior_payload["changes"]):
                        connection.execute("UPDATE candidates SET payload=NULL,status=CASE WHEN status='committed' THEN status ELSE 'suppressed' END WHERE id=?", (prior_payload["candidate_id"],))
                        connection.execute("UPDATE proposals SET payload=NULL,status=CASE WHEN status='committed' THEN status ELSE 'suppressed' END WHERE id=?", (prior["id"],))
            connection.execute("INSERT INTO operations VALUES (?,?,?,?,?,?,?,?,?,?)", (
                operation_id, proposal_id, digest(current), digest(updated), updated,
                _json([c["fact_id"] for c in proposal["changes"]]), generation, "prepared", _json(receipt), _now(),
            ))
        if updated != current:
            _atomic_text(context.memory_file, updated, context.root)
        operation = connection.execute("SELECT * FROM operations WHERE id=?", (operation_id,)).fetchone()
        _finish(connection, operation)
        return receipt


@contextlib.contextmanager
def published_document(path: Path) -> Iterator[tuple[ProjectContext | None, str | None, set[str], set[str]]]:
    context = resolve_context(path)
    if context is None:
        yield None, None, set(), set()
        return
    with _locked(context) as connection:
        _recover(connection, context)
        text = _read_published(connection, context)
        suppressed = {row[0] for row in connection.execute("SELECT fact_id FROM suppressions")}
        sources = {row[0] for row in connection.execute("SELECT source_id FROM suppressed_sources")}
        suppressed.update(f.fact_id for f in parse_facts(text) if any(s["source_id"] in sources for s in f.metadata["sources"]))
        yield context, text, suppressed, sources


def inspect(path: Path, fact_id: str, *, metadata_only: bool = False) -> dict[str, Any] | None:
    with published_document(path) as (context, text, suppressed, suppressed_sources):
        if context is None:
            raise MemoryProtocolError("PROJECT_UNREGISTERED", "Register the project to inspect governed facts.")
        if fact_id in suppressed:
            raise MemoryProtocolError("FACT_SUPPRESSED", "The requested fact is suppressed.")
        fact = inspect_fact(text, fact_id)
        if fact is None:
            return None
        if metadata_only:
            return {"fact_id": fact_id, "version": fact.version, "revision": fact.metadata["revision"],
                    "status": fact.metadata["status"], "legacy": fact.legacy}
        if fact.legacy and suppressed_sources:
            raise MemoryProtocolError("LEGACY_SUPPRESSION_UNCERTAIN", "Unstructured history cannot be recalled without resolving source suppression; metadata-only inspection is available.")
        return {"fact_id": fact_id, "version": fact.version, "metadata": fact.metadata, "body": fact.body, "legacy": fact.legacy}


def _health(connection: sqlite3.Connection, context: ProjectContext) -> dict[str, Any]:
    counts = {r[0]: r[1] for r in connection.execute("SELECT status,count(*) FROM candidates GROUP BY status")}
    return {"project_id": context.project_id, "state_instance_id": context.state_instance_id,
            "scope": "persistent_store", "registered": context.registered,
            "maintenance_enabled": context.maintenance_enabled, "candidate_counts": counts,
            "prepared_operations": connection.execute("SELECT count(*) FROM operations WHERE status='prepared'").fetchone()[0],
            "suppression_count": connection.execute("SELECT count(*) FROM suppressions").fetchone()[0],
            "last_publication": _meta(connection, "last_publication"), "document_version": _meta(connection, "published_sha")}


def health(path: Path) -> dict[str, Any]:
    context = _context(path)
    with _locked(context) as connection:
        result = _health(connection, context)
        if result["prepared_operations"]:
            result["status"] = "recovery_required"
        else:
            _read_published(connection, context)
            result["status"] = "ok"
        return result


def pending(path: Path) -> list[dict[str, Any]]:
    context = _context(path)
    with _locked(context) as connection:
        return [{"candidate_id": row["id"], "status": row["status"], "source_id": row["source_id"],
                 "source_revision": row["source_revision"], "received_at": row["received_at"]}
                for row in connection.execute("SELECT * FROM candidates WHERE status='pending' ORDER BY received_at,id")]


def candidate(path: Path, candidate_id: str) -> dict[str, Any]:
    context = _context(path)
    with _locked(context) as connection:
        row = connection.execute("SELECT * FROM candidates WHERE id=?", (candidate_id,)).fetchone()
        if row is None:
            raise MemoryProtocolError("CANDIDATE_NOT_FOUND", "Candidate does not exist.")
        if row["payload"] is None or row["status"] == "suppressed":
            raise MemoryProtocolError("SOURCE_SUPPRESSED", "Candidate content is no longer available.")
        return {"candidate_id": row["id"], "status": row["status"], "payload": json.loads(row["payload"])}


def recover(path: Path) -> dict[str, Any]:
    context = _context(path, writing=True)
    with _locked(context) as connection:
        _recover(connection, context)
        _read_published(connection, context)
        return _health(connection, context)
