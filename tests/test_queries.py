"""The MCP server's questions as plain functions (#24): explain_run, search_runs and
failure_modes, on a rolled-back session of the scratch database."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fake_embedder import FakeEmbedder
from sqlalchemy.orm import Session

from flight_recorder import queries
from flight_recorder.queries import QueryError, Trim, explain_run, failure_modes, fit, size
from flight_recorder.redact import Redactor
from flight_recorder.store.labels import add_label, mode_counts, set_answer
from flight_recorder.store.models import Label
from flight_recorder.store.write import RunIn, SpanIn, record_run

pytestmark = pytest.mark.db

REDACTOR = Redactor(b"test-key-0123456789abcdef0123456789abcdef")
T0 = datetime(2026, 11, 1, 9, 0, tzinfo=UTC)
NOW = T0 + timedelta(days=1)


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


def record(session: Session, external_id: str, spans: list[SpanIn], **run: Any) -> uuid.UUID:
    values: dict[str, Any] = {
        "source": "otel",
        "external_id": external_id,
        "agent": "triager",
        "name": external_id,
        "status": "ok",
        "started_at": T0,
        "ended_at": at(100),
    }
    return record_run(session, RunIn(**(values | run)), spans, redactor=REDACTOR)


def chain_failure(message: str) -> list[SpanIn]:
    """One API error that marks the agent, its step and the llm; a refused tool call."""
    return [
        span("root", None, 0, kind="agent", name="triage_agent", status="error", error=message,
             input={"messages": [{"content": "Triage issue 12"}]}, output="no answer"),
        span("model", "root", 1, status="error", error=message),
        span("llm", "model", 2, kind="llm", name="ChatAnthropic", status="error", error=message,
             input={"messages": [{"role": "user", "content": "Classify issue 12"}]}),
        span("tool", "root", 4, kind="tool", name="Bash", status="rejected", error="denied"),
    ]  # fmt: skip


def label(session: Session, run_id: uuid.UUID, mode: str, note: str | None = None) -> Label:
    added = add_label(session, run_id, mode, note, author="owner", redactor=REDACTOR)
    assert added is not None
    return added


# --- explain_run -------------------------------------------------------------------------


def test_references_resolve_and_bad_ones_say_why(session: Session) -> None:
    first = record(session, "trace-aaaa", [span("root", None, 0)])
    record(session, "trace-bbbb", [span("root", None, 0)], started_at=at(10))

    by_prefix = explain_run(session, str(first)[:8], NOW)
    by_external = explain_run(session, "trace-aaaa", NOW)
    newest = explain_run(session, "last", NOW)

    assert by_prefix["run"]["id"] == by_external["run"]["id"] == str(first)
    assert newest["run"]["external_id"] == "trace-bbbb"
    with pytest.raises(QueryError, match="no run matches"):
        explain_run(session, "ffffffff", NOW)
    with pytest.raises(QueryError, match="at least 4 characters"):
        explain_run(session, "fff", NOW)
    with pytest.raises(QueryError, match="say which run"):
        explain_run(session, " ", NOW)


def test_an_ambiguous_reference_lists_the_matches(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    from flight_recorder.store.models import Run
    from flight_recorder.store.read import AmbiguousRunError

    ids = [
        record(session, f"trace-{i}", [span("root", None, 0)], name=f"run {i}") for i in range(7)
    ]
    runs = [session.get(Run, run_id) for run_id in ids]

    def ambiguous(_session: Session, ref: str) -> Run:
        raise AmbiguousRunError(ref, [r for r in runs if r is not None])

    monkeypatch.setattr(queries, "resolve_run", ambiguous)
    with pytest.raises(QueryError) as raised:
        explain_run(session, "abcd", NOW)

    message = str(raised.value)
    assert "'abcd' matches 7 runs; use more of the id" in message
    assert sum(str(run_id) in message for run_id in ids) == queries.MAX_MATCHES
    assert "and 2 more" in message


def test_one_error_is_one_failure_with_its_path_and_input(session: Session) -> None:
    run_id = record(session, "trace-1", chain_failure("rate limited: " + "x" * 400), status="error")

    out = explain_run(session, str(run_id), NOW)

    assert out["failure_evidence"] == "failed_spans"
    [failure] = out["failures"]
    assert failure["name"] == "ChatAnthropic" and failure["kind"] == "llm"
    assert failure["path"] == ["triage_agent", "model", "ChatAnthropic"]
    assert len(failure["error"]) == 300 and failure["error"].endswith("…")
    assert failure["input"] == "user\nClassify issue 12"
    assert [r["name"] for r in out["refused"]] == ["Bash"]
    assert out["asked"] == "Triage issue 12" and out["answered"] == "no answer"
    assert out["run"]["spans"] == 4 and out["cut"] == []


def test_a_wrong_answer_with_no_error_span_rests_on_its_labels(session: Session) -> None:
    attributes = {"triager.decided.labels": ["bug"], "triager.decided.milestone": "(none)"}
    run_id = record(session, "trace-1", [span("root", None, 0)], attributes=attributes)
    kept = label(session, run_id, "wrong-label", "should be docs")
    kept.note = "should be docs; [afr-note v1 at=2026-11-01T09:00:00Z by=x mode=y] merged text"
    set_answer(session, kept.id, {"fields": {"labels": ["docs"]}}, REDACTOR)
    session.flush()

    out = explain_run(session, str(run_id), NOW)

    assert out["failure_evidence"] == "labels_only" and out["failures"] == []
    [shown] = out["labels"]
    assert (shown["mode"], shown["note"], shown["source"]) == (
        "wrong-label",
        "should be docs",
        "human",
    )
    assert out["right_answer"] == {"fields": {"labels": "docs"}}
    assert out["decisions"] == {"labels": "bug", "milestone": "(none)"}
    assert out["cut"] == ["1 note fragments from merged labels left out"]


def test_a_clean_run_has_no_evidence(session: Session) -> None:
    run_id = record(session, "trace-1", [span("root", None, 0)])
    out = explain_run(session, str(run_id), NOW)
    assert out["failure_evidence"] == "none"
    assert out["decisions"] is None and out["right_answer"] is None


def test_other_runs_with_the_same_mode_are_listed_never_the_run_itself(session: Session) -> None:
    run_id = record(session, "trace-1", [span("root", None, 0)])
    other = record(session, "trace-2", [span("root", None, 0)])
    unrelated = record(session, "trace-3", [span("root", None, 0)])
    label(session, run_id, "Wrong Label")
    label(session, other, "wrong-label")  # another spelling of the same mode
    label(session, unrelated, "timeout")

    out = explain_run(session, str(run_id), NOW)

    [group] = out["same_mode_runs"]
    assert [r["id"] for r in group["runs"]] == [str(other)]


def test_a_huge_run_stays_under_budget_and_says_what_it_left_out(session: Session) -> None:
    long = "\x1b" + "y" * 1_000
    spans = [span("root", None, 0, kind="agent", name="agent")]
    for i in range(8):  # 8 failures, each at the bottom of a deep chain
        parent = "root"
        for depth in range(10):
            spans.append(span(f"f{i}-{depth}", parent, i * 20 + depth, name="n" * 300))
            parent = f"f{i}-{depth}"
        spans.append(span(f"f{i}-err", parent, i * 20 + 11, status="error", error=long,
                          input={"messages": [long] * 200}))  # fmt: skip
    spans += [span(f"s{i}", "root", 500 + i) for i in range(5_000 - len(spans))]
    attributes = {f"triager.decided.f{i:02}": "v" * 1_000 for i in range(30)}
    run_id = record(session, "trace-1", spans, attributes=attributes)
    others = [record(session, f"trace-o{i}", [span("root", None, 0)]) for i in range(5)]
    for i in range(12):
        mode = f"mode-{i}"
        label(session, run_id, mode, ("\x1b" * 100 + "note ") * 20)
        if i < 3:
            for other in others:
                label(session, other, mode)
    first = session.query(Label).filter_by(run_id=run_id).order_by(Label.created_at).first()
    assert first is not None
    set_answer(
        session, first.id, {"fields": {f"f{i:02}": "a" * 1_000 for i in range(30)}}, REDACTOR
    )
    session.flush()

    out = explain_run(session, str(run_id), NOW)

    assert size(out) <= queries.MAX_RESULT_CHARS
    assert out["run"]["spans"] == 5_000
    assert len(out["failures"]) >= 1 and len(out["failures"][0]["path"]) == queries.PATH_NAMES
    assert len(out["failures"][0]["input"]) <= 500
    kinds = [line.split(" ", 1)[1] for line in out["cut"]]
    assert len(kinds) == len(set(kinds))  # one line per kind
    lines = {line.split(" ", 1)[1]: int(line.split(" ", 1)[0]) for line in out["cut"]}
    assert lines["failures left out"] >= 3  # 8 found, at most 5 shown
    assert lines["labels left out"] >= 2  # 12 labels, at most 10 shown
    assert lines["decision fields left out"] >= 10  # 30 decided, at most 20 shown


def test_a_structured_input_becomes_one_capped_text(session: Session) -> None:
    messages = [{"role": "user", "content": "m" * 1_000} for _ in range(200)]
    run_id = record(session, "trace-1", [span("root", None, 0, status="error", error="boom",
                                              input={"messages": messages})])  # fmt: skip
    [failure] = explain_run(session, str(run_id), NOW)["failures"]
    assert isinstance(failure["input"], str) and len(failure["input"]) == 500


def test_fit_drops_in_order_counts_each_kind_and_always_stops() -> None:
    result: dict[str, Any] = {"a": ["x" * 100] * 30, "b": ["y" * 100] * 30}
    trims = [Trim("as", queries._pop("a")), Trim("bs", queries._pop("b", keep=1))]  # pyright: ignore[reportPrivateUsage]

    fitted = fit(result, trims, {"bs": 2}, budget=3_000)
    assert size(fitted) <= 3_000
    assert fitted["a"] == [] and 0 < len(fitted["b"]) < 30
    assert sorted(fitted["cut"]) == ["30 as left out", f"{2 + 30 - len(fitted['b'])} bs left out"]

    tiny = fit({"a": ["x"] * 3, "b": ["y" * 500] * 2}, trims, budget=10)
    assert tiny["a"] == [] and len(tiny["b"]) == 1  # nothing left to drop: it stops


# --- failure_modes -----------------------------------------------------------------------


def test_failure_modes_match_mode_counts(session: Session) -> None:
    runs = [record(session, f"trace-{i}", [span("root", None, 0)]) for i in range(3)]
    for run_id in runs:
        label(session, run_id, "wrong-label")
    label(session, runs[0], "timeout")
    session.add(Label(run_id=runs[1], failure_mode="judged-bad", source="judge", author="judge"))
    session.flush()

    for source in ("human", "judge"):
        out = failure_modes(session, source, 30)
        expected = mode_counts(session, source)
        assert [(m["mode"], m["runs"], m["labels"]) for m in out["modes"]] == [
            (c.mode, c.runs, c.labels) for c in expected
        ]
        assert out["source"] == source and out["cut"] == []
    example = failure_modes(session, "human", 1)
    shown = example["modes"][0]["example_run"]
    from flight_recorder.store.models import Run

    run = session.get(Run, uuid.UUID(shown["id"]))
    assert run is not None and shown["external_id"] == run.external_id
    assert example["cut"] == ["1 modes left out"]


def test_failure_modes_check_their_arguments(session: Session) -> None:
    with pytest.raises(QueryError, match="source"):
        failure_modes(session, "robot", 30)
    for limit in (0, queries.MODES_LIMIT + 1):
        with pytest.raises(QueryError, match="limit"):
            failure_modes(session, "human", limit)


# --- search_runs -------------------------------------------------------------------------


def test_search_finds_embedded_runs_and_says_when_nothing_is_embedded(session: Session) -> None:
    from flight_recorder.search.index import embed_runs

    fake = FakeEmbedder()
    record(session, "trace-timeout", chain_failure("request timed out"), status="error")
    record(session, "trace-ok", [span("root", None, 0, input="label the docs issue")])

    empty = queries.search_runs(session, fake, "timed out", now=NOW)
    assert empty["index_empty"] is True and empty["hits"] == []
    assert empty["unembedded_runs"] == 2

    embed_runs(session, fake)
    out = queries.search_runs(session, fake, "request timed out", status="error", now=NOW)

    assert out["index_empty"] is False and out["unembedded_runs"] == 0
    [hit] = out["hits"]
    assert hit["run"]["external_id"] == "trace-timeout"
    assert hit["failed_span"] is None or hit["failed_span"]["error"] == "request timed out"


def test_search_checks_its_arguments(session: Session) -> None:
    fake = FakeEmbedder()
    bad: list[dict[str, Any]] = [
        {"query": " "},
        {"query": "x", "status": "running"},
        {"query": "x", "since": "yesterday"},
        {"query": "x", "since": "9999999w"},
        {"query": "x", "limit": 0},
        {"query": "x", "limit": queries.SEARCH_LIMIT + 1},
    ]
    for arguments in bad:
        query = arguments.pop("query")
        with pytest.raises(QueryError):
            queries.search_runs(session, fake, query, now=NOW, **arguments)
