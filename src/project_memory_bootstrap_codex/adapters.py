"""Bounded host configuration installation; receipts do not attest runtime use."""
from __future__ import annotations

from importlib import resources
import json
import os
from pathlib import Path
import shlex
import stat
import tempfile

from . import PROTOCOL_VERSION
from .errors import MemoryProtocolError
from .hooks import driver

_MARKER = "// Managed adapter for project-memory-bootstrap-codex."


def _read(path: Path) -> bytes | None:
    return path.read_bytes() if path.exists() else None


def _object(raw: bytes | None) -> dict:
    try:
        value = json.loads(raw) if raw is not None else {}
    except (ValueError, UnicodeError):
        raise MemoryProtocolError("CONFIG_INVALID", "Adapter configuration must be valid JSON.") from None
    if not isinstance(value, dict):
        raise MemoryProtocolError("CONFIG_INVALID", "Adapter configuration must be a JSON object.")
    return value


def _json(value: dict) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode()


def _replace(path: Path, expected: bytes | None, content: bytes) -> None:
    if _read(path) != expected:
        raise MemoryProtocolError("CONFIG_CHANGED", "Configuration changed during adapter installation.")
    if expected == content:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = stat.S_IMODE(path.stat().st_mode) if expected is not None else 0o600
    fd, temporary = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        if _read(path) != expected:
            raise MemoryProtocolError("CONFIG_CHANGED", "Configuration changed during adapter installation.")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def install_adapter(tool: str, config_path: Path, *, ownership_path: Path | None = None,
                    project_root: Path | None = None) -> dict:
    if tool not in {"codex", "claude", "workbuddy", "zcode", "opencode"}:
        raise MemoryProtocolError("HOOK_TOOL_INVALID", "Unsupported adapter tool.")
    config_path = Path(config_path).resolve()
    owner_path = (Path(ownership_path).resolve() if ownership_path is not None else
                  config_path.parent / f".project-memory-adapter-{tool}.json")
    if owner_path == config_path:
        raise MemoryProtocolError("CONFIG_INVALID", "Ownership and host configuration paths must differ.")
    owner_raw = _read(owner_path)
    owner = _object(owner_raw)
    if owner and (owner.get("tool") != tool or owner.get("config_path") != str(config_path)):
        raise MemoryProtocolError("HOOK_OWNERSHIP_INVALID", "Ownership record belongs to another adapter target.")
    invocation = driver()
    handlers = {}
    changes = []
    if tool == "opencode":
        target = config_path.parent / "plugins/project-memory-shared.js"
        if owner_path == target:
            raise MemoryProtocolError("CONFIG_INVALID", "Ownership and plugin paths must differ.")
        previous = _read(target)
        if previous is not None and not previous.startswith(_MARKER.encode()):
            raise MemoryProtocolError("HOOK_PLUGIN_NOT_OWNED", "Existing plugin is not managed by this installer.")
        template = resources.files("project_memory_bootstrap_codex").joinpath("resources/opencode-memory-plugin.js").read_text(encoding="utf-8")
        changes.append((target, previous, template.replace("__PROJECT_MEMORY_DRIVER__", json.dumps(invocation)).encode()))
    else:
        if tool == "zcode" and (project_root is None or Path(project_root).resolve() not in config_path.parents):
            raise MemoryProtocolError("HOOK_ENABLE_SCOPE_CHANGE", "ZCode configuration must be inside the supplied workspace.")
        original = _read(config_path)
        config = _object(original)
        if config.get("disableAllHooks") is True:
            raise MemoryProtocolError("HOOKS_DISABLED", "Host hooks are explicitly disabled.")
        hooks = config.setdefault("hooks", {})
        if not isinstance(hooks, dict):
            raise MemoryProtocolError("CONFIG_INVALID", "Host hooks must be an object.")
        if tool == "zcode":
            events = hooks.setdefault("events", {})
            if not isinstance(events, dict):
                raise MemoryProtocolError("CONFIG_INVALID", "Hook events must be an object.")
            handler = {"type": "process", "command": invocation[0],
                       "args": invocation[1:] + ["hook-memory", "--tool", tool]}
        else:
            events = hooks
            handler = {"type": "command", "command": shlex.join(invocation + ["hook-memory", "--tool", tool])}
        previous_handlers = owner.get("handlers", {})
        if not isinstance(previous_handlers, dict):
            raise MemoryProtocolError("HOOK_OWNERSHIP_INVALID", "Invalid adapter ownership handlers.")
        # Only exact current or recorded handlers are owned. Similar commands are unrelated.
        for event in ("SessionStart", "Stop"):
            groups = events.get(event, [])
            if not isinstance(groups, list):
                raise MemoryProtocolError("CONFIG_INVALID", "Hook event handlers must be arrays.")
            owned = [handler, previous_handlers.get(event)]
            kept = []
            for group in groups:
                if tool == "zcode" and group in owned:
                    # Upgrade exact direct handlers written by the previous installer.
                    continue
                if isinstance(group, dict) and "hooks" in group:
                    if not isinstance(group["hooks"], list):
                        raise MemoryProtocolError("CONFIG_INVALID", "Hook groups must contain handler arrays.")
                    remaining = [entry for entry in group["hooks"] if entry not in owned]
                    if remaining or len(group) > 1:
                        kept.append(dict(group, hooks=remaining))
                else:
                    kept.append(group)
            events[event] = kept
            handlers[event] = handler
        if tool == "zcode":
            unrelated_handlers = any(
                bool(group["hooks"]) if isinstance(group, dict) and "hooks" in group else bool(group)
                for groups in events.values() for group in groups
            )
            if hooks.get("enabled") is False and unrelated_handlers:
                raise MemoryProtocolError("HOOK_ENABLE_SCOPE_CHANGE", "Enabling hooks would activate existing workspace handlers.")
            hooks["enabled"] = True
        for event in ("SessionStart", "Stop"):
            events[event].append({"hooks": [handler]})
        changes.append((config_path, original, _json(config)))
    ownership = {"tool": tool, "config_path": str(config_path), "handlers": handlers,
                 "driver": invocation, "protocol_version": PROTOCOL_VERSION}
    changes.append((owner_path, owner_raw, _json(ownership)))
    for path, expected, _ in changes:
        if _read(path) != expected:
            raise MemoryProtocolError("CONFIG_CHANGED", "Configuration changed during adapter installation.")
    for path, expected, content in changes:
        _replace(path, expected, content)
    return {"tool": tool, "config_path": str(config_path), "ownership_path": str(owner_path),
            "status": "configured_not_runtime_verified"}
