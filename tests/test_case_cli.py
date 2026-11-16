"""`afr case new` and the right answer `afr label` asks for (#59).

Written from issue #59 before the code.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Generator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import pytest
import yaml
from sqlalchemy import create_engine, delete, func, select, update
from sqlalchemy.orm import Session
from typer.testing import CliRunner

from flight_recorder.cli import app
from flight_recorder.redact import Redactor
from flight_recorder.store.db import upgrade
from flight_recorder.store.models import Label
from flight_recorder.store.write import RunIn, SpanIn, record_run

pytestmark = pytest.mark.db

runner = CliRunner()
REDACTOR = Redactor(b"test-key-0123456789abcdef0123456789abcdef")
T0 = datetime(2026, 11, 1, 9, 0, tzinfo=UTC)
SECRET = "sk-ant-" + "q" * 30
TRIAGER = {
    "triager.decided.labels": ["evals", "infra"],
    "triager.expected.labels": ["evals"],
    "triager.decided.milestone": "Week 3",
    "triager.expected.milestone": "Week 3",
    "triager.grade": "wrong",
}


def _record(
    session: Session,
    external_id: str,
    *,
    source: Literal["otel", "claude_code"] = "otel",
    attributes: dict[str, Any] | None = None,
    asked: Any = None,
    answered: Any = None,
) -> uuid.UUID:
    span = SpanIn(
        external_id="session" if source == "claude_code" else f"root-{external_id}",
        kind="agent",
        name="Claude Code session" if source == "claude_code" else "agent",
        started_at=T0,
        ended_at=T0 + timedelta(seconds=5),
        status="ok",
        input=asked,
        output=answered,
        attributes=attributes or {},
    )
    return record_run(
        session,
        RunIn(
            source=source,
            external_id=external_id,
            agent="claude-code" if source == "claude_code" else "agent",
            name=external_id,
            status="ok",
            started_at=T0,
            ended_at=T0 + timedelta(seconds=5),
            attributes=attributes or {},
        ),
        [span],
        redactor=REDACTOR,
    )


@pytest.fixture(scope="module")
def store(make_database: Callable[[], str]) -> Generator[tuple[str, dict[str, uuid.UUID]]]:
    url = make_database()
    upgrade(url)
    engine = create_engine(url)
    with Session(engine) as session, session.begin():
        ids = {
            "triager": _record(session, "trace-tri", attributes=TRIAGER,
                               asked="Triage this issue.\n\nTitle: flaky eval"),
            "plain": _record(session, "trace-plain", asked={"question": "sum 2 and 2"},
                             answered="5"),
            "secret": _record(session, "trace-secret", asked=f"deploy with {SECRET}",
                              answered="done"),
            "silent": _record(session, "trace-silent"),
            "claude": _record(session, "cc-session-1", source="claude_code",
                              asked="fix the tests", answered="fixed"),
        }  # fmt: skip
    yield url, ids
    engine.dispose()


@pytest.fixture
def db(store: tuple[str, dict[str, uuid.UUID]]) -> Generator[Session]:
    engine = create_engine(store[0])
    with Session(engine) as session:
        session.execute(delete(Label))
        session.commit()
        yield session
    engine.dispose()


@pytest.fixture
def ids(store: tuple[str, dict[str, uuid.UUID]], db: Session) -> dict[str, uuid.UUID]:
    return store[1]


@pytest.fixture
def cases(tmp_path: Path) -> Path:
    return tmp_path / "evals" / "cases"


@pytest.fixture
def cli(
    store: tuple[str, dict[str, uuid.UUID]],
    db: Session,
    clean_env: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Callable[..., Any]:
    clean_env.setenv("AFR_DATABASE_URL", store[0])
    clean_env.setenv("AFR_HOME", str(tmp_path / "home"))  # a fresh redaction key
    clean_env.setenv("COLUMNS", "160")

    def invoke(*args: str, input: str | None = None) -> Any:
        return runner.invoke(app, list(args), input=input)

    return invoke


def _labels(db: Session) -> list[Label]:
    db.expire_all()
    return list(db.scalars(select(Label).order_by(Label.created_at, Label.id)))


def _written(cases: Path) -> list[Path]:
    return sorted(cases.rglob("*.yaml")) if cases.exists() else []


# --- afr label asks for the right answer ------------------------------------------------


def test_label_asks_once_for_the_right_answer_field_by_field(
    cli: Callable[..., Any], db: Session, ids: dict[str, uuid.UUID]
) -> None:
    result = cli(
        "label", str(ids["triager"]),
        input="extra-label\n\nwrong-label\n\n\nlabels=evals\n",
    )  # fmt: skip

    assert result.exit_code == 0, result.output
    assert "right answer" in result.output
    rows = _labels(db)
    assert [r.failure_mode for r in rows] == ["extra-label", "wrong-label"]
    # on the first label only, the agent's other fields kept as decided
    assert rows[0].expected == {"fields": {"labels": ["evals"], "milestone": "Week 3"}}
    assert rows[1].expected is None


def test_label_refuses_an_unknown_field_and_asks_again(
    cli: Callable[..., Any], db: Session, ids: dict[str, uuid.UUID]
) -> None:
    result = cli("label", str(ids["triager"]),
                 input="extra-label\n\n\npriority=high\nmilestone=Week 4\n")  # fmt: skip

    assert result.exit_code == 0, result.output
    assert "priority" in result.output
    assert _labels(db)[0].expected == {
        "fields": {"labels": ["evals", "infra"], "milestone": "Week 4"}
    }


def test_label_takes_free_text_for_a_run_with_no_decided_fields_redacted(
    cli: Callable[..., Any], db: Session, ids: dict[str, uuid.UUID]
) -> None:
    result = cli("label", str(ids["plain"]), input=f"bad-sum\n\n\n4, using key {SECRET}\n")

    assert result.exit_code == 0, result.output
    answer = _labels(db)[0].expected
    assert answer is not None
    assert answer["text"].startswith("4, using key [REDACTED:anthropic_key:")
    assert SECRET not in str(answer)


def test_an_empty_answer_or_end_of_input_stores_none(
    cli: Callable[..., Any], db: Session, ids: dict[str, uuid.UUID]
) -> None:
    assert cli("label", str(ids["plain"]), input="bad-sum\n\n\n").exit_code == 0
    assert cli("label", str(ids["silent"]), input="no-output\n\n\n").exit_code == 0

    assert [r.expected for r in _labels(db)] == [None, None]


def test_label_with_mode_takes_answer_from_a_flag_and_never_prompts(
    cli: Callable[..., Any], db: Session, ids: dict[str, uuid.UUID]
) -> None:
    result = cli("label", str(ids["triager"]), "--mode", "extra-label",
                 "--answer", "labels=evals; milestone=Week 3")  # fmt: skip
    plain = cli("label", str(ids["plain"]), "--mode", "bad-sum")

    assert result.exit_code == 0, result.output
    assert plain.exit_code == 0, plain.output
    assert "right answer" not in plain.output
    rows = _labels(db)
    assert rows[0].expected == {"fields": {"labels": ["evals"], "milestone": "Week 3"}}
    assert rows[1].expected is None


def test_answer_needs_mode(cli: Callable[..., Any], ids: dict[str, uuid.UUID]) -> None:
    result = cli("label", str(ids["plain"]), "--answer", "4", input="m\n\n")

    assert result.exit_code == 2


def test_the_label_screen_shows_a_stored_answer(
    cli: Callable[..., Any], ids: dict[str, uuid.UUID]
) -> None:
    cli("label", str(ids["plain"]), "--mode", "bad-sum", "--answer", "four")

    result = cli("label", str(ids["plain"]), input="q\n")

    assert "right answer: four" in result.output


# --- drafting a case (the library the analyst reuses) -----------------------------------


def test_a_case_from_a_labelled_triager_run_expects_the_right_answer(
    db: Session, ids: dict[str, uuid.UUID], cli: Callable[..., Any]
) -> None:
    from flight_recorder.evals.cases import draft_case

    cli("label", str(ids["triager"]), "--mode", "extra-label", "--answer", "labels=evals")
    cli("label", str(ids["triager"]), "--mode", "wrong-label")

    draft = draft_case(db, ids["triager"], suite="smoke", redactor=REDACTOR)

    case = draft.case
    assert draft.local is False
    assert case["id"] == f"run-{str(ids['triager'])[:8]}"
    assert case["from_run"] == "trace-tri"
    assert case["tags"] == ["extra-label", "wrong-label"]
    assert case["input"] == {"prompt": "Triage this issue.\n\nTitle: flaky eval"}
    assert case["expected"] == {"labels": ["evals"], "milestone": "Week 3"}
    assert "unreviewed" not in case
    assert case["checks"] == [
        {"type": "equals", "field": "labels", "value": ["evals"]},
        {"type": "equals", "field": "milestone", "value": "Week 3"},
    ]


def test_a_case_from_an_unlabelled_run_expects_the_agents_output_marked_unreviewed(
    db: Session, ids: dict[str, uuid.UUID]
) -> None:
    from flight_recorder.evals.cases import draft_case

    triager = draft_case(db, ids["triager"], suite="smoke", redactor=REDACTOR).case
    plain = draft_case(db, ids["plain"], suite="smoke", redactor=REDACTOR).case

    assert triager["expected"] == {"labels": ["evals", "infra"], "milestone": "Week 3"}
    assert triager["unreviewed"] is True
    assert plain["input"] == {"question": "sum 2 and 2"}  # a dict input as recorded
    assert plain["expected"] == {"answer": "5"}
    assert plain["unreviewed"] is True
    assert plain["tags"] == []


def test_a_free_text_answer_becomes_the_expected_answer(
    db: Session, ids: dict[str, uuid.UUID], cli: Callable[..., Any]
) -> None:
    from flight_recorder.evals.cases import draft_case

    cli("label", str(ids["plain"]), "--mode", "bad-sum", "--answer", "4")

    case = draft_case(db, ids["plain"], suite="smoke", redactor=REDACTOR).case

    assert case["expected"] == {"answer": "4"}
    assert case["checks"] == [{"type": "equals", "field": "answer", "value": "4"}]
    assert "unreviewed" not in case


def test_a_claude_code_session_is_routed_to_the_local_folder(
    db: Session, ids: dict[str, uuid.UUID]
) -> None:
    from flight_recorder.evals.cases import draft_case

    draft = draft_case(db, ids["claude"], suite="smoke", redactor=REDACTOR)

    assert draft.local is True
    assert draft.case["input"] == {"prompt": "fix the tests"}


def test_a_run_with_no_recorded_input_has_no_case(db: Session, ids: dict[str, uuid.UUID]) -> None:
    from flight_recorder.evals.cases import CaseError, draft_case

    with pytest.raises(CaseError, match="input"):
        draft_case(db, ids["silent"], suite="smoke", redactor=REDACTOR)


def test_drafting_checks_the_ids_before_reading_the_run(
    db: Session, ids: dict[str, uuid.UUID]
) -> None:
    from flight_recorder.evals.cases import CaseError, draft_case

    for suite, case_id in [("../x", None), ("local", None), ("smoke", "/abs"), ("smoke", "..")]:
        with pytest.raises(CaseError):
            draft_case(db, ids["plain"], suite=suite, case_id=case_id, redactor=REDACTOR)


# --- afr case new -----------------------------------------------------------------------


def test_case_new_prints_the_case_asks_and_writes_it(
    cli: Callable[..., Any], ids: dict[str, uuid.UUID], cases: Path
) -> None:
    result = cli("case", "new", "--from-run", str(ids["plain"]), "--suite", "smoke",
                 "--root", str(cases), "--id", "sum-two", input="y\n")  # fmt: skip

    assert result.exit_code == 0, result.output
    assert "sum 2 and 2" in result.output  # the whole case, printed
    assert "not everything personal" in result.output  # says what redaction misses
    path = cases / "smoke" / "sum-two.yaml"
    assert _written(cases) == [path]
    assert yaml.safe_load(path.read_text(encoding="utf-8"))["id"] == "sum-two"


def test_case_new_writes_nothing_without_a_yes(
    cli: Callable[..., Any], ids: dict[str, uuid.UUID], cases: Path
) -> None:
    no = cli("case", "new", "--from-run", str(ids["plain"]), "--suite", "smoke",
             "--root", str(cases), input="n\n")  # fmt: skip
    eof = cli("case", "new", "--from-run", str(ids["plain"]), "--suite", "smoke",
              "--root", str(cases), input="")  # fmt: skip

    assert no.exit_code == 1
    assert eof.exit_code == 1
    assert _written(cases) == []


def test_case_new_yes_skips_the_question(
    cli: Callable[..., Any], ids: dict[str, uuid.UUID], cases: Path
) -> None:
    result = cli("case", "new", "--from-run", str(ids["plain"]), "--suite", "smoke",
                 "--root", str(cases), "--yes")  # fmt: skip

    assert result.exit_code == 0, result.output
    assert len(_written(cases)) == 1


def test_case_new_refuses_placeholders_until_faked_or_kept(
    cli: Callable[..., Any], ids: dict[str, uuid.UUID], cases: Path
) -> None:
    from flight_recorder.evals.cases import FAKES

    args = ("case", "new", "--from-run", str(ids["secret"]), "--suite", "smoke",
            "--root", str(cases), "--yes")  # fmt: skip

    refused = cli(*args)
    faked = cli(*args, "--fake", "--id", "faked")
    kept = cli(*args, "--keep-placeholders", "--id", "kept")

    assert refused.exit_code == 1
    assert "[REDACTED:anthropic_key:" in refused.output
    assert "--fake" in refused.output
    assert faked.exit_code == 0, faked.output
    assert kept.exit_code == 0, kept.output
    assert FAKES["anthropic_key"] in (cases / "smoke" / "faked.yaml").read_text(encoding="utf-8")
    kept_case = yaml.safe_load((cases / "smoke" / "kept.yaml").read_text(encoding="utf-8"))
    assert kept_case["placeholders"] is True
    assert kept_case["input"]["prompt"] == "deploy with [REDACTED:anthropic_key]"
    assert SECRET not in "".join(p.read_text(encoding="utf-8") for p in _written(cases))


def test_fake_and_keep_together_are_refused(
    cli: Callable[..., Any], ids: dict[str, uuid.UUID], cases: Path
) -> None:
    result = cli("case", "new", "--from-run", str(ids["secret"]), "--suite", "smoke",
                 "--root", str(cases), "--yes", "--fake", "--keep-placeholders")  # fmt: skip

    assert result.exit_code == 2


def test_case_new_puts_a_claude_code_session_in_the_ignored_local_folder(
    cli: Callable[..., Any], ids: dict[str, uuid.UUID], cases: Path
) -> None:
    result = cli("case", "new", "--from-run", str(ids["claude"]), "--suite", "smoke",
                 "--root", str(cases), "--yes")  # fmt: skip

    assert result.exit_code == 0, result.output
    assert [p.relative_to(cases).parts[:2] for p in _written(cases)] == [("local", "smoke")]
    assert "local" in result.output


def test_case_new_refuses_bad_ids_and_never_overwrites(
    cli: Callable[..., Any], ids: dict[str, uuid.UUID], cases: Path
) -> None:
    base = ("case", "new", "--from-run", str(ids["plain"]), "--root", str(cases), "--yes")

    escape = cli(*base, "--suite", "../up")
    reserved = cli(*base, "--suite", "local")
    first = cli(*base, "--suite", "smoke", "--id", "one")
    again = cli(*base, "--suite", "smoke", "--id", "one")

    assert escape.exit_code == 2
    assert reserved.exit_code == 2
    assert first.exit_code == 0, first.output
    assert again.exit_code == 1
    assert "exists" in again.output
    assert _written(cases) == [cases / "smoke" / "one.yaml"]


def test_case_new_prints_recorded_text_cleaned(
    cli: Callable[..., Any], db: Session, cases: Path
) -> None:
    run = _record(
        db, "trace-esc", asked=f"hi {chr(0x202E)}evil"
    )  # YAML leaves it raw; clean() can't
    db.commit()

    result = cli("case", "new", "--from-run", str(run), "--suite", "smoke",
                 "--root", str(cases), input="n\n")  # fmt: skip

    assert "\u202e" not in result.output
    assert "\\u202e" in result.output


def test_the_repo_ignores_local_cases() -> None:
    ignore = Path(__file__).parents[1] / ".gitignore"

    assert "evals/cases/local/" in ignore.read_text(encoding="utf-8").splitlines()


def test_expected_fields_keep_their_name_order_whatever_order_the_store_keeps(
    db: Session, cli: Callable[..., Any]
) -> None:
    from flight_recorder.evals.cases import draft_case

    wide = {f"agent.decided.{f}": "x" for f in ("labels", "milestone", "duplicate_of")}
    run = _record(db, "trace-wide", attributes=wide, asked="go")
    db.commit()
    cli("label", str(run), "--mode", "m", "--answer", "milestone=y")  # JSONB sorts by length

    case = draft_case(db, run, suite="smoke", redactor=REDACTOR).case

    assert list(case["expected"]) == ["duplicate_of", "labels", "milestone"]
    assert [c["field"] for c in case["checks"]] == ["duplicate_of", "labels", "milestone"]


# --- review fixes (.review/59-afr-case) -----------------------------------------------


def test_a_secret_the_recording_missed_is_redacted_when_the_case_is_drafted(
    db: Session, ids: dict[str, uuid.UUID]
) -> None:
    from flight_recorder.evals.cases import draft_case, placeholders
    from flight_recorder.store.models import Span

    run = _record(db, "trace-old", asked="placeholder")
    db.flush()
    # as a run recorded before a pattern existed holds it: the stored text, unredacted
    db.execute(update(Span).where(Span.run_id == run).values(input=f"use {SECRET} please"))
    db.commit()

    case = draft_case(db, run, suite="smoke", redactor=REDACTOR).case

    assert SECRET not in str(case)
    assert placeholders(case)  # so the gate stops the write


def test_a_right_answer_survives_merging_and_removing_its_label(
    cli: Callable[..., Any], db: Session, ids: dict[str, uuid.UUID]
) -> None:
    from flight_recorder.store.labels import answer_of

    run = str(ids["plain"])
    cli("label", run, "--mode", "first", "--answer", "4")
    cli("label", run, "--mode", "second")
    cli("label", run, "--mode", "third")

    assert cli("labels", "rename", "first", "second").exit_code == 0  # merged into second
    db.expire_all()
    assert answer_of(db, ids["plain"]) == {"text": "4"}
    assert cli("labels", "remove", run, "--mode", "second").exit_code == 0
    db.expire_all()
    assert answer_of(db, ids["plain"]) == {"text": "4"}  # moved to third
    assert cli("labels", "remove", run).exit_code == 0
    db.expire_all()
    assert answer_of(db, ids["plain"]) is None  # all gone: so is the answer


def test_answer_with_a_mode_the_run_has_is_stored_on_that_label(
    cli: Callable[..., Any], db: Session, ids: dict[str, uuid.UUID]
) -> None:
    cli("label", str(ids["plain"]), "--mode", "bad-sum")

    result = cli("label", str(ids["plain"]), "--mode", "Bad Sum", "--answer", "4")

    assert result.exit_code == 0, result.output
    assert "storing the right answer" in result.output
    assert [r.expected for r in _labels(db)] == [{"text": "4"}]


def test_typed_values_take_the_type_of_the_agents_value(
    cli: Callable[..., Any], db: Session
) -> None:
    attrs = {"t.decided.priority": 2, "t.decided.urgent": False, "t.decided.ids": [1, 2],
             "t.decided.result.label": "bug"}  # fmt: skip
    run = _record(db, "trace-typed", attributes=attrs, asked="go")
    db.commit()

    ok = cli("label", str(run), "--mode", "m",
             "--answer", "priority=3; urgent=true; ids=1, 3; result.label=feature")  # fmt: skip
    bad = cli("label", str(run), "--mode", "n", "--answer", "priority=high")

    assert ok.exit_code == 0, ok.output
    assert bad.exit_code == 2
    assert _labels(db)[0].expected == {
        "fields": {"ids": [1, 3], "priority": 3, "result.label": "feature", "urgent": True}
    }


def test_an_answer_error_escapes_recorded_field_names(cli: Callable[..., Any], db: Session) -> None:
    run = _record(db, "trace-esc-field", attributes={"t.decided.\x1b[31mred": "x"}, asked="go")
    db.commit()

    result = cli("label", str(run), "--mode", "m", "--answer", "bogus=1")

    assert result.exit_code == 2
    # Rich colours its own error box when CI forces colour: look for the recorded name only
    assert "\x1b[31mred" not in result.output
    assert "\\x1b[31mred" in result.output


def test_a_langchain_runs_unreviewed_answer_is_its_raw_structured_output(
    db: Session,
) -> None:
    from flight_recorder.evals.cases import draft_case

    def llm(n: int, output: Any) -> SpanIn:
        start = T0 + timedelta(seconds=n)
        return SpanIn(external_id=f"llm-{n}", parent_external_id="root-lc", kind="llm",
                      name="ChatModel", started_at=start, ended_at=start, status="ok",
                      output=output)  # fmt: skip

    def message(**kwargs: Any) -> dict[str, Any]:
        return {"generations": [[{"message": {"kwargs": kwargs}}]]}

    def run(external_id: str, spans: list[SpanIn]) -> uuid.UUID:
        recorded = RunIn(source="otel", external_id=external_id, agent="lc", started_at=T0)
        return record_run(db, recorded, spans, redactor=REDACTOR)

    root = SpanIn(
        external_id="root-lc",
        kind="agent",
        name="LangGraph",
        started_at=T0,
        ended_at=T0,
        status="ok",
        input="classify this",
    )  # records no output
    long_text = "Line one.\n\nLine two with   spaces. " + "word " * 120
    decision = {"name": "Decision", "args": {"label": "bug", "notes": "a\nb", "n": 3}}
    spoke = llm(1, message(content=long_text))
    structured = run("trace-lc", [root, spoke, llm(2, message(content="", tool_calls=[decision]))])
    said = run("trace-lc2", [root, spoke])
    db.commit()

    first = draft_case(db, structured, suite="smoke", redactor=REDACTOR).case
    second = draft_case(db, said, suite="smoke", redactor=REDACTOR).case

    assert first["expected"] == {"label": "bug", "n": 3, "notes": "a\nb"}
    assert second["expected"] == {"answer": long_text}  # in full, not a summary's 400 chars


def test_case_new_writes_the_local_gitignore(
    cli: Callable[..., Any], ids: dict[str, uuid.UUID], cases: Path
) -> None:
    result = cli("case", "new", "--from-run", str(ids["claude"]), "--suite", "smoke",
                 "--root", str(cases), "--yes")  # fmt: skip

    assert result.exit_code == 0, result.output
    assert (cases / "local" / ".gitignore").read_text(encoding="utf-8").endswith("*\n")


# --- review fixes, round 2 ----------------------------------------------------------------


def test_a_run_keeps_one_right_answer_the_last_one_given(
    cli: Callable[..., Any], db: Session, ids: dict[str, uuid.UUID]
) -> None:
    from flight_recorder.store.labels import answer_of

    run = str(ids["plain"])
    cli("label", run, "--mode", "first", "--answer", "old")
    cli("label", run, "--mode", "second", "--answer", "new")
    cli("label", run, "--mode", "first", "--answer", "newest")  # on the older label

    db.expire_all()
    assert answer_of(db, ids["plain"]) == {"text": "newest"}
    assert [r.expected for r in _labels(db)] == [{"text": "newest"}, None]
    assert cli("labels", "remove", run, "--mode", "first").exit_code == 0
    db.expire_all()
    assert answer_of(db, ids["plain"]) == {"text": "newest"}  # moved, not the old one back


def test_non_finite_numbers_are_refused(cli: Callable[..., Any], db: Session) -> None:
    run = _record(db, "trace-float", attributes={"t.decided.score": 0.5}, asked="go")
    db.commit()

    for value in ("nan", "inf", "-inf"):
        result = cli("label", str(run), "--mode", "m", "--answer", f"score={value}")
        assert result.exit_code == 2, (value, result.output)


def test_the_unreviewed_answer_is_the_newest_model_calls_by_time(db: Session) -> None:
    from flight_recorder.evals.cases import draft_case

    def llm(name: str, second: int, parent: str, output: Any) -> SpanIn:
        start = T0 + timedelta(seconds=second)
        return SpanIn(external_id=name, parent_external_id=parent, kind="llm", name="Chat",
                      started_at=start, ended_at=start, status="ok", output=output)  # fmt: skip

    def message(**kwargs: Any) -> dict[str, Any]:
        return {"generations": [[{"message": {"kwargs": kwargs}}]]}

    root = SpanIn(external_id="root-t", kind="agent", name="graph", started_at=T0,
                  ended_at=T0, status="ok", input="weather?")  # fmt: skip
    branch = SpanIn(external_id="branch", parent_external_id="root-t", kind="step",
                    name="branch", started_at=T0, ended_at=T0, status="ok")  # fmt: skip
    spans = [
        root,
        branch,
        # in tree order the branch's call comes last, but it's the oldest
        llm("late", 5, "root-t", message(content="It is sunny in Paris.")),
        llm("early", 1, "branch", message(content="", tool_calls=[
            {"name": "lookup", "args": {"city": "Paris"}}])),
    ]  # fmt: skip
    recorded = RunIn(source="otel", external_id="trace-time", agent="lc", started_at=T0)
    run = record_run(db, recorded, spans, redactor=REDACTOR)
    db.commit()

    case = draft_case(db, run, suite="smoke", redactor=REDACTOR).case

    assert case["expected"] == {"answer": "It is sunny in Paris."}


def test_a_cleared_answer_is_sql_null_not_json_null(
    cli: Callable[..., Any], db: Session, ids: dict[str, uuid.UUID]
) -> None:
    run = str(ids["plain"])
    cli("label", run, "--mode", "first", "--answer", "old")
    cli("label", run, "--mode", "second", "--answer", "new")

    nulls = db.scalar(select(func.count()).where(Label.expected.is_(None)))

    assert nulls == 1  # the first label's answer, cleared
