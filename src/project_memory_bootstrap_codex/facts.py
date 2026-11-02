"""One readable Markdown document, with stable, indivisible fact records."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

from .errors import MemoryProtocolError

START = "<!-- project-memory-fact "
END = "<!-- /project-memory-fact -->"
OPEN_RE = re.compile(r"^<!-- project-memory-fact (.+) -->\r?$", re.MULTILINE)
CLOSE_RE = re.compile(r"^<!-- /project-memory-fact -->\r?$", re.MULTILINE)
ID_RE = re.compile(r"F-[A-Za-z0-9][A-Za-z0-9_-]*\Z")
STATUSES = {"active", "needs_review", "conflict", "superseded", "expired", "retracted", "forgotten"}
KINDS = {"requirement", "implementation", "verification", "decision", "lesson", "navigation"}
SENSITIVE_RE = re.compile(
    r"(?i)(?:(?:\b(?:password|secret|token|api[_-]?key)[\"'`]?\s*[:=：]"
    r"|(?:密码|口令|密钥|令牌)[\"'`]?\s*(?:[:：]|为|是))\s*[\"'`]?"
    r"(?!<|\$|\*|\.\.\.)[A-Za-z0-9_./+=-]{8,}"
    r"|\bBearer\s+[A-Za-z0-9._~+/=-]{20,}|\bAKIA[0-9A-Z]{16}\b"
    r"|\bcurl\b[^\r\n]*?\s(?:--user(?:\s+|=)|-u\s*)[\"'`]?[^\s\"'`:]+:"
    r"(?!<|\$|\*|\.\.\.)[^\s\"'`$<*][^\s\"'`]*"
    r"|\$\{(?:[A-Za-z_][A-Za-z0-9_]*_)?(?:PASSWORD|PASSWD|TOKEN|SECRET|API_KEY|ACCESS_KEY|PRIVATE_KEY)"
    r"(?::-|-)[\"'`]?(?!<|\$|\*|\.\.\.)[^\s\"'`$<*}][^}\r\n]*\}"
    r"|\b[a-z][a-z0-9+.-]*://[^/\s:@]+:[^/\s:@]+@)"
)

_NATIVE_CREDENTIAL_CONTEXT_RE = re.compile(
    r"(?i)(?:密码|口令|密钥|令牌|凭据|凭证|登录|免密|认证|鉴权|账号|账户|pgpass|passwordencoder"
    r"|\b[A-Za-z_]*(?:PASSWORD|PASSWD)[A-Za-z_]*\s*="
    r"|(?<![A-Za-z0-9_])(?:password|passwd|username|credentials?|secret)(?![A-Za-z0-9_])"
    r"|(?<![A-Za-z0-9_])(?:api|access|private)[ _-]?key(?![A-Za-z0-9_])"
    r"|(?:(?<![A-Za-z0-9_])(?:service|access|auth|session)|服务)[\s_-]*tokens?(?![A-Za-z0-9_])"
    r"|(?<![A-Za-z0-9_])(?:authorization|cookies?|basic[ _-]?auth)(?![A-Za-z0-9_])"
    r"|(?<![A-Za-z0-9_])curl(?![A-Za-z0-9_])[^\r\n]*?\s(?:--user(?:\s|=)|-u(?:\s|[^\s])))"
)


def native_credential_context(text: str) -> bool:
    """Conservatively quarantine native prose; this is not a credential verdict.

    Shared facts deliberately use only SENSITIVE_RE, so abstract security policy
    remains admissible after curation. Standalone budget tokens are not context.
    """
    return _NATIVE_CREDENTIAL_CONTEXT_RE.search(text) is not None


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def reject_sensitive(value: Any) -> None:
    # Keep structured key/value detection, and inspect original strings before
    # JSON escaping can hide a quoted credential inside an excerpt or body.
    pending = [value, json.dumps(value, ensure_ascii=False)]
    while pending:
        item = pending.pop()
        if isinstance(item, str):
            if SENSITIVE_RE.search(item):
                raise MemoryProtocolError("SENSITIVE_CONTENT", "Possible credential content; nothing was stored.")
        elif isinstance(item, dict):
            pending.extend(item.keys())
            pending.extend(item.values())
        elif isinstance(item, (list, tuple)):
            pending.extend(item)


def _nonempty(value: Any, field: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise MemoryProtocolError("FACT_INVALID", f"{field} must be a nonempty string")


def validate_metadata(meta: Any) -> None:
    if not isinstance(meta, dict) or meta.get("schema_version") != 1:
        raise MemoryProtocolError("FACT_INVALID", "Unsupported fact schema")
    fact_id = meta.get("fact_id")
    if not isinstance(fact_id, str) or not ID_RE.fullmatch(fact_id):
        raise MemoryProtocolError("FACT_INVALID", "fact_id must be a stable F-* identifier")
    for field in ("project_id", "title", "verification", "expires_when"):
        _nonempty(meta.get(field), field)
    if "\n" in meta["title"] or "\r" in meta["title"]:
        raise MemoryProtocolError("FACT_INVALID", "title must be one line")
    if type(meta.get("revision")) is not int or meta["revision"] < 1:
        raise MemoryProtocolError("FACT_INVALID", "revision must be a positive integer")
    if meta.get("kind") not in KINDS or meta.get("status") not in STATUSES:
        raise MemoryProtocolError("FACT_INVALID", "Unknown fact kind or lifecycle status")
    scope = meta.get("scope")
    if not isinstance(scope, list) or not scope:
        raise MemoryProtocolError("FACT_INVALID", "scope must be a nonempty list")
    for item in scope:
        _nonempty(item, "scope item")
    try:
        dt.date.fromisoformat(meta.get("verified_at", ""))
    except (TypeError, ValueError):
        raise MemoryProtocolError("FACT_INVALID", "verified_at must be an ISO date") from None
    sources = meta.get("sources")
    if not isinstance(sources, list) or (not sources and meta["status"] == "active"):
        raise MemoryProtocolError("FACT_INVALID", "An active fact requires source evidence")
    for source in sources:
        if not isinstance(source, dict):
            raise MemoryProtocolError("FACT_INVALID", "source must be an object")
        for field in ("source_id", "version", "locator"):
            _nonempty(source.get(field), f"source.{field}")
    supersedes = meta.get("supersedes", [])
    if not isinstance(supersedes, list) or any(not isinstance(x, str) or not ID_RE.fullmatch(x) for x in supersedes):
        raise MemoryProtocolError("FACT_INVALID", "supersedes must contain fact identifiers")
    if fact_id in supersedes:
        raise MemoryProtocolError("FACT_INVALID", "A fact cannot supersede itself")


@dataclass(frozen=True)
class Fact:
    metadata: dict[str, Any]
    body: str
    raw: str
    start: int
    end: int
    legacy: bool = False

    @property
    def fact_id(self) -> str:
        return self.metadata["fact_id"]

    @property
    def version(self) -> str:
        return digest(self.raw)

    @property
    def title(self) -> str:
        return self.metadata["title"]


def parse_facts(text: str) -> list[Fact]:
    facts: list[Fact] = []
    ids: set[str] = set()
    opens = list(OPEN_RE.finditer(text))
    if text.count(START) != len(opens):
        raise MemoryProtocolError("FACT_INVALID", "Malformed fact opening marker")
    for index, opening in enumerate(opens):
        closing = CLOSE_RE.search(text, opening.end())
        next_start = opens[index + 1].start() if index + 1 < len(opens) else len(text) + 1
        if closing is None or closing.start() >= next_start:
            raise MemoryProtocolError("FACT_INVALID", "Missing or nested fact closing marker")
        try:
            meta = json.loads(opening.group(1))
        except json.JSONDecodeError:
            raise MemoryProtocolError("FACT_INVALID", "Fact metadata is not JSON") from None
        validate_metadata(meta)
        if meta["fact_id"] in ids:
            raise MemoryProtocolError("FACT_INVALID", "Duplicate fact_id in document")
        ids.add(meta["fact_id"])
        content = text[opening.end():closing.start()].strip()
        heading = f"## {meta['fact_id']}：{meta['title']}"
        if not content.startswith(heading + "\n") and content != heading:
            raise MemoryProtocolError("FACT_INVALID", "Fact heading and metadata differ")
        body = content[len(heading):].strip()
        facts.append(Fact(meta, body, text[opening.start():closing.end()], opening.start(), closing.end()))
    if text.count(END) != len(facts):
        raise MemoryProtocolError("FACT_INVALID", "Orphan fact closing marker")
    return facts


def _find_legacy(text: str, fact_id: str, managed: list[Fact]) -> Fact | None:
    headings = list(re.finditer(r"^##\s+(F-[A-Za-z0-9][A-Za-z0-9_-]*)(?:[：:]\s*|\s+|$)([^\n]*)", text, re.MULTILINE))
    matches = [h for h in headings if h.group(1) == fact_id and not any(f.start <= h.start() < f.end for f in managed)]
    if len(matches) > 1:
        raise MemoryProtocolError("FACT_INVALID", "Ambiguous legacy fact identifier")
    if not matches:
        return None
    h = matches[0]
    next_heading = re.search(r"^##\s", text[h.end():], re.MULTILINE)
    end = h.end() + next_heading.start() if next_heading else len(text)
    end = min([end] + [f.start for f in managed if f.start > h.start()])
    raw = text[h.start():end].rstrip()
    return Fact({"fact_id": fact_id, "title": h.group(2).strip() or fact_id, "revision": 0,
                 "status": "needs_review"}, raw, raw, h.start(), h.start() + len(raw), True)


def inspect_fact(text: str, fact_id: str) -> Fact | None:
    managed = parse_facts(text)
    return next((f for f in managed if f.fact_id == fact_id), None) or _find_legacy(text, fact_id, managed)


def validate_changes(changes: Any, project_id: str) -> list[dict[str, Any]]:
    if not isinstance(changes, list):
        raise MemoryProtocolError("PROPOSAL_INVALID", "changes must be a list")
    seen: set[str] = set()
    for change in changes:
        if not isinstance(change, dict):
            raise MemoryProtocolError("PROPOSAL_INVALID", "change must be an object")
        if "expected_version" not in change or (change["expected_version"] is not None and not re.fullmatch(r"[0-9a-f]{64}", str(change["expected_version"]))):
            raise MemoryProtocolError("PROPOSAL_INVALID", "expected_version must be a SHA-256 or null")
        meta = {k: v for k, v in change.items() if k not in {"body", "expected_version"}}
        meta.update(schema_version=1, project_id=project_id, revision=1)
        validate_metadata(meta)
        if meta["fact_id"] in seen:
            raise MemoryProtocolError("PROPOSAL_INVALID", "Duplicate fact change in one proposal")
        seen.add(meta["fact_id"])
        body = change.get("body")
        if not isinstance(body, str) or (meta["status"] == "active" and not body.strip()):
            raise MemoryProtocolError("PROPOSAL_INVALID", "An active fact requires a body")
        if START in body or END in body:
            raise MemoryProtocolError("PROPOSAL_INVALID", "Reserved fact marker in body")
        if "forget_sources" in change and (type(change["forget_sources"]) is not bool or meta["status"] != "forgotten"):
            raise MemoryProtocolError("PROPOSAL_INVALID", "forget_sources is an explicit boolean scope for forgetting only")
    reject_sensitive(changes)
    return changes


def render_fact(meta: dict[str, Any], body: str) -> str:
    encoded = json.dumps(meta, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    encoded = encoded.replace("<", "\\u003c").replace(">", "\\u003e")
    return f"{START}{encoded} -->\n## {meta['fact_id']}：{meta['title']}\n\n{body.strip()}\n{END}"


def apply_changes(text: str, changes: list[dict[str, Any]], project_id: str) -> tuple[str, list[dict[str, Any]]]:
    managed = parse_facts(text)
    by_id = {f.fact_id: f for f in managed}
    edits: list[tuple[int, int, str]] = []
    appended: list[str] = []
    receipts: list[dict[str, Any]] = []
    for change in changes:
        fact_id = change["fact_id"]
        existing = by_id.get(fact_id) or _find_legacy(text, fact_id, managed)
        if (existing.version if existing else None) != change["expected_version"]:
            raise MemoryProtocolError("VERSION_CONFLICT", f"Fact changed since observation: {fact_id}")
        meta = {k: v for k, v in change.items() if k not in {"body", "expected_version"}}
        meta.update(schema_version=1, project_id=project_id, revision=(existing.metadata["revision"] if existing else 0) + 1)
        body = change["body"]
        if meta["status"] == "forgotten":
            meta.update(title="已遗忘", sources=[], verification="Content removed by an explicit forgetting proposal.",
                        expires_when="Suppression remains in effect", supersedes=[])
            body = ""
        rendered = render_fact(meta, body)
        if existing:
            edits.append((existing.start, existing.end, rendered))
        else:
            appended.append(rendered)
        receipts.append({"fact_id": fact_id, "revision": meta["revision"], "version": digest(rendered), "status": meta["status"]})
    result = text
    for start, end, replacement in sorted(edits, reverse=True):
        result = result[:start] + replacement + result[end:]
    if appended:
        result = result.rstrip() + "\n\n" + "\n\n".join(appended) + "\n"
    final = {f.fact_id: f for f in parse_facts(result)}
    for change in changes:
        if change["status"] == "forgotten":
            continue
        for old_id in change.get("supersedes", []):
            old = final.get(old_id)
            if old is None or old.metadata["status"] != "superseded":
                raise MemoryProtocolError("SUPERSESSION_INCOMPLETE", "A superseded fact must be updated in the same publication")
    return result, receipts
