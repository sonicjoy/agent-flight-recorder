"""`afr analyse` end to end on a throwaway store, with a fake model (#22, docs/analyst.md)."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable, Generator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from typer.testing import CliRunner

import flight_recorder.analyst.judgment as judgment
import flight_recorder.cli.analyse as analyse_cli
from flight_recorder.cli import app
from flight_recorder.redact import Redactor
from flight_recorder.store.db import upgrade
from flight_recorder.store.write import RunIn, SpanIn, record_run

pytestmark = pytest.mark.db

runner = CliRunner()
REDACTOR = Redactor(b"test-key-0123456789abcdef0123456789abcdef")
T0 = datetime(2026, 11, 12, 9, 0, tzinfo=UTC)
EXTRA = {"t.decided.labels": ["evals", "infra"], "t.expected.labels": ["evals"],
         "t.grade": "wrong"}  # fmt: skip


def _record(session: Session, external_id: str, *, agent: str = "tri",
            status: Literal["ok", "error"] = "ok", attributes: dict[str, Any] | None = None,
            asked: str | None = "Triage: an issue", minute: int = 0) -> uuid.UUID:  # fmt: skip
    at = T0 + timedelta(minutes=minute)
    span = SpanIn(external_id=f"root-{external_id}", kind="agent", name="agent", started_at=at,
                  ended_at=at + timedelta(seconds=5), status=status, input=asked,
                  error="TimeoutError: 30s" if status == "error" else None,
                  attributes=attributes or {})  # fmt: skip
    return record_run(
        session,
        RunIn(
            source="otel",
            external_id=external_id,
            agent=agent,
            name=external_id,
            status=status,
            started_at=at,
            ended_at=at + timedelta(seconds=5),
            attributes=attributes or {},
        ),
        [span],
        redactor=REDACTOR,
    )


@pytest.fixture
def store(make_database: Callable[[], str]) -> Generator[tuple[str, dict[str, uuid.UUID]]]:
    url = make_database()
    upgrade(url)
    engine = create_engine(url)
    with Session(engine) as session, session.begin():
        ids = {
            "extra1": _record(session, "extra1", attributes=EXTRA, minute=1),
            "extra2": _record(session, "extra2", attributes=EXTRA, minute=2),
            "errored": _record(session, "errored", status="error", minute=3),
            "silent": _record(session, "silent", attributes=EXTRA, asked=None, minute=4),
        }
    yield url, ids
    engine.dispose()


class Fake:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, spec: str) -> Fake:
        return self

    def with_structured_output(self, schema: Any) -> Fake:
        self.schema = schema
        return self

    def invoke(self, messages: Any) -> Any:
        self.calls += 1
        shown = messages[-1].content.count("<item ")
        return self.schema(name="Extra Label", cause="It adds labels.",
                           representatives=[f"e{n}" for n in range(1, shown + 1)])  # fmt: skip


@pytest.fixture
def cli(
    store: tuple[str, dict[str, uuid.UUID]],
    clean_env: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Callable[..., Any]:
    clean_env.chdir(tmp_path)
    clean_env.setenv("AFR_DATABASE_URL", store[0])
    clean_env.setenv("AFR_HOME", str(tmp_path / "home"))
    clean_env.setenv("OPENAI_API_KEY", "test-key-not-real")
    clean_env.setenv("COLUMNS", "160")
    clean_env.setattr(analyse_cli, "interactive", lambda: True)
    clean_env.setattr(judgment, "chat_model", Fake())

    def invoke(*args: str, input: str = "\n") -> Any:
        return runner.invoke(app, ["analyse", "--agent", "tri", "--attr", "t.grade=wrong",
                                   "--model", "openai:fake", "--suite", "found", *args],
                             input=input)  # fmt: skip

    return invoke


def _cases(tmp_path: Path) -> list[Path]:
    folder = tmp_path / "evals" / "cases" / "found"
    return sorted(folder.glob("*.yaml")) if folder.exists() else []


def _log(tmp_path: Path) -> dict[str, Any]:
    [path] = (tmp_path / "evals" / "analyst-log").glob("*.json")
    return json.loads(path.read_text(encoding="utf-8"))


def test_kept_groups_become_cases_and_the_log_records_them(
    cli: Callable[..., Any], tmp_path: Path, store: Any
) -> None:
    result = cli(input="1\n")

    assert result.exit_code == 0, result.output
    assert "extra-label" in result.output and "labels:extra" in result.output
    written = _cases(tmp_path)
    wanted = sorted(f"run-{str(store[1][k])[:8]}" for k in ("extra1", "extra2"))
    assert [p.stem for p in written] == wanted
    text = written[0].read_text(encoding="utf-8")
    assert "labels:extra" in text and "extra-label" in text  # the group's tags
    assert "skipped run" in result.output  # the run with no recorded input
    log = _log(tmp_path)
    assert [g["kept"] for g in log["groups"]] == [True, False]
    for entry in log["written"]:
        assert hashlib.sha256(Path(entry["path"]).read_bytes()).hexdigest() == entry["sha256"]


def test_keeping_nothing_writes_no_case_but_still_logs(
    cli: Callable[..., Any], tmp_path: Path
) -> None:
    result = cli(input="\n")

    assert result.exit_code == 0, result.output
    assert _cases(tmp_path) == []
    assert all(not g["kept"] for g in _log(tmp_path)["groups"])


def test_a_bad_answer_is_asked_again(cli: Callable[..., Any], tmp_path: Path) -> None:
    result = cli(input="9\nx\n2\n")

    assert result.exit_code == 0, result.output
    assert result.output.count("give group numbers from 1 to 2") == 2
    assert [g["kept"] for g in _log(tmp_path)["groups"]] == [False, True]


def test_a_rerun_skips_runs_that_already_have_a_case(
    cli: Callable[..., Any], tmp_path: Path
) -> None:
    cli(input="1\n")
    again = cli(input="1\n")

    assert again.exit_code == 0
    assert "a case for this run exists" in again.output
    assert len(_cases(tmp_path)) == 2


def test_no_failed_runs_exits_1(cli: Callable[..., Any]) -> None:
    result = runner.invoke(app, ["analyse", "--agent", "nobody", "--model", "openai:fake"])

    assert result.exit_code == 1
    assert "no failed runs" in result.output


def test_without_a_terminal_it_refuses_before_any_model_call(
    cli: Callable[..., Any], clean_env: pytest.MonkeyPatch
) -> None:
    clean_env.setattr(analyse_cli, "interactive", lambda: False)
    fake = Fake()
    clean_env.setattr(judgment, "chat_model", fake)

    result = cli()

    assert result.exit_code == 2
    assert "run it in a terminal" in result.output
    assert fake.calls == 0


def test_a_bad_model_spec_or_a_missing_key_exits_2(
    cli: Callable[..., Any], clean_env: pytest.MonkeyPatch
) -> None:
    bad_spec = runner.invoke(app, ["analyse", "--agent", "tri", "--model", "nonsense"])
    clean_env.delenv("OPENAI_API_KEY")
    no_key = runner.invoke(app, ["analyse", "--agent", "tri", "--model", "openai:fake"])

    assert bad_spec.exit_code == 2
    assert no_key.exit_code == 2 and "OPENAI_API_KEY" in no_key.output


def test_a_write_the_os_refuses_exits_1_and_the_log_keeps_what_was_written(
    cli: Callable[..., Any], tmp_path: Path
) -> None:
    (tmp_path / "evals" / "cases").mkdir(parents=True)
    (tmp_path / "evals" / "cases" / "found").write_text("a file where the folder goes")

    result = cli(input="1\n")

    assert result.exit_code == 1
    assert "can't write" in result.output
    assert _log(tmp_path)["written"] == []
