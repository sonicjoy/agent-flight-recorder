"""`afr hook install`: add the recorder's hooks to one project, never to user-level settings.

They go in the project's .claude/settings.local.json: project-level, personal and not
committed (the command holds this machine's path to Python). Other hooks in that file are
kept; running install again replaces only ours, and uninstall removes only ours.

Every event but SessionEnd is async: Claude Code doesn't wait. SessionEnd runs
synchronously, once, at exit (with a short send timeout): under `claude -p`, async hooks
still running at exit are cancelled, and SessionEnd also picks up any token usage a
cancelled Stop missed. Nothing blocks: the hook always exits 0.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, cast

MARKER = "flight_recorder.hook"
ASYNC_EVENTS = (
    "SessionStart",
    "UserPromptSubmit",
    "PreToolUse",
    "PostToolUse",
    "PostToolUseFailure",
    "SubagentStart",
    "SubagentStop",
    "Stop",
    "StopFailure",
)
SYNC_EVENTS = ("SessionEnd",)
SYNC_TIMEOUT_SECONDS = 5
TOOL_EVENTS = {"PreToolUse", "PostToolUse", "PostToolUseFailure"}


class InstallError(ValueError):
    pass


def hook_command() -> str:
    """This install's Python, so the hook works from any project's folder.

    -I (isolated): Claude Code runs hooks in the project folder, and without it `-m` would
    put that folder first on the import path, so a json.py or hmac.py there would run, as
    you, on every event, outside Claude Code's approvals. -I also ignores PYTHON* variables.
    """
    return f'"{Path(sys.executable).as_posix()}" -I -m {MARKER}'


def settings_file(project: Path) -> Path:
    return project / ".claude" / "settings.local.json"


def _ours(handler: Any) -> bool:
    return isinstance(handler, dict) and MARKER in str(
        cast("dict[str, Any]", handler).get("command", "")
    )


def _load(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8") or "{}")
    if not isinstance(data, dict):
        raise InstallError(f"{path} isn't a JSON object")
    return cast("dict[str, Any]", data)


def _without_ours(data: dict[str, Any]) -> dict[str, Any]:
    """Remove our handlers only: a group shared with someone else's handler keeps theirs."""
    hooks = cast("dict[str, Any]", data.get("hooks") or {})
    kept: dict[str, Any] = {}
    for event, groups in hooks.items():
        remaining: list[Any] = []
        for group in cast("list[Any]", groups):
            handlers = (
                cast("dict[str, Any]", group).get("hooks") if isinstance(group, dict) else None
            )
            if not isinstance(handlers, list):
                remaining.append(group)
                continue
            others = [h for h in cast("list[Any]", handlers) if not _ours(h)]
            if others:
                remaining.append(cast("dict[str, Any]", group) | {"hooks": others})
        if remaining:
            kept[event] = remaining
    result = {k: v for k, v in data.items() if k != "hooks"}
    if kept:
        result["hooks"] = kept
    return result


def install(project: Path, command: str | None = None) -> Path:
    project = project.resolve()
    if project == Path.home().resolve():
        raise InstallError(
            "refusing to install in your home folder: that would be user-level settings, "
            "which record every Claude Code session on this machine. Pick a project folder."
        )
    if not project.is_dir():
        raise InstallError(f"{project} isn't a folder")
    path = settings_file(project)
    data = _without_ours(_load(path))
    hooks = cast("dict[str, Any]", data.setdefault("hooks", {}))
    command = command or hook_command()
    for event in (*ASYNC_EVENTS, *SYNC_EVENTS):
        handler: dict[str, Any] = {"type": "command", "command": command}
        handler |= {"timeout": SYNC_TIMEOUT_SECONDS} if event in SYNC_EVENTS else {"async": True}
        group: dict[str, Any] = {"hooks": [handler]}
        if event in TOOL_EVENTS:
            group = {"matcher": "*"} | group
        cast("list[Any]", hooks.setdefault(event, [])).append(group)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return path


def uninstall(project: Path) -> bool:
    """Remove our hooks. Returns whether there were any."""
    path = settings_file(project.resolve())
    data = _load(path)
    cleaned = _without_ours(data)
    if cleaned == data:
        return False
    path.write_text(json.dumps(cleaned, indent=2) + "\n", encoding="utf-8")
    return True
