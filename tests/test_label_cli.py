"""`afr label`, `afr labels` and `--attr`: hand labelling of runs (#14, docs/cli.md).

Written from the #14 plan before the code (.review/14-label/plan.md).
"""

from __future__ import annotations

import uuid
from collections import Counter
from collections.abc import Callable, Generator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import typer
from fake_secrets import private_key
from sqlalchemy import create_engine, delete, func, select, text
from sqlalchemy.orm import Session
from typer.testing import CliRunner

from flight_recorder.cli import app
from flight_recorder.redact import Redactor
from flight_recorder.store.db import upgrade
from flight_recorder.store.models import Label, Run, Span
from flight_recorder.store.read import RunFilter, find_runs
from flight_recorder.store.write import RunIn, SpanIn, record_run

pytestmark = pytest.mark.db

runner = CliRunner()
REDACTOR = Redactor(b"test-key-0123456789abcdef0123456789abcdef")
T0 = datetime(2026, 11, 1, 9, 0, tzinfo=UTC)
GROUPS = {"a": 6, "b": 3, "c": 1}  # wrong runs per batch group


def _run(session: Session, n: int, attributes: dict[str, Any]) -> uuid.UUID:
    start = T0 + timedelta(minutes=n)
    return record_run(
        session,
        RunIn(
            source="otel",
            external_id=f"trace-{n:03d}",
            agent="triager",
            name=f"triage #{n}",
            status="ok",
            started_at=start,
            ended_at=start + timedelta(seconds=5),
            attributes=attributes,
        ),
        [
            SpanIn(
                external_id=f"root-{n}",
                kind="agent",
                name="triage",
                started_at=start,
                ended_at=start + timedelta(seconds=5),
                status="ok",
                attributes=attributes,
            ),
            SpanIn(
                external_id=f"tool-{n}",
                parent_external_id=f"root-{n}",
                kind="tool",
                name="search_issues",
                started_at=start,
                ended_at=start + timedelta(seconds=1),
                status="error",
                error="timed out",
            ),
        ],
        redactor=REDACTOR,
    )


def seed(session: Session) -> dict[str, Any]:
    wrong: dict[str, list[uuid.UUID]] = {}
    n = 0
    for group, count in GROUPS.items():
        for _ in range(count):
            n += 1
            wrong.setdefault(group, []).append(
                _run(
                    session,
                    n,
                    {
                        "triager.grade": "wrong",
                        "triager.batch.group": group,
                        "triager.decided.labels": ["evals", "infra"],
                        "triager.expected.labels": ["evals"],
                        "triager.decided.milestone": "Week 3",
                        "triager.expected.milestone": "Week 3",
                        "telemetry.sdk.name": "opentelemetry",
                    },
                )
            )
    correct = _run(session, 99, {"triager.grade": "correct", "triager.batch.group": "a"})
    return {"wrong": wrong, "correct": correct}


@pytest.fixture(scope="module")
def store(make_database: Callable[[], str]) -> Generator[tuple[str, dict[str, Any]]]:
    url = make_database()
    upgrade(url)
    engine = create_engine(url)
    with Session(engine) as session, session.begin():
        ids = seed(session)
    yield url, ids
    engine.dispose()


@pytest.fixture
def db(store: tuple[str, dict[str, Any]]) -> Generator[Session]:
    """A session on the module's store; every test starts with no labels."""
    engine = create_engine(store[0])
    with Session(engine) as session:
        session.execute(delete(Label))
        session.commit()
        yield session
    engine.dispose()


@pytest.fixture
def ids(store: tuple[str, dict[str, Any]], db: Session) -> dict[str, Any]:
    return store[1]


@pytest.fixture
def cli(store: tuple[str, dict[str, Any]], db: Session, clean_env: pytest.MonkeyPatch,
        tmp_path: Any) -> Callable[..., Any]:  # fmt: skip
    clean_env.setenv("AFR_DATABASE_URL", store[0])
    clean_env.setenv("AFR_HOME", str(tmp_path))  # a fresh redaction key
    clean_env.setenv("COLUMNS", "160")

    def invoke(*args: str, input: str | None = None) -> Any:
        return runner.invoke(app, list(args), input=input)

    return invoke


def labels(db: Session) -> list[Label]:
    db.expire_all()
    return list(db.scalars(select(Label).order_by(Label.created_at, Label.id)))


def all_wrong(ids: dict[str, Any]) -> list[uuid.UUID]:
    return [run for group in ids["wrong"].values() for run in group]


# --- --attr -----------------------------------------------------------------------


def test_attr_options_parse_into_any_of_per_key() -> None:
    from flight_recorder.cli.runs import parse_attrs

    assert parse_attrs(["a=1", "b=x=y", "a=2"]) == {"a": ("1", "2"), "b": ("x=y",)}
    assert parse_attrs([]) == {}
    with pytest.raises(typer.BadParameter):
        parse_attrs(["no-equals"])
    with pytest.raises(typer.BadParameter):
        parse_attrs(["=value"])


def test_attr_filters_and_across_keys_and_or_within_one(db: Session, ids: dict[str, Any]) -> None:
    now = datetime.now(UTC)

    def found(attrs: dict[str, tuple[str, ...]]) -> set[uuid.UUID]:
        return {s.run.id for s in find_runs(db, RunFilter(attrs=attrs, limit=100), now)}

    assert found({"triager.grade": ("wrong",)}) == set(all_wrong(ids))
    assert found({"triager.grade": ("wrong",), "triager.batch.group": ("b",)}) == set(
        ids["wrong"]["b"]
    )
    assert found({"triager.batch.group": ("b", "c")}) == set(ids["wrong"]["b"] + ids["wrong"]["c"])
    assert found({"triager.grade": ("nope",)}) == set()


def test_runs_list_takes_attr(cli: Callable[..., Any], ids: dict[str, Any]) -> None:
    result = cli("runs", "list", "--attr", "triager.grade=correct")

    assert result.exit_code == 0, result.output
    assert "triage #99" in result.output
    assert "triage #1 " not in result.output


# --- the sample -------------------------------------------------------------------


def test_the_sample_is_seeded_and_proportional_to_each_stratum() -> None:
    from flight_recorder.store.labels import sample

    population = [(f"run-{i:02d}", "a" if i < 60 else "b" if i < 90 else "c") for i in range(100)]

    picked = sample(population, 10, seed=14)

    assert len(picked) == 10 == len(set(picked))
    assert picked == sample(population, 10, seed=14)  # the same every time
    assert picked != sample(population, 10, seed=15)
    by = Counter(dict(population)[run] for run in picked)
    assert by == {"a": 6, "b": 3, "c": 1}
    assert sample(population, 500, seed=14) == sorted(r for r, _ in population)  # all of them


def test_the_sample_does_not_depend_on_the_order_runs_come_in() -> None:
    from flight_recorder.store.labels import sample

    population = [(f"run-{i:02d}", str(i % 3)) for i in range(30)]

    assert sample(population, 7, seed=1) == sample(list(reversed(population)), 7, seed=1)


def test_a_sample_rounds_by_largest_remainder() -> None:
    from flight_recorder.store.labels import sample

    population = [("x1", "x"), ("x2", "x"), ("y1", "y"), ("y2", "y"), ("z1", "z")]

    by = Counter(dict(population)[run] for run in sample(population, 3, seed=0))
    assert sum(by.values()) == 3 and set(by) == {"x", "y", "z"}  # 1.2, 1.2, 0.6 → 1, 1, 1


# --- afr label <run> --------------------------------------------------------------


def test_label_records_a_human_failure_mode_and_a_redacted_note(
    cli: Callable[..., Any], db: Session, ids: dict[str, Any]
) -> None:
    run = ids["wrong"]["a"][0]
    secret = private_key()

    result = cli("label", str(run)[:8], "--mode", "over-labelling", "--note",
                 f"adds infra; pasted {secret}", "--author", "tester")  # fmt: skip

    assert result.exit_code == 0, result.output
    [label] = labels(db)
    assert (label.run_id, label.span_id, label.failure_mode) == (run, None, "over-labelling")
    assert (label.source, label.author, label.confidence) == ("human", "tester", None)
    assert label.note is not None and label.note.startswith("adds infra; pasted ")
    assert secret not in label.note and "REDACTED" in label.note


def test_a_run_can_have_several_labels(
    cli: Callable[..., Any], db: Session, ids: dict[str, Any]
) -> None:
    run = str(ids["wrong"]["a"][0])
    cli("label", run, "--mode", "over-labelling")
    cli("label", run, "--mode", "false-duplicate")

    assert sorted(label.failure_mode for label in labels(db)) == [
        "false-duplicate",
        "over-labelling",
    ]


def test_the_author_defaults_to_the_os_user(
    cli: Callable[..., Any], db: Session, ids: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("getpass.getuser", lambda: "someone")
    cli("label", str(ids["wrong"]["a"][0]), "--mode", "m")

    assert labels(db)[0].author == "someone"


def test_label_shows_the_run_decided_next_to_expected_and_its_errors(
    cli: Callable[..., Any], ids: dict[str, Any]
) -> None:
    run = str(ids["wrong"]["a"][0])
    cli("label", run, "--mode", "earlier-mode", "--note", "first look")

    result = cli("label", run, "--mode", "m")

    out = result.output
    assert result.exit_code == 0, out
    assert "triage #1" in out
    labels_line = next(line for line in out.splitlines() if "triager" in line and "labels" in line)
    assert "evals, infra" in labels_line and "≠" in labels_line  # decided vs expected, one row
    milestone = next(line for line in out.splitlines() if "milestone" in line)
    assert milestone.count("Week 3") == 2 and "≠" not in milestone
    assert "triager.grade" in out and "wrong" in out
    assert "telemetry.sdk.name" not in out  # SDK noise is left out
    assert "search_issues" in out and "timed out" in out  # the error span
    assert "earlier-mode" in out and "first look" in out  # labels so far


def test_label_asks_for_the_mode_offering_existing_ones(
    cli: Callable[..., Any], db: Session, ids: dict[str, Any]
) -> None:
    first, second, third = (str(r) for r in ids["wrong"]["a"][:3])
    cli("label", first, "--mode", "over-labelling")
    cli("label", second, "--mode", "over-labelling")
    cli("label", second, "--mode", "false-duplicate")

    picked = cli("label", third, input="1\nsame as the others\n")
    typed = cli("label", third, input="missed-priority\n\n")

    assert picked.exit_code == 0 and typed.exit_code == 0, picked.output + typed.output
    assert "1) over-labelling (2)" in picked.output  # most used first, with counts
    assert "2) false-duplicate (1)" in picked.output
    mine = [label for label in labels(db) if str(label.run_id) == third]
    assert [(m.failure_mode, m.note) for m in mine] == [
        ("over-labelling", "same as the others"),
        ("missed-priority", None),  # an empty note is no note
    ]


def test_label_without_a_mode_and_no_answer_writes_nothing(
    cli: Callable[..., Any], db: Session, ids: dict[str, Any]
) -> None:
    result = cli("label", str(ids["wrong"]["a"][0]), input="")

    assert result.exit_code != 0
    assert labels(db) == []


@pytest.mark.parametrize("mode", ["", "   ", "x" * 81])
def test_a_mode_must_be_a_short_name(
    cli: Callable[..., Any], db: Session, ids: dict[str, Any], mode: str
) -> None:
    result = cli("label", str(ids["wrong"]["a"][0]), "--mode", mode)

    assert result.exit_code == 2
    assert labels(db) == []


def test_label_can_name_a_span_of_the_run(
    cli: Callable[..., Any], db: Session, ids: dict[str, Any]
) -> None:
    run, other = ids["wrong"]["a"][0], ids["wrong"]["b"][0]
    tool = db.scalar(select(Span.id).where(Span.run_id == run, Span.kind == "tool"))
    foreign = db.scalar(select(Span.id).where(Span.run_id == other, Span.kind == "tool"))

    ok = cli("label", str(run), "--span", str(tool)[:8], "--mode", "tool-timeout")
    refused = cli("label", str(run), "--span", str(foreign)[:8], "--mode", "tool-timeout")

    assert ok.exit_code == 0, ok.output
    assert refused.exit_code != 0 and "span" in refused.output
    [label] = labels(db)
    assert label.span_id == tool


# --- afr label --next -------------------------------------------------------------


def test_next_walks_the_sample_skipping_labelled_runs(
    cli: Callable[..., Any], db: Session, ids: dict[str, Any]
) -> None:
    from flight_recorder.store.labels import sample

    queue = ["label", "--next", "--attr", "triager.grade=wrong", "--sample", "5",
             "--by", "triager.batch.group", "--seed", "14"]  # fmt: skip
    expected = sample([(str(r), g) for g, runs in ids["wrong"].items() for r in runs], 5, seed=14)

    first = cli(*queue, input="mode-one\nnote one\nq\n")
    second = cli(*queue, input="mode-two\n\nq\n")

    assert first.exit_code == 0 and second.exit_code == 0, first.output + second.output
    assert "0 of 5 labelled" in first.output and "1 of 5 labelled" in second.output
    assert [str(label.run_id) for label in labels(db)] == expected[:2]
    assert ids["correct"] not in {label.run_id for label in labels(db)}


def test_next_can_skip_a_run_and_says_when_the_sample_is_done(
    cli: Callable[..., Any], db: Session, ids: dict[str, Any]
) -> None:
    queue = ["label", "--next", "--attr", "triager.batch.group=c"]  # one run, no --sample: all

    skipped = cli(*queue, input="s\n")
    done = cli(*queue, input="mode\n\n")
    again = cli(*queue)

    assert skipped.exit_code == 0 and labels(db)[0].run_id == ids["wrong"]["c"][0]
    assert done.exit_code == 0
    assert "all 1 labelled" in again.output and again.exit_code == 0


def test_next_with_mode_is_refused(cli: Callable[..., Any]) -> None:
    result = cli("label", "--next", "--mode", "m")

    assert result.exit_code == 2


def test_label_needs_a_run_or_next(cli: Callable[..., Any]) -> None:
    assert cli("label").exit_code == 2


# --- afr labels -------------------------------------------------------------------


def test_labels_list_counts_each_mode_with_an_example(
    cli: Callable[..., Any], ids: dict[str, Any]
) -> None:
    a = ids["wrong"]["a"]
    for run in a[:3]:
        cli("label", str(run), "--mode", "over-labelling")
    cli("label", str(a[3]), "--mode", "false-duplicate")

    result = cli("labels", "list")

    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    over = next(i for i, line in enumerate(lines) if "over-labelling" in line)
    dup = next(i for i, line in enumerate(lines) if "false-duplicate" in line)
    assert over < dup  # most common first
    assert " 3 " in f" {lines[over]} " and str(a[0])[:8] in lines[over]
    assert "4 labels on 4 runs" in result.output


def test_labels_rename_merges_modes(
    cli: Callable[..., Any], db: Session, ids: dict[str, Any]
) -> None:
    a = ids["wrong"]["a"]
    cli("label", str(a[0]), "--mode", "extra-label")
    cli("label", str(a[1]), "--mode", "over-labelling")

    result = cli("labels", "rename", "extra-label", "over-labelling")

    assert result.exit_code == 0 and "1 label" in result.output, result.output
    assert {label.failure_mode for label in labels(db)} == {"over-labelling"}
    assert cli("labels", "rename", "no-such-mode", "x").exit_code == 1


def test_labels_rename_leaves_judge_labels_alone(
    cli: Callable[..., Any], db: Session, ids: dict[str, Any]
) -> None:
    run = ids["wrong"]["a"][0]
    db.add(Label(run_id=run, failure_mode="extra-label", source="judge", author="model"))
    db.commit()

    cli("labels", "rename", "extra-label", "over-labelling")

    assert [label.failure_mode for label in labels(db)] == ["extra-label"]


def test_labels_remove_deletes_a_runs_human_labels(
    cli: Callable[..., Any], db: Session, ids: dict[str, Any]
) -> None:
    run = str(ids["wrong"]["a"][0])
    cli("label", run, "--mode", "one")
    cli("label", run, "--mode", "two")

    one = cli("labels", "remove", run, "--mode", "one")
    assert one.exit_code == 0, one.output
    assert [label.failure_mode for label in labels(db)] == ["two"]

    rest = cli("labels", "remove", run)
    assert rest.exit_code == 0 and labels(db) == []


def test_labelling_never_changes_runs_or_spans(
    cli: Callable[..., Any], db: Session, ids: dict[str, Any]
) -> None:
    def snapshot() -> tuple[Any, ...]:
        db.expire_all()
        return (
            db.scalar(select(func.max(Run.updated_at))),
            db.scalar(select(func.max(Span.updated_at))),
            db.scalar(select(func.count()).select_from(Run)),
        )

    before = snapshot()
    cli("label", str(ids["wrong"]["a"][0]), "--mode", "m", "--note", "n")
    cli("labels", "rename", "m", "n")
    assert snapshot() == before


# --- review round 1: tests written before the fixes ----------------------------------


@pytest.mark.parametrize("mode", ["bad\x1b[2Jmode", "tab\tmode", "q", "s", "Q", "S", "7"])
def test_modes_refuse_control_characters_and_reserved_answers(mode: str) -> None:
    from flight_recorder.store.labels import check_mode

    with pytest.raises(ValueError):
        check_mode(mode)


def test_a_note_given_without_a_mode_is_kept(
    cli: Callable[..., Any], db: Session, ids: dict[str, Any]
) -> None:
    result = cli("label", str(ids["wrong"]["a"][0]), "--note", "from the flag", input="m\n")

    assert result.exit_code == 0, result.output
    assert labels(db)[0].note == "from the flag"


@pytest.mark.parametrize("wrong", ["12", "0", "²", "9" * 5000])
def test_a_number_not_on_the_list_asks_again(
    cli: Callable[..., Any], db: Session, ids: dict[str, Any], wrong: str
) -> None:
    run = str(ids["wrong"]["a"][0])
    cli("label", run, "--mode", "existing")

    result = cli("label", run, input=f"{wrong}\nreal-mode\n\n")

    assert result.exit_code == 0, result.output
    assert sorted(label.failure_mode for label in labels(db)) == ["existing", "real-mode"]


def test_q_quits_a_single_run_without_labelling(
    cli: Callable[..., Any], db: Session, ids: dict[str, Any]
) -> None:
    result = cli("label", str(ids["wrong"]["a"][0]), input="q\n")

    assert result.exit_code == 0, result.output
    assert labels(db) == []


def test_a_new_run_in_one_stratum_leaves_the_others_picks_alone() -> None:
    from flight_recorder.store.labels import sample

    population = [(f"a{i}", "a") for i in range(10)] + [(f"b{i}", "b") for i in range(10)]
    before = [r for r in sample(population, 6, seed=14) if r.startswith("b")]
    after = [r for r in sample([*population, ("a99", "a")], 6, seed=14) if r.startswith("b")]

    assert before == after


# --- several modes on one run -------------------------------------------------------


def test_label_asks_for_another_mode_until_an_empty_answer(
    cli: Callable[..., Any], db: Session, ids: dict[str, Any]
) -> None:
    run = str(ids["wrong"]["a"][0])

    result = cli("label", run, input="over-labelling\nnote a\nfalse-duplicate\nnote b\n\n")

    assert result.exit_code == 0, result.output
    assert "another mode" in result.output
    assert [(m.failure_mode, m.note) for m in labels(db)] == [
        ("over-labelling", "note a"),
        ("false-duplicate", "note b"),
    ]


def test_next_takes_several_modes_for_a_run_before_moving_on(
    cli: Callable[..., Any], db: Session, ids: dict[str, Any]
) -> None:
    queue = ["label", "--next", "--attr", "triager.grade=wrong"]

    result = cli(*queue, input="mode-one\n\nmode-two\n\n\nq\n")

    assert result.exit_code == 0, result.output
    assert len({label.run_id for label in labels(db)}) == 1
    assert sorted(label.failure_mode for label in labels(db)) == ["mode-one", "mode-two"]
    assert "1 of " in result.output  # moved on to the next run


def test_q_after_a_mode_keeps_it_and_stops_the_queue(
    cli: Callable[..., Any], db: Session, ids: dict[str, Any]
) -> None:
    result = cli("label", "--next", "--attr", "triager.grade=wrong", input="mode-one\n\nq\n")

    assert result.exit_code == 0, result.output
    assert [label.failure_mode for label in labels(db)] == ["mode-one"]
    assert "1 of " not in result.output


def test_a_mode_the_run_already_has_is_asked_again(
    cli: Callable[..., Any], db: Session, ids: dict[str, Any]
) -> None:
    run = str(ids["wrong"]["a"][0])
    cli("label", run, "--mode", "existing")

    result = cli("label", run, input="1\nexisting\nnew-one\n\n\n")

    assert result.exit_code == 0, result.output
    assert "already" in result.output
    assert sorted(label.failure_mode for label in labels(db)) == ["existing", "new-one"]


def test_label_with_a_mode_the_run_already_has_adds_nothing(
    cli: Callable[..., Any], db: Session, ids: dict[str, Any]
) -> None:
    run = str(ids["wrong"]["a"][0])
    cli("label", run, "--mode", "existing")

    result = cli("label", run, "--mode", "existing", "--note", "again")

    assert result.exit_code == 0, result.output
    assert "already" in result.output
    assert [(m.failure_mode, m.note) for m in labels(db)] == [("existing", None)]


def test_the_same_mode_on_another_span_of_the_run_is_a_new_label(
    cli: Callable[..., Any], db: Session, ids: dict[str, Any]
) -> None:
    run = ids["wrong"]["a"][0]
    tool = db.scalar(select(Span.id).where(Span.run_id == run, Span.kind == "tool"))

    cli("label", str(run), "--mode", "tool-timeout")
    cli("label", str(run), "--span", str(tool)[:8], "--mode", "tool-timeout")

    assert sorted(str(label.span_id) for label in labels(db)) == sorted(["None", str(tool)])


def test_merging_two_modes_a_run_has_leaves_it_one_label(
    cli: Callable[..., Any], db: Session, ids: dict[str, Any]
) -> None:
    a = ids["wrong"]["a"]
    cli("label", str(a[0]), "--mode", "extra-label")
    cli("label", str(a[0]), "--mode", "over-labelling")
    cli("label", str(a[1]), "--mode", "extra-label")

    result = cli("labels", "rename", "extra-label", "over-labelling")

    assert result.exit_code == 0, result.output
    assert sorted((str(m.run_id), m.failure_mode) for m in labels(db)) == sorted(
        [(str(a[0]), "over-labelling"), (str(a[1]), "over-labelling")]
    )
    listed = cli("labels", "list").output
    assert "2 labels on 2 runs" in listed


def test_renaming_a_mode_to_itself_is_refused_and_keeps_its_labels(
    cli: Callable[..., Any], db: Session, ids: dict[str, Any]
) -> None:
    cli("label", str(ids["wrong"]["a"][0]), "--mode", "extra-label")

    result = cli("labels", "rename", "extra-label", " extra-label ")

    assert result.exit_code == 2, result.output
    assert [label.failure_mode for label in labels(db)] == ["extra-label"]


# --- review round 1 ------------------------------------------------------------------


def test_input_ending_at_the_another_mode_prompt_keeps_the_label_and_exits_0(
    cli: Callable[..., Any], db: Session, ids: dict[str, Any]
) -> None:
    run = str(ids["wrong"]["a"][0])

    single = cli("label", run, input="m1\nnote\n")
    queued = cli("label", "--next", "--attr", "triager.grade=wrong", input="m2\n\n")

    assert single.exit_code == 0 and queued.exit_code == 0, single.output + queued.output
    assert "labelled" in single.output
    # the single run above is in the queue too, so it starts at 1; stopping means no 2
    assert "1 of " in queued.output and "2 of " not in queued.output
    assert sorted(label.failure_mode for label in labels(db)) == ["m1", "m2"]


def test_q_at_the_another_mode_prompt_says_what_was_labelled(
    cli: Callable[..., Any], db: Session, ids: dict[str, Any]
) -> None:
    run = str(ids["wrong"]["a"][0])

    result = cli("label", run, input="m1\n\nq\n")

    assert result.exit_code == 0, result.output
    assert f"labelled {run[:8]}: m1" in result.output


def test_s_at_the_another_mode_prompt_explains_itself(
    cli: Callable[..., Any], db: Session, ids: dict[str, Any]
) -> None:
    result = cli("label", "--next", "--attr", "triager.batch.group=c", input="m1\n\ns\n\n")

    assert result.exit_code == 0, result.output
    assert "skips only before the first mode" in result.output
    assert [label.failure_mode for label in labels(db)] == ["m1"]


def test_a_mode_the_span_already_has_names_the_span(
    cli: Callable[..., Any], db: Session, ids: dict[str, Any]
) -> None:
    run = ids["wrong"]["a"][0]
    tool = str(db.scalar(select(Span.id).where(Span.run_id == run, Span.kind == "tool")))[:8]
    cli("label", str(run), "--span", tool, "--mode", "tool-timeout")

    result = cli("label", str(run), "--span", tool, "--mode", "tool-timeout")

    assert result.exit_code == 0, result.output
    assert f"span {tool} already has tool-timeout" in result.output


def test_rename_merges_only_on_the_same_span_and_says_how_many(
    cli: Callable[..., Any], db: Session, ids: dict[str, Any]
) -> None:
    a = ids["wrong"]["a"]
    tool = str(db.scalar(select(Span.id).where(Span.run_id == a[0], Span.kind == "tool")))[:8]
    cli("label", str(a[0]), "--span", tool, "--mode", "old")  # on a span: renamed
    cli("label", str(a[0]), "--mode", "new")
    cli("label", str(a[1]), "--mode", "old", "--note", "lost")  # same run and span: merged
    cli("label", str(a[1]), "--mode", "new")

    result = cli("labels", "rename", "old", "new")

    assert result.exit_code == 0, result.output
    assert "renamed 1 label" in result.output and "1 merged" in result.output
    assert "notes" in result.output
    assert sorted((str(m.run_id), str(m.span_id) != "None") for m in labels(db)) == sorted(
        [(str(a[0]), True), (str(a[0]), False), (str(a[1]), False)]
    )
    assert {m.failure_mode for m in labels(db)} == {"new"}


# --- notes kept on merge, and one mode per run enforced by the database ----------------


def test_a_merge_keeps_the_dropped_labels_note_with_a_versioned_tag(
    cli: Callable[..., Any], db: Session, ids: dict[str, Any]
) -> None:
    a = ids["wrong"]["a"]
    put(db, a[0], "old", "dropped note", at=AT(10), author="sonic")
    put(db, a[0], "new", "kept note", at=AT(11))
    put(db, a[1], "old", "only note", at=AT(12), author=None)
    put(db, a[1], "new", None, at=AT(13))
    put(db, a[2], "old", None, at=AT(14))
    put(db, a[2], "new", "untouched", at=AT(15))

    result = cli("labels", "rename", "old", "new")

    assert result.exit_code == 0, result.output
    assert "notes are kept" in result.output
    notes = {str(m.run_id): m.note for m in labels(db)}
    assert notes == {
        str(
            a[0]
        ): "kept note; [afr-note v1 at=2026-11-13T10:10:00Z by=sonic mode=old] dropped note",
        str(a[1]): "[afr-note v1 at=2026-11-13T10:12:00Z by=- mode=old] only note",
        str(a[2]): "untouched",
    }


def test_the_database_refuses_a_second_human_label_with_the_same_mode(
    db: Session, ids: dict[str, Any]
) -> None:
    from sqlalchemy.exc import IntegrityError

    run = ids["wrong"]["a"][0]
    tool = db.scalar(select(Span.id).where(Span.run_id == run, Span.kind == "tool"))
    for span in (None, tool):
        db.add(Label(run_id=run, span_id=span, failure_mode="m", source="human"))
        db.commit()
        db.add(Label(run_id=run, span_id=span, failure_mode="m", source="human"))
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()
    db.add_all([Label(run_id=run, failure_mode="m", source="judge") for _ in range(2)])
    db.commit()  # model labels may repeat: each judge run is its own opinion


def test_add_label_racing_another_writer_adds_nothing(
    db: Session, ids: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two labellers at once: both pass the "already has it?" check, one insert wins."""
    import flight_recorder.store.labels as store

    monkeypatch.setattr(store, "modes_of", lambda *args, **kwargs: set())
    run = ids["wrong"]["a"][0]

    first = store.add_label(db, run, "m", "one", author="a", redactor=REDACTOR)
    db.commit()
    second = store.add_label(db, run, "m", "two", author="b", redactor=REDACTOR)
    db.commit()

    assert first is not None and second is None
    assert [(m.failure_mode, m.note) for m in labels(db)] == [("m", "one")]


def test_two_labellers_at_once_add_the_mode_once(
    store: tuple[str, dict[str, Any]], db: Session, ids: dict[str, Any]
) -> None:
    """Two connections, as two `afr label` processes: the first adds the mode but hasn't
    committed; the second checks (sees nothing yet), inserts, waits on the first, and adds
    nothing once it commits. In either order, one label."""
    import threading
    import time

    from flight_recorder.store.labels import add_label

    run = ids["wrong"]["a"][0]
    engine = create_engine(store[0])
    results: list[Label | None] = []
    try:
        with Session(engine) as first, Session(engine) as second:
            assert add_label(first, run, "m", "first", author="a", redactor=REDACTOR)

            def label_too() -> None:
                results.append(add_label(second, run, "m", "second", author="b",
                                         redactor=REDACTOR))  # fmt: skip
                second.commit()

            racer = threading.Thread(target=label_too)
            racer.start()
            # wait until the second is past its check and blocked on the first's row, so
            # the test proves the race rather than passing on the check alone
            blocked = False
            for _ in range(100):
                db.commit()  # pg_stat_activity is a snapshot per transaction
                blocked = bool(
                    db.scalar(
                        text(
                            "SELECT count(*) FROM pg_stat_activity WHERE wait_event_type = "
                            "'Lock' AND datname = current_database()"
                        )
                    )
                )
                if blocked:
                    break
                time.sleep(0.05)
            first.commit()
            racer.join(timeout=10)
            assert blocked, "the second labeller never waited on the first"
    finally:
        engine.dispose()

    assert results == [None]
    assert [(m.failure_mode, m.note) for m in labels(db)] == [("m", "first")]


# --- review round 3 ------------------------------------------------------------------


def test_many_labels_on_one_connection_keep_working(
    db: Session, ids: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """psycopg prepares a statement after 5 runs, and Postgres then may use a generic plan:
    the ON CONFLICT predicate must still match the partial index (a bound parameter didn't)."""
    import flight_recorder.store.labels as store

    db.execute(text("SET plan_cache_mode = force_generic_plan"))
    monkeypatch.setattr(store, "modes_of", lambda *args, **kwargs: set())  # always insert
    run = ids["wrong"]["a"][0]

    added = [store.add_label(db, run, f"m{i % 15}", None, author="a", redactor=REDACTOR)
             for i in range(30)]  # fmt: skip
    db.commit()

    assert sum(a is not None for a in added) == 15
    assert len(labels(db)) == 15


def test_a_mode_another_labeller_just_added_is_reported_not_claimed(
    cli: Callable[..., Any], db: Session, ids: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    import flight_recorder.store.labels as store

    run = ids["wrong"]["a"][0]
    db.add(Label(run_id=run, failure_mode="m", source="human", note="theirs"))
    db.commit()
    monkeypatch.setattr(store, "modes_of", lambda *args, **kwargs: set())  # checked too early

    result = cli("label", str(run), input="m\nmine\nq\n")  # nothing saved: asked again

    assert result.exit_code == 0, result.output
    assert "someone just added" in result.output
    assert f"labelled {str(run)[:8]}: m" not in result.output
    assert [(m.failure_mode, m.note) for m in labels(db)] == [("m", "theirs")]


@pytest.mark.parametrize("sqlstate", ["23505", "40P01"])  # unique violation, deadlock
def test_rename_racing_another_labeller_renames_nothing_and_says_try_again(
    cli: Callable[..., Any],
    db: Session,
    ids: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    sqlstate: str,
) -> None:
    from sqlalchemy.exc import IntegrityError, OperationalError

    import flight_recorder.store.labels as store

    cli("label", str(ids["wrong"]["a"][0]), "--mode", "old")

    class Raced(Exception):
        sqlstate = ""

    raced = Raced()
    raced.sqlstate = sqlstate
    error = (IntegrityError if sqlstate == "23505" else OperationalError)("stmt", {}, raced)

    def rename_then_collide(session: Session, old: str, new: str) -> tuple[int, int]:
        session.execute(store.update(Label).values(failure_mode=new))
        raise error

    monkeypatch.setattr(store, "rename_mode", rename_then_collide)

    result = cli("labels", "rename", "old", "new")

    assert result.exit_code == 1
    assert "try again" in result.output
    assert [m.failure_mode for m in labels(db)] == ["old"]


# --- round 4: one mode whatever its spelling; versioned tags; merge order free ----------


def AT(minute: int) -> datetime:
    return datetime(2026, 11, 13, 10, minute, tzinfo=UTC)


def put(db: Session, run: uuid.UUID, mode: str, note: str | None, *, at: datetime,
        author: str | None = "t") -> None:  # fmt: skip
    """A human label with a fixed time, as `afr label` would have written it."""
    db.add(Label(run_id=run, failure_mode=mode, note=note, source="human", author=author,
                 created_at=at))  # fmt: skip
    db.commit()


def test_a_mode_is_stored_in_one_canonical_spelling(
    cli: Callable[..., Any], db: Session, ids: dict[str, Any]
) -> None:
    result = cli("label", str(ids["wrong"]["a"][0]), "--mode", "  Tool   Timeout ")

    assert result.exit_code == 0, result.output
    assert [m.failure_mode for m in labels(db)] == ["tool-timeout"]


def test_the_same_words_in_any_order_case_or_separators_are_one_mode(
    cli: Callable[..., Any], db: Session, ids: dict[str, Any]
) -> None:
    a = ids["wrong"]["a"]
    cli("label", str(a[0]), "--mode", "tool-timeout")

    again = cli("label", str(a[0]), "--mode", "Timeout_Tool")
    other_run = cli("label", str(a[1]), "--mode", "TIMEOUT tool")
    listed = cli("labels", "list")

    assert "already has" in again.output and again.exit_code == 0
    assert other_run.exit_code == 0, other_run.output
    assert sorted((str(m.run_id), m.failure_mode) for m in labels(db)) == sorted(
        [(str(a[0]), "tool-timeout"), (str(a[1]), "tool-timeout")]
    )
    assert "2 labels on 2 runs" in listed.output
    assert "timeout-tool" not in listed.output  # one mode, shown in its first spelling


def test_the_database_refuses_the_same_words_in_another_order(
    db: Session, ids: dict[str, Any]
) -> None:
    from sqlalchemy.exc import IntegrityError

    run = ids["wrong"]["a"][0]
    put(db, run, "tool-timeout", None, at=AT(1))
    db.add(Label(run_id=run, failure_mode="timeout-tool", source="human"))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_typing_a_mode_the_run_has_in_another_spelling_asks_again(
    cli: Callable[..., Any], db: Session, ids: dict[str, Any]
) -> None:
    run = str(ids["wrong"]["a"][0])
    cli("label", run, "--mode", "tool-timeout")

    result = cli("label", run, input="Timeout Tool\nother\n\n\n")

    assert result.exit_code == 0, result.output
    assert "already" in result.output
    assert sorted(m.failure_mode for m in labels(db)) == ["other", "tool-timeout"]


@pytest.mark.parametrize("mode", ["!!!", "- _ -"])
def test_a_mode_needs_a_letter_or_digit(
    cli: Callable[..., Any], db: Session, ids: dict[str, Any], mode: str
) -> None:
    result = cli("label", str(ids["wrong"]["a"][0]), "--mode", mode)

    assert result.exit_code == 2
    assert labels(db) == []


def test_rename_to_the_same_words_respells_them(
    cli: Callable[..., Any], db: Session, ids: dict[str, Any]
) -> None:
    a = ids["wrong"]["a"]
    cli("label", str(a[0]), "--mode", "timeout-tool")
    cli("label", str(a[1]), "--mode", "timeout-tool")

    respelled = cli("labels", "rename", "timeout-tool", "Tool Timeout")
    same = cli("labels", "rename", "tool-timeout", "Tool_Timeout")

    assert respelled.exit_code == 0, respelled.output
    assert "respelled 2 labels" in respelled.output
    assert {m.failure_mode for m in labels(db)} == {"tool-timeout"}
    assert same.exit_code == 2


def test_rename_and_remove_find_a_mode_whatever_its_spelling(
    cli: Callable[..., Any], db: Session, ids: dict[str, Any]
) -> None:
    a = ids["wrong"]["a"]
    cli("label", str(a[0]), "--mode", "tool-timeout")
    cli("label", str(a[1]), "--mode", "tool-timeout")

    renamed = cli("labels", "rename", "TIMEOUT tool", "slow-tool")
    removed = cli("labels", "remove", str(a[0]), "--mode", "Slow Tool")

    assert renamed.exit_code == 0 and "renamed 2 labels" in renamed.output, renamed.output
    assert removed.exit_code == 0 and "removed 1 label" in removed.output, removed.output
    assert [(str(m.run_id), m.failure_mode) for m in labels(db)] == [(str(a[1]), "slow-tool")]


def test_tag_values_are_percent_encoded(
    cli: Callable[..., Any], db: Session, ids: dict[str, Any]
) -> None:
    run = ids["wrong"]["a"][0]
    put(db, run, "old", "n", at=AT(5), author="a b;[x]%")
    put(db, run, "new", None, at=AT(6))

    cli("labels", "rename", "old", "new")

    assert (
        labels(db)[0].note
        == "[afr-note v1 at=2026-11-13T10:05:00Z by=a%20b%3B%5Bx%5D%25 mode=old] n"
    )


def _seed_abc(db: Session, run: uuid.UUID) -> None:
    db.execute(delete(Label))
    db.commit()
    put(db, run, "a", "note a", at=AT(1), author="ann")
    put(db, run, "b", "note b", at=AT(2), author="bob")
    put(db, run, "c", "note c", at=AT(3), author="cy")


def test_the_merged_note_is_the_same_whatever_order_the_merges_ran_in(
    cli: Callable[..., Any], db: Session, ids: dict[str, Any]
) -> None:
    run = ids["wrong"]["a"][0]
    notes = []
    for renames in ([("a", "c"), ("b", "c")], [("b", "c"), ("a", "c")], [("a", "b"), ("b", "c")]):
        _seed_abc(db, run)
        for old, new in renames:
            assert cli("labels", "rename", old, new).exit_code == 0
        [label] = labels(db)
        notes.append((label.failure_mode, label.note))

    expected = (
        "c",
        "note c; [afr-note v1 at=2026-11-13T10:01:00Z by=ann mode=a] note a; "
        "[afr-note v1 at=2026-11-13T10:02:00Z by=bob mode=b] note b",
    )
    assert notes == [expected, expected, expected]


# --- round 5: one word rule, in Python and Postgres; tags can't be typed --------------

AWKWARD = [
    "Tool Timeout",
    "tool_timeout",
    "timeout-tool",
    "  a..b  ",
    "x²",
    "x",
    "½ ¾",
    "ΟΔΟΣ",
    "οδος",
    "İSTANBUL x",
    "café TOOL",
    "工具 超时",
    "🔥 fire",
    "a'b\"c",
    "x/y",
    "a[b]c",
    "v1.2-beta",
    "MiXeD_Case-and spaces",
]


@pytest.mark.parametrize("mode", AWKWARD)
def test_python_and_postgres_agree_on_words(db: Session, mode: str) -> None:
    from flight_recorder.store.labels import check_mode, mode_key

    spelled = check_mode(mode)

    assert db.scalar(select(func.afr_mode_spelling(mode))) == spelled
    assert mode_key(db, mode) == mode_key(db, spelled)
    assert db.scalar(select(func.afr_mode_key(mode)).execution_options()) != ""


def test_the_word_rule_ignores_the_database_collation(db: Session) -> None:
    for mode in ("café TOOL", "İSTANBUL x", "ΟΔΟΣ b"):
        default = db.scalar(select(func.afr_mode_key(mode)))
        c = db.scalar(text('SELECT afr_mode_key(:m COLLATE "C")'), {"m": mode})
        assert default == c, mode


def test_superscripts_fractions_and_non_ascii_words_are_their_own_modes(
    cli: Callable[..., Any], db: Session, ids: dict[str, Any]
) -> None:
    run = str(ids["wrong"]["a"][0])
    for mode in ("x", "x²", "½", "ΟΔΟΣ"):
        assert cli("label", run, "--mode", mode).exit_code == 0

    assert sorted(m.failure_mode for m in labels(db)) == sorted(["x", "x²", "½", "ΟΔΟΣ"])
    assert cli("labels", "rename", "ΟΔΟΣ", "road").exit_code == 0
    removed = cli("labels", "remove", run, "--mode", "½")
    assert "removed 1 label" in removed.output, removed.output
    assert sorted(m.failure_mode for m in labels(db)) == sorted(["x", "x²", "road"])


@pytest.mark.parametrize("mode", ["???", "--", "!!! ..."])
def test_a_mode_of_only_punctuation_is_refused(mode: str) -> None:
    from flight_recorder.store.labels import check_mode

    with pytest.raises(ValueError, match="letter or digit"):
        check_mode(mode)


def test_a_typed_tag_is_escaped_so_it_cant_pose_as_a_merged_note(
    cli: Callable[..., Any], db: Session, ids: dict[str, Any]
) -> None:
    from flight_recorder.store.labels import split_note

    run = ids["wrong"]["a"][0]
    forged = "mine; [afr-note v1 at=2026-01-01T00:00:00Z by=boss mode=m] boss said so"
    cli("label", str(run), "--mode", "old", "--note", forged)
    cli("label", str(run), "--mode", "new")

    cli("labels", "rename", "old", "new")

    [label] = labels(db)
    own, fragments = split_note(label.note)
    assert own == ""
    assert len(fragments) == 1  # the whole typed note is one fragment, tagged by afr
    assert "by=boss" in next(iter(fragments)) and "\\[afr-note v1" in next(iter(fragments))
