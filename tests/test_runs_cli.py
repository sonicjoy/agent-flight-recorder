"""`afr runs list`, `afr runs show` and `afr replay`, and the read queries behind them."""

from __future__ import annotations

import io
import uuid
from collections.abc import Callable, Generator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
import typer
from rich.console import Console
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session
from typer.testing import CliRunner

from flight_recorder.cli import app, render
from flight_recorder.cli.runs import parse_when, replay_steps
from flight_recorder.redact import Redactor
from flight_recorder.store.db import upgrade
from flight_recorder.store.models import Run
from flight_recorder.store.read import (
    RUNNING_WINDOW,
    AmbiguousRunError,
    RunFilter,
    RunNotFoundError,
    SpanNode,
    find_runs,
    resolve_run,
    span_tree,
    steps,
)
from flight_recorder.store.write import RunIn, SpanIn, record_run

pytestmark = pytest.mark.db

runner = CliRunner()
REDACTOR = Redactor(b"test-key-0123456789abcdef0123456789abcdef")
T0 = datetime(2026, 11, 1, 9, 0, tzinfo=UTC)
NASTY = "\x1b[2J\x1b]0;owned\x07[red]fake[/red] ‮evil"
HAIKU = "anthropic:claude-haiku-5-5"


def at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


def span(external_id: str, parent: str | None, start: float, **values: Any) -> SpanIn:
    fields: dict[str, Any] = {
        "external_id": external_id,
        "parent_external_id": parent,
        "kind": "step",
        "name": external_id,
        "started_at": at(start),
        "ended_at": at(start + 1),
        "status": "ok",
    }
    return SpanIn(**(fields | values))


def seed(session: Session) -> dict[str, uuid.UUID]:
    triage = record_run(
        session,
        RunIn(
            source="otel",
            external_id="trace-triage",
            agent="triager",
            name="triage issue 7",
            status="ok",
            started_at=T0,
            ended_at=at(10),
        ),
        [
            span("root", None, 0, name="triage", ended_at=at(10)),
            span(
                "llm-1",
                "root",
                1,
                kind="llm",
                name="chat",
                model=HAIKU,
                input_tokens=1000,
                output_tokens=100,
                cache_read_tokens=800,
                input={"prompt": "line\n" * 100},
                output="the answer",
            ),
            span("tool-1", "root", 2, kind="tool", name="search", status="error", error="boom"),
            span("llm-2", "root", 3, kind="llm", name="chat", model="acme:mystery", input_tokens=5),
        ],
        redactor=REDACTOR,
    )
    hours_2 = 7200  # the session started two hours after the triage run
    big = [span("root", None, hours_2, kind="agent", name=f"session {NASTY}", ended_at=None)]
    for i in range(30):
        status = "rejected" if i == 15 else "ok"
        big.append(
            span(f"tool-{i:02d}", "root", hours_2 + 1 + i, kind="tool", name="Bash", status=status)
        )
    big.append(span("lost-child", "turn:gone", hours_2 + 50, kind="tool", name="Read"))
    session_run = record_run(
        session,
        RunIn(
            source="claude_code",
            external_id="session-big",
            agent="claude-code",
            name=f"fix it {NASTY}",
            started_at=at(hours_2),
        ),
        big,
        redactor=REDACTOR,
    )
    failed = record_run(
        session,
        RunIn(
            source="otel",
            external_id="trace-failed",
            agent="other",
            name="failed run",
            status="error",
            started_at=at(3600),
            ended_at=at(3605),
        ),
        [],
        redactor=REDACTOR,
    )
    for suffix in ("0000", "1111"):  # two runs whose ids share a prefix
        session.add(
            Run(
                id=uuid.UUID(f"abcd{suffix}-0000-4000-8000-000000000000"),
                source="otel",
                external_id=f"trace-abcd{suffix}",
                status="ok",
                started_at=T0 - timedelta(days=30),
                ended_at=T0 - timedelta(days=30),
            )
        )
    return {"triage": triage, "session": session_run, "failed": failed}


@pytest.fixture(scope="module")
def store(make_database: Callable[[], str]) -> Generator[tuple[str, dict[str, uuid.UUID]]]:
    url = make_database()
    upgrade(url)
    engine = create_engine(url)
    with Session(engine) as session, session.begin():
        ids = seed(session)
    yield url, ids
    engine.dispose()


@pytest.fixture
def db(store: tuple[str, dict[str, uuid.UUID]]) -> Generator[Session]:
    engine: Engine = create_engine(store[0])
    with Session(engine) as session:
        yield session
    engine.dispose()


@pytest.fixture
def ids(store: tuple[str, dict[str, uuid.UUID]]) -> dict[str, uuid.UUID]:
    return store[1]


@pytest.fixture
def cli(
    store: tuple[str, dict[str, uuid.UUID]], clean_env: pytest.MonkeyPatch
) -> Callable[..., Any]:
    clean_env.setenv("AFR_DATABASE_URL", store[0])
    clean_env.setenv("COLUMNS", "160")

    def invoke(*args: str) -> Any:
        return runner.invoke(app, list(args))

    return invoke


# --- read queries ---------------------------------------------------------------------


def test_runs_are_newest_first_with_filters(db: Session, ids: dict[str, uuid.UUID]) -> None:
    def names(where: RunFilter) -> list[str | None]:
        return [s.run.name for s in find_runs(db, where, datetime.now(UTC))]

    every = names(RunFilter())
    assert every[:3] == [every[0], "failed run", "triage issue 7"]
    assert names(RunFilter(source="otel", agent="triager")) == ["triage issue 7"]
    assert names(RunFilter(status="error")) == ["failed run"]
    assert names(RunFilter(since=at(1), until=at(7000))) == ["failed run"]
    assert len(names(RunFilter(limit=2))) == 2


def test_an_open_run_is_running_only_while_it_keeps_changing(db: Session) -> None:
    now = datetime.now(UTC)
    later = now + RUNNING_WINDOW + timedelta(minutes=1)

    def states(when: datetime) -> dict[str | None, str]:
        return {s.run.external_id: s.state for s in find_runs(db, RunFilter(limit=50), when)}

    assert states(now)["session-big"] == "running"
    assert states(later)["session-big"] == "unfinished"
    assert [s.run.external_id for s in find_runs(db, RunFilter(status="unfinished"), later)] == [
        "session-big"
    ]
    assert find_runs(db, RunFilter(status="running"), later) == []


def test_a_partly_priced_run_keeps_the_known_cost(db: Session, ids: dict[str, uuid.UUID]) -> None:
    (triage,) = find_runs(db, RunFilter(), datetime.now(UTC), ids=[ids["triage"]])

    assert triage.run.cost_usd is None  # the run total is unknown...
    assert triage.unpriced
    assert triage.known_cost is not None and triage.known_cost > 0  # ...but part is known
    assert render.cost(triage.known_cost, triage.unpriced).endswith(" + unpriced")


@pytest.mark.parametrize(
    ("known", "unpriced", "shown"),
    [
        (None, False, ""),  # tool and step spans: no cost, not $0
        (Decimal("0.0012"), False, "$0.0012"),
        (Decimal("12.5"), False, "$12.50"),
        (None, True, "unpriced"),
        (Decimal("0.5"), True, "$0.5000 + unpriced"),
    ],
)
def test_costs_never_pretend_to_be_zero(known: Decimal | None, unpriced: bool, shown: str) -> None:
    assert render.cost(known, unpriced) == shown


def test_last_is_the_newest_finished_run(db: Session, ids: dict[str, uuid.UUID]) -> None:
    assert resolve_run(db, "last").id == ids["failed"]  # not the newer open session


def test_runs_resolve_by_prefix_or_full_source_id(db: Session, ids: dict[str, uuid.UUID]) -> None:
    triage = str(ids["triage"])
    assert resolve_run(db, triage[:8]).id == ids["triage"]
    assert resolve_run(db, triage.upper()).id == ids["triage"]
    assert resolve_run(db, "session-big").id == ids["session"]
    with pytest.raises(RunNotFoundError):
        resolve_run(db, "session")  # source ids only in full
    with pytest.raises(RunNotFoundError, match="at least 4 characters"):
        resolve_run(db, triage[:3])
    with pytest.raises(RunNotFoundError):
        resolve_run(db, "%")  # not a LIKE pattern


def test_an_ambiguous_prefix_lists_the_matches(db: Session) -> None:
    with pytest.raises(AmbiguousRunError) as raised:
        resolve_run(db, "abcd")
    assert len(raised.value.matches) == 2
    assert resolve_run(db, "abcd1").external_id == "trace-abcd1111"


def test_the_tree_nests_spans_and_keeps_orphans(db: Session, ids: dict[str, uuid.UUID]) -> None:
    roots = span_tree(db, ids["session"])

    assert [r.external_id for r in roots] == ["root", "lost-child"]  # its parent never came
    children = roots[0].children
    assert [c.external_id for c in children] == [f"tool-{i:02d}" for i in range(30)]
    assert {c.depth for c in children} == {1}
    assert children[3].path() == [roots[0], children[3]]


def test_a_parent_loop_is_cut_rather_than_lost(db: Session, ids: dict[str, uuid.UUID]) -> None:
    db.execute(
        text("""
            UPDATE spans SET parent_id = (SELECT id FROM spans WHERE run_id = :run
                                          AND external_id = 'tool-01')
            WHERE run_id = :run AND external_id = 'tool-00'
        """),
        {"run": ids["session"]},
    )
    db.execute(
        text("""
            UPDATE spans SET parent_id = (SELECT id FROM spans WHERE run_id = :run
                                          AND external_id = 'tool-00')
            WHERE run_id = :run AND external_id = 'tool-01'
        """),
        {"run": ids["session"]},
    )
    roots = span_tree(db, ids["session"])  # not committed: rolled back with the session

    assert sum(len(r.walk()) for r in roots) == 32
    assert "tool-00" in [r.external_id for r in roots]
    db.rollback()


def test_replay_steps_go_in_time_order_parents_first() -> None:
    parent = node("placeholder", 5)
    child = node("child", 5, parent)  # a placeholder starts with its first child
    early = node("early", 1)

    assert steps([parent, early]) == [early, parent, child]


# --- rendering ------------------------------------------------------------------------


def node(external_id: str, start: float, parent: SpanNode | None = None, **values: Any) -> SpanNode:
    fields: dict[str, Any] = {
        "id": uuid.uuid4(),
        "external_id": external_id,
        "parent_id": parent.id if parent else None,
        "kind": "tool",
        "name": external_id,
        "model": None,
        "status": "ok",
        "error": None,
        "started_at": at(start),
        "ended_at": at(start + 1),
        "input_tokens": None,
        "output_tokens": None,
        "cost_usd": None,
        "usage": {},
    }
    made = SpanNode(**(fields | values))
    if parent:
        made.parent = parent
        made.depth = parent.depth + 1
        parent.children.append(made)
    return made


def rendered(*things: Any, width: int = 160) -> str:
    out = Console(file=io.StringIO(), width=width, color_system=None, markup=False)  # no styles:
    # any escape code in the output came from recorded text
    for thing in things:
        out.print(thing)
    return out.file.getvalue()  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    ("raw", "shown"),
    [
        ("\x1b[2J", "\\x1b[2J"),
        ("title\x1b]0;owned\x07", "title\\x1b]0;owned\\x07"),
        ("a‮b", "a\\u202eb"),
        ("over\rwrite", "over\\x0dwrite"),
        ("crlf\r\nkept\ttab", "crlf\nkept\ttab"),
        ("c1\x9b", "c1\\x9b"),
        ("ok ünïcode 日本", "ok ünïcode 日本"),
    ],
)
def test_clean_shows_control_characters_instead_of_running_them(raw: str, shown: str) -> None:
    assert render.clean(raw) == shown


def test_recorded_text_never_reaches_the_terminal_raw(
    db: Session, ids: dict[str, uuid.UUID]
) -> None:
    roots = span_tree(db, ids["session"])
    output = rendered(render.span_tree(roots), render.step(roots[0], 1, 1, T0, (NASTY, None)))

    assert "\x1b[2J" not in output and "\x1b]0" not in output and "‮" not in output
    assert "[red]fake[/red]" in output  # markup shown as typed, not obeyed
    assert "\\x1b[2J" in output


def test_long_lists_fold_but_never_hide_a_problem(db: Session, ids: dict[str, uuid.UUID]) -> None:
    roots = span_tree(db, ids["session"])
    folded = rendered(render.span_tree(roots))
    everything = rendered(render.span_tree(roots, fold=False))

    assert folded.count("tool Bash") == 2 * render.FOLD_KEEP + 1  # first, last, the rejected
    assert "rejected" in folded
    assert "… 9 more (9 tool)" in folded and "… 10 more (10 tool)" in folded
    assert everything.count("tool Bash") == 30


def test_depth_and_filters_trim_the_tree(db: Session, ids: dict[str, uuid.UUID]) -> None:
    triage = span_tree(db, ids["triage"])

    shallow = rendered(render.span_tree(triage, depth=1))
    assert "… 3 more (2 llm, 1 tool)" in shallow and "chat" not in shallow
    problems = rendered(render.span_tree(triage, shown=render.keep(triage, None, True)))
    assert "search" in problems and "boom" in problems and "chat" not in problems
    llms = rendered(render.span_tree(triage, shown=render.keep(triage, "llm", False)))
    assert llms.count("llm chat") == 2 and "search" not in llms


def test_a_span_line_shows_tokens_cache_cost_and_unpriced(
    db: Session, ids: dict[str, uuid.UUID]
) -> None:
    llm_1, _, llm_2 = span_tree(db, ids["triage"])[0].children
    priced = rendered(render.span_line(llm_1))
    assert "1.0k/100 tok (800 cached)" in priced and "$" in priced
    assert "unpriced" in rendered(render.span_line(llm_2))


def test_long_content_is_cut_unless_full() -> None:
    long = "\n".join(["line"] * 100)
    cut = rendered(render.step(node("x", 0), 1, 1, T0, (long, None)))
    assert "60 more lines; --full shows everything" in cut
    whole = rendered(render.step(node("x", 0), 1, 1, T0, (long, None), full=True))
    assert "more lines" not in whole
    one_long_line = rendered(render.step(node("x", 0), 1, 1, T0, ("x" * 10_000, None)))
    assert "6,000 more characters" in one_long_line


@pytest.mark.parametrize(
    ("count", "shown"),
    [(None, ""), (999, "999"), (1_234, "1.2k"), (158_000, "158k"), (226_000_000, "226M")],
)
def test_token_counts_are_short(count: int | None, shown: str) -> None:
    assert render.tokens(count) == shown


TIME_NOW = datetime(2026, 11, 12, 12, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("typed", "means"),
    [
        ("30m", TIME_NOW - timedelta(minutes=30)),
        ("2h", TIME_NOW - timedelta(hours=2)),
        ("3d", TIME_NOW - timedelta(days=3)),
        ("2w", TIME_NOW - timedelta(weeks=2)),
        ("2026-11-10", datetime(2026, 11, 10).astimezone()),  # local midnight
        ("2026-11-10T14:30", datetime(2026, 11, 10, 14, 30).astimezone()),  # local time
        ("2026-11-10T14:30+00:00", datetime(2026, 11, 10, 14, 30, tzinfo=UTC)),
    ],
)
def test_relative_and_absolute_times(typed: str, means: datetime) -> None:
    assert parse_when(typed, TIME_NOW) == means


@pytest.mark.parametrize("typed", ["2mo", "1y", "yesterday", "10", ""])
def test_other_time_forms_are_refused(typed: str) -> None:
    with pytest.raises(typer.BadParameter):  # no months: `m` is minutes
        parse_when(typed, TIME_NOW)


def test_times_are_shown_in_local_time() -> None:
    now = datetime(2026, 11, 12, 12, 0).astimezone()
    this_year = datetime(2026, 3, 4, 5, 6, tzinfo=UTC)
    assert render.short_time(this_year, now) == this_year.astimezone().strftime("%m-%d %H:%M")
    assert render.short_time(datetime(2025, 3, 4, 12, tzinfo=UTC), now) == "2025-03-04"


# --- replay keys ----------------------------------------------------------------------


def test_replay_moves_on_keys_and_stops_at_the_end() -> None:
    nodes = [node("a", 0), node("b", 1), node("c", 2)]
    out = Console(file=io.StringIO(), width=100)
    loads: list[str] = []

    def load(n: SpanNode) -> tuple[Any, Any]:
        loads.append(n.external_id)
        return None, None

    seen = replay_steps(out, nodes, load, iter(["n", "p", "\r", "\xe0M", "n"]), clear=False)

    assert loads == ["a", "b", "a", "b", "c"]  # content loaded one step at a time
    assert seen == 3


def test_a_replay_step_fits_the_terminal() -> None:
    out = Console(file=io.StringIO(), width=80, height=30, color_system=None)
    long = "\n".join(["line"] * 100)

    replay_steps(out, [node("a", 0)], lambda n: (long, long), iter(["q"]), clear=False)

    assert len(out.file.getvalue().splitlines()) <= 30  # type: ignore[attr-defined]


def test_replay_quits_on_q_and_when_keys_run_out() -> None:
    nodes = [node("a", 0), node("b", 1)]
    out = Console(file=io.StringIO(), width=100)

    assert replay_steps(out, nodes, lambda n: (None, None), iter(["q"]), clear=False) == 1
    assert replay_steps(out, nodes, lambda n: (None, None), iter([]), clear=False) == 1
    assert replay_steps(out, nodes, lambda n: (None, None), iter(["\x1b"]), clear=False) == 1


@pytest.mark.parametrize(
    ("forward", "back"),
    [("\x1b[C", "\x1b[D"), ("\xe0M", "\xe0K"), ("j", "k")],  # POSIX arrows, Windows arrows
)
def test_replay_arrow_keys_move_on_and_back(forward: str, back: str) -> None:
    nodes = [node("a", 0), node("b", 1)]
    loads: list[str] = []

    def load(n: SpanNode) -> tuple[Any, Any]:
        loads.append(n.external_id)
        return None, None

    out = Console(file=io.StringIO(), width=100)
    replay_steps(out, nodes, load, iter([forward, back, "q"]), clear=False)

    assert loads == ["a", "b", "a"]


# --- commands -------------------------------------------------------------------------


def test_list_command(cli: Callable[..., Any]) -> None:
    result = cli("runs", "list", "--source", "otel", "--limit", "2")

    assert result.exit_code == 0, result.output
    assert "failed run" in result.output and "triage issue 7" in result.output
    assert "the newest 2" in result.output
    assert (
        "\x1b" not in result.output and "\\x1b" not in cli("runs", "list").output.split("fix it")[0]
    )


def test_list_filters_by_start_time_and_open_runs(cli: Callable[..., Any]) -> None:
    window = cli(
        "runs",
        "list",
        "--since",
        "2026-11-01T09:00:01+00:00",
        "--until",
        "2026-11-01T10:30:00+00:00",
    )
    assert window.exit_code == 0, window.output
    assert "failed run" in window.output and "triage issue 7" not in window.output
    running = cli("runs", "list", "--status", "running")
    assert "fix it" in running.output and "failed run" not in running.output
    bad = cli("runs", "list", "--since", "yesterday")
    assert bad.exit_code == 2 and "30m, 2h, 3d or 1w" in bad.output


def test_list_rejects_an_unknown_status(cli: Callable[..., Any]) -> None:
    result = cli("runs", "list", "--status", "rejected")
    assert result.exit_code == 2
    assert "running" in result.output


def test_show_command(cli: Callable[..., Any], ids: dict[str, uuid.UUID]) -> None:
    result = cli("runs", "show", str(ids["triage"])[:8])

    assert result.exit_code == 0, result.output
    assert "triage issue 7" in result.output
    assert "1.0k in (80% cache reads)" in result.output
    assert "+ unpriced" in result.output
    assert "boom" in result.output


def test_show_explains_missing_and_ambiguous_runs(cli: Callable[..., Any]) -> None:
    missing = cli("runs", "show", "ffffffff")
    assert missing.exit_code == 1 and "no run matches" in missing.output
    ambiguous = cli("runs", "show", "abcd")
    assert ambiguous.exit_code == 2
    assert "matches 2 runs" in ambiguous.output and "abcd0000" in ambiguous.output


def test_replay_off_a_terminal_prints_headers_and_content_only_on_request(
    cli: Callable[..., Any], ids: dict[str, uuid.UUID]
) -> None:
    run = str(ids["triage"])
    headers = cli("replay", run)
    assert headers.exit_code == 0, headers.output
    assert "step 4/4" in headers.output and "the answer" not in headers.output
    with_content = cli("replay", run, "--content", "--kind", "llm")
    assert "step 2/2" in with_content.output and "the answer" in with_content.output


def test_commands_explain_an_unreachable_store(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("AFR_DATABASE_URL", "postgresql+psycopg://afr:afr@127.0.0.1:1/afr")
    result = runner.invoke(app, ["runs", "list"])
    assert result.exit_code == 2
    assert "can't reach the run store" in result.output


def test_the_commands_can_only_read(cli: Callable[..., Any]) -> None:
    from flight_recorder.cli.runs import _store  # pyright: ignore[reportPrivateUsage]

    with _store() as session:
        assert session.execute(text("SHOW transaction_read_only")).scalar() == "on"
