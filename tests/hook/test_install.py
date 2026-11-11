"""`afr hook install`: project-level only, keeps other hooks, idempotent."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from flight_recorder.cli import app
from flight_recorder.hook.install import (
    ASYNC_EVENTS,
    MARKER,
    SYNC_EVENTS,
    InstallError,
    install,
    settings_file,
    uninstall,
)

THEIRS = {"type": "command", "command": "echo their own hook"}


def hooks_in(project: Path) -> dict[str, list[dict[str, object]]]:
    return json.loads(settings_file(project).read_text(encoding="utf-8"))["hooks"]


def test_install_adds_every_event_async_except_session_end(tmp_path: Path) -> None:
    path = install(tmp_path)

    assert path == tmp_path / ".claude" / "settings.local.json"
    hooks = hooks_in(tmp_path)
    assert set(hooks) == {*ASYNC_EVENTS, *SYNC_EVENTS}
    for event, [group] in hooks.items():
        [handler] = group["hooks"]  # pyright: ignore[reportGeneralTypeIssues]
        assert MARKER in handler["command"]  # pyright: ignore[reportIndexIssue]
        assert handler.get("async") is (True if event in ASYNC_EVENTS else None)  # pyright: ignore[reportAttributeAccessIssue]
    assert hooks["PreToolUse"][0]["matcher"] == "*"
    assert "matcher" not in hooks["Stop"][0]
    assert hooks["SessionEnd"][0]["hooks"][0]["timeout"] == 5  # pyright: ignore[reportIndexIssue, reportArgumentType, reportCallIssue]


def test_other_settings_and_hooks_are_kept_and_reinstall_does_not_duplicate(tmp_path: Path) -> None:
    path = settings_file(tmp_path)
    path.parent.mkdir()
    path.write_text(
        json.dumps({"model": "x", "hooks": {"Stop": [{"hooks": [THEIRS]}]}}), encoding="utf-8"
    )

    install(tmp_path)
    install(tmp_path)

    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["model"] == "x"
    assert data["hooks"]["Stop"][0] == {"hooks": [THEIRS]}
    assert len(data["hooks"]["Stop"]) == 2  # theirs, and ours once
    assert all(len(groups) == 1 for e, groups in data["hooks"].items() if e != "Stop")


def test_uninstall_removes_only_ours(tmp_path: Path) -> None:
    path = settings_file(tmp_path)
    path.parent.mkdir()
    path.write_text(json.dumps({"hooks": {"Stop": [{"hooks": [THEIRS]}]}}), encoding="utf-8")
    install(tmp_path)

    assert uninstall(tmp_path)
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "hooks": {"Stop": [{"hooks": [THEIRS]}]}
    }
    assert not uninstall(tmp_path)


def test_refuses_the_home_folder(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    with pytest.raises(InstallError, match="user-level"):
        install(tmp_path)


def test_cli_install_and_uninstall(tmp_path: Path) -> None:
    runner = CliRunner()

    installed = runner.invoke(app, ["hook", "install", "--project", str(tmp_path)])
    removed = runner.invoke(app, ["hook", "uninstall", "--project", str(tmp_path)])

    assert installed.exit_code == 0 and "settings.local.json" in installed.output
    assert removed.exit_code == 0 and "hooks removed" in removed.output


def test_uninstall_keeps_someone_elses_handler_in_a_shared_group(tmp_path: Path) -> None:
    install(tmp_path)
    path = settings_file(tmp_path)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["hooks"]["Stop"][0]["hooks"].append(THEIRS)  # another tool joined our group
    path.write_text(json.dumps(data), encoding="utf-8")

    uninstall(tmp_path)

    assert json.loads(path.read_text(encoding="utf-8")) == {
        "hooks": {"Stop": [{"hooks": [THEIRS]}]}
    }


def test_the_hook_runs_python_isolated() -> None:
    """-I: a json.py or hmac.py in the project folder must not replace the standard module."""
    from flight_recorder.hook.install import hook_command

    assert " -I -m flight_recorder.hook" in hook_command()
