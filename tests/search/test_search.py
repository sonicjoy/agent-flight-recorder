"""Embeddings and hybrid search (#11), written from the agreed decisions before the code.

Decisions (.review/11-search/plan.md): an `afr embed` job; root-cause failures only; a short
run summary from redacted columns; finished runs only, re-embedded on change; a fake
embedder in tests and the real model in a smoke test; one line per run in `afr search`;
vector and keyword search combined.
"""

from __future__ import annotations

import hashlib
import math
import re
import uuid
from collections.abc import Callable, Generator, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import Engine, create_engine, select, text
from sqlalchemy.orm import Session
from typer.testing import CliRunner

from flight_recorder.cli import app
from flight_recorder.redact import Redactor
from flight_recorder.store.db import upgrade
from flight_recorder.store.models import Embedding
from flight_recorder.store.read import RunFilter
from flight_recorder.store.write import RunIn, SpanIn, record_run

pytestmark = pytest.mark.db

runner = CliRunner()
REDACTOR = Redactor(b"test-key-0123456789abcdef0123456789abcdef")
T0 = datetime(2026, 11, 1, 9, 0, tzinfo=UTC)
WORD = re.compile(r"[a-z0-9]+")
SECRET = "sk-proj-" + "Ab3dE5gH7jK9mN1pQ3sT5vX7zA9cE1gI"  # an OpenAI-shaped fake


class FakeEmbedder:
    """Deterministic and offline: each word adds to one dimension, so texts that share
    words are close. Enough to test the plumbing, not real meaning (the smoke test does)."""

    model = "fake-bag-of-words"

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def _vector(self, value: str) -> list[float]:
        vector = [0.0] * 384
        for word in WORD.findall(value.lower()):
            vector[int(hashlib.sha256(word.encode()).hexdigest(), 16) % 384] += 1.0
        norm = math.sqrt(sum(v * v for v in vector)) or 1.0
        return [v / norm for v in vector]

    def documents(self, texts: Sequence[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        return [self._vector(t) for t in texts]

    def query(self, value: str) -> list[float]:
        return self._vector(value)


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
    """One API error that marks the llm, its step and the agent: one root cause."""
    return [
        span("root", None, 0, kind="agent", name="triage_agent", status="error", error=message,
             input={"messages": [{"content": "Triage issue 12: replay crashes"}]},
             output="no answer"),
        span("model", "root", 1, status="error", error=message),
        span("llm", "model", 2, kind="llm", name="ChatAnthropic", status="error", error=message),
        span("tool-ok", "root", 3, kind="tool", name="search_issues"),
    ]  # fmt: skip


@pytest.fixture
def db(make_database: Callable[[], str]) -> Generator[Session]:
    url = make_database()
    upgrade(url)
    engine: Engine = create_engine(url)
    with Session(engine) as session:
        yield session
    engine.dispose()


@pytest.fixture
def fake() -> FakeEmbedder:
    return FakeEmbedder()


def embeddings(session: Session) -> list[Embedding]:
    return list(session.scalars(select(Embedding).order_by(Embedding.kind, Embedding.content)))


# --- what gets embedded ---------------------------------------------------------------


def test_only_finished_runs_are_embedded(db: Session, fake: FakeEmbedder) -> None:
    from flight_recorder.search.index import embed_runs, pending_runs

    record(db, "finished", chain_failure("boom"))
    record(db, "open", [span("root", None, 0, ended_at=None)], ended_at=None, status="unset")

    stats = embed_runs(db, fake)

    assert {e.run.external_id for e in embeddings(db)} == {"finished"}
    assert (stats.runs, stats.open_runs) == (1, 1)
    assert pending_runs(db, fake.model) == 0  # open runs aren't pending: they aren't due yet


def test_a_chain_of_errors_gives_one_failed_span(db: Session, fake: FakeEmbedder) -> None:
    from flight_recorder.search.index import embed_runs

    record(db, "run", chain_failure("Error code: 400 - credit balance is too low"))
    embed_runs(db, fake)

    failed = [e for e in embeddings(db) if e.kind == "failed_span"]
    assert len(failed) == 1
    assert failed[0].span is not None and failed[0].span.external_id == "llm"  # the deepest


def test_independent_failures_each_get_one_and_rejections_none(
    db: Session, fake: FakeEmbedder
) -> None:
    from flight_recorder.search.index import embed_runs

    record(db, "run", [
        span("root", None, 0, kind="agent", name="session"),
        span("t1", "root", 1, kind="tool", name="fetch", status="error", error="ReadTimeout"),
        span("t2", "root", 2, kind="tool", name="Read", status="error", error="no such file"),
        span("t3", "root", 3, kind="tool", name="Bash", status="rejected", error="rejected by you"),
    ])  # fmt: skip
    embed_runs(db, fake)

    names = sorted(e.span.name for e in embeddings(db) if e.span is not None)
    assert names == ["Read", "fetch"]


def test_the_run_summary_tells_what_the_run_was(db: Session, fake: FakeEmbedder) -> None:
    from flight_recorder.search.index import embed_runs
    from flight_recorder.search.summary import MAX_CHARS

    record(db, "run-1", chain_failure("Error code: 400 - credit balance is too low"),
           name="triage issue 12", status="error")  # fmt: skip
    embed_runs(db, fake)

    (summary,) = [e for e in embeddings(db) if e.kind == "run_summary"]
    for part in ("triager", "triage issue 12", "error", "Triage issue 12: replay crashes",
                 "credit balance is too low", "search_issues"):  # fmt: skip
        assert part in summary.content, part
    assert len(summary.content) <= MAX_CHARS
    assert summary.content.count("credit balance") == 1  # one root cause, said once


def test_long_runs_are_summarised_within_the_models_reach(db: Session, fake: FakeEmbedder) -> None:
    from flight_recorder.search.index import embed_runs
    from flight_recorder.search.summary import MAX_CHARS

    spans = [span("root", None, 0, input="word " * 5_000, output="answer " * 5_000)]
    spans += [span(f"t{i}", "root", i, kind="tool", name=f"tool_{i}", status="error",
                   error="failure " * 200) for i in range(1, 30)]  # fmt: skip
    record(db, "long", spans)
    embed_runs(db, fake)

    assert all(len(e.content) <= MAX_CHARS for e in embeddings(db))


def test_embedded_text_comes_from_redacted_columns(db: Session, fake: FakeEmbedder) -> None:
    from flight_recorder.search.index import embed_runs

    record(db, "leaky", [
        span("root", None, 0, input=f"deploy with key {SECRET}", status="error",
             error=f"auth failed for {SECRET}"),
    ])  # fmt: skip
    embed_runs(db, fake)

    stored = " ".join(e.content for e in embeddings(db))
    sent = " ".join(t for call in fake.calls for t in call)
    assert SECRET not in stored and SECRET not in sent
    assert "[REDACTED:" in stored


# --- re-embedding ---------------------------------------------------------------------


def test_unchanged_runs_are_not_embedded_again(db: Session, fake: FakeEmbedder) -> None:
    from flight_recorder.search.index import embed_runs

    record(db, "run", chain_failure("boom"))
    first = embed_runs(db, fake)
    again = embed_runs(db, fake)

    assert first.vectors == 2 and again.vectors == 0 and again.unchanged == 1
    assert len(fake.calls) == 1


def test_a_changed_run_is_embedded_again_and_stale_failures_go(
    db: Session, fake: FakeEmbedder
) -> None:
    from flight_recorder.search.index import embed_runs

    record(db, "run", chain_failure("boom"))
    embed_runs(db, fake)
    # the llm is retried and succeeds: the failure is gone, the run's summary changes
    record(db, "run", [span("root", None, 0, kind="agent", name="triage_agent"),
                       span("model", "root", 1), span("llm", "model", 2, kind="llm")])  # fmt: skip
    stats = embed_runs(db, fake)

    assert stats.vectors == 1 and stats.removed == 1
    assert [e.kind for e in embeddings(db)] == ["run_summary"]


def test_vectors_from_another_model_are_left_alone(db: Session, fake: FakeEmbedder) -> None:
    from flight_recorder.search.index import embed_runs, pending_runs

    run_id = record(db, "run", chain_failure("boom"))
    db.add(Embedding(run_id=run_id, kind="run_summary", model="other-model", content="x",
                     embedding=[0.0] * 383 + [1.0]))  # fmt: skip
    db.flush()
    assert pending_runs(db, fake.model) == 1  # the other model's vector doesn't count

    embed_runs(db, fake)
    assert {e.model for e in embeddings(db)} == {"other-model", fake.model}


# --- searching ------------------------------------------------------------------------


@pytest.fixture
def corpus(db: Session, fake: FakeEmbedder) -> dict[str, uuid.UUID]:
    from flight_recorder.search.index import embed_runs

    ids = {
        "timeout": record(db, "timeout", [
            span("root", None, 0, kind="agent", name="fetcher", status="error", error="failed"),
            span("fetch", "root", 1, kind="tool", name="web_fetch", status="error",
                 error="ReadTimeout: the tool timed out while fetching the page"),
        ], name="fetch the release notes", started_at=at(0)),
        "rate": record(db, "rate", [
            span("root", None, 0, kind="llm", name="ChatAnthropic", status="error",
                 error="HTTP 429 Too Many Requests"),
        ], name="summarise the backlog", started_at=at(10), source="claude_code",
           agent="claude-code"),
        "fine": record(db, "fine", [span("root", None, 0, output="labels: bug, ui")],
                       name="label a ui bug", started_at=at(20)),
    }  # fmt: skip
    embed_runs(db, fake)
    db.commit()
    return ids


def test_search_finds_the_run_by_its_failure(
    db: Session, fake: FakeEmbedder, corpus: dict[str, uuid.UUID]
) -> None:
    from flight_recorder.search.query import search

    hits = search(db, fake, "tool timed out while fetching", RunFilter(), datetime.now(UTC))

    assert hits[0].summary.run.id == corpus["timeout"]
    assert hits[0].span_name == "web_fetch"  # the failed span was the best match
    assert "timed out" in (hits[0].span_error or "")
    assert len({h.summary.run.id for h in hits}) == len(hits)  # one line per run
    assert 0 < hits[0].similarity <= 1


def test_exact_terms_match_through_keyword_search(
    db: Session, corpus: dict[str, uuid.UUID]
) -> None:
    """A query embedder that knows nothing still finds `429`: the keyword side does."""
    from flight_recorder.search.query import search

    class Blind(FakeEmbedder):
        def query(self, value: str) -> list[float]:
            return [1.0] + [0.0] * 383  # unrelated to every stored vector

    hits = search(db, Blind(), "429", RunFilter(), datetime.now(UTC))

    assert hits[0].summary.run.id == corpus["rate"]
    assert hits[0].matched in ("words", "both")


def test_meaning_and_words_combine(db: Session, fake: FakeEmbedder, corpus: Any) -> None:
    from flight_recorder.search.query import search

    hits = search(db, fake, "429 too many requests", RunFilter(), datetime.now(UTC))
    assert hits[0].summary.run.id == corpus["rate"] and hits[0].matched == "both"


def test_fusion_rewards_agreement() -> None:
    from flight_recorder.search.query import fuse

    a, b, c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    scores = fuse([[a, b, c], [b, a]])

    assert scores[a] == scores[b] > scores[c]  # first-and-second beats only-third
    assert fuse([[a], []]) == {a: pytest.approx(1 / 61)}


def test_search_filters_and_limits(db: Session, fake: FakeEmbedder, corpus: Any) -> None:
    from flight_recorder.search.query import search

    now = datetime.now(UTC)
    only_cc = search(db, fake, "failed", RunFilter(source="claude_code"), now)
    assert {h.summary.run.id for h in only_cc} == {corpus["rate"]}
    assert len(search(db, fake, "the", RunFilter(limit=1), now)) == 1
    recent = search(db, fake, "failed", RunFilter(since=at(15)), now)
    assert [h.summary.run.id for h in recent] == [corpus["fine"]]


def test_nothing_embedded_means_no_hits(db: Session, fake: FakeEmbedder) -> None:
    from flight_recorder.search.query import search

    record(db, "run", chain_failure("boom"))
    assert search(db, fake, "boom", RunFilter(), datetime.now(UTC)) == []


# --- commands -------------------------------------------------------------------------


@pytest.fixture
def cli(
    db: Session,
    fake: FakeEmbedder,
    clean_env: pytest.MonkeyPatch,
    corpus: dict[str, uuid.UUID],
) -> Callable[..., Any]:
    import flight_recorder.search.embedder as embedder

    url = db.get_bind().engine.url.render_as_string(hide_password=False)  # type: ignore[union-attr]
    clean_env.setenv("AFR_DATABASE_URL", url)
    clean_env.setenv("COLUMNS", "160")
    clean_env.setattr(embedder, "default_embedder", lambda: fake)

    def invoke(*args: str) -> Any:
        return runner.invoke(app, list(args))

    return invoke


def test_search_command_prints_one_line_per_run(cli: Callable[..., Any]) -> None:
    result = cli("search", "tool timed out while fetching", "--limit", "2")

    assert result.exit_code == 0, result.output
    assert "fetch the release notes" in result.output
    assert "web_fetch" in result.output and "timed out" in result.output
    assert result.output.count("fetch the release notes") == 1


def test_search_says_how_many_finished_runs_are_not_embedded(
    cli: Callable[..., Any], db: Session
) -> None:
    record(db, "late", chain_failure("boom"), name="a late run")
    db.commit()

    result = cli("search", "boom")
    assert "1 settled run isn't embedded yet; run `afr embed`" in result.output


def test_search_only_reads(cli: Callable[..., Any], db: Session) -> None:
    before = db.scalar(text("SELECT count(*) FROM embeddings"))
    record(db, "late", chain_failure("boom"))
    db.commit()
    cli("search", "boom")
    assert db.scalar(text("SELECT count(*) FROM embeddings")) == before


def test_embed_command_reports_what_it_did(cli: Callable[..., Any], db: Session) -> None:
    record(db, "late", chain_failure("boom"))
    record(db, "open", [span("root", None, 0, ended_at=None)], ended_at=None, status="unset")
    db.commit()

    result = cli("embed")
    assert result.exit_code == 0, result.output
    assert "embedded 1 run (2 vectors)" in result.output
    assert "3 unchanged" in result.output and "1 still running" in result.output


def test_search_rejects_an_empty_query_and_escapes_text(
    cli: Callable[..., Any], db: Session, fake: FakeEmbedder
) -> None:
    from flight_recorder.search.index import embed_runs

    assert cli("search", "  ").exit_code == 2
    record(db, "nasty", [span("root", None, 0, status="error", error="boom \x1b[2J")],
           name="nasty \x1b]0;owned\x07")  # fmt: skip
    embed_runs(db, fake)
    db.commit()
    result = cli("search", "boom")
    assert "\x1b" not in result.output and "\\x1b[2J" in result.output


def test_migration_adds_a_keyword_index(db: Session) -> None:
    indexes = db.scalars(
        text("SELECT indexdef FROM pg_indexes WHERE tablename = 'embeddings'")
    ).all()
    assert any("gin" in i.lower() and "content_tsv" in i for i in indexes)


# --- the real model -------------------------------------------------------------------


@pytest.mark.smoke
def test_the_real_model_matches_meaning_not_just_words() -> None:
    """Downloads bge-small (about 67 MB) once into AFR_HOME/models."""
    from flight_recorder.search.embedder import default_embedder
    from flight_recorder.store.models import EMBEDDING_DIMENSIONS

    model = default_embedder()
    timeout, quota, ui = model.documents([
        "web_fetch failed: ReadTimeout: HTTPSConnectionPool read timed out",
        "HTTP 429 Too Many Requests: rate limit exceeded",
        "labelled the issue as a ui bug",
    ])  # fmt: skip
    query = model.query("tool timed out while fetching")

    def similarity(a: list[float], b: list[float]) -> float:
        return sum(x * y for x, y in zip(a, b, strict=True))

    assert len(query) == EMBEDDING_DIMENSIONS
    assert similarity(query, timeout) > similarity(query, quota)
    assert similarity(query, timeout) > similarity(query, ui)


def test_each_model_embeds_on_its_own(db: Session, fake: FakeEmbedder) -> None:
    """A second model doesn't treat the first model's identical texts as its own."""
    from flight_recorder.search.index import embed_runs

    record(db, "run", chain_failure("boom"))
    embed_runs(db, fake)
    assert embed_runs(db, OtherModel()).vectors == 2


def test_search_ignores_other_models_vectors(db: Session, fake: FakeEmbedder) -> None:
    from flight_recorder.search.index import embed_runs
    from flight_recorder.search.query import search

    record(db, "run", chain_failure("boom"))
    embed_runs(db, OtherModel())
    assert search(db, fake, "boom", RunFilter(), datetime.now(UTC)) == []


# --- review round 1: tests written before the fixes ----------------------------------


class OtherModel(FakeEmbedder):
    model = "another-fake"


def age_run(session: Session, run_id: uuid.UUID, hours: float) -> None:
    """Make a run and its spans look untouched for `hours` (triggers off for the update)."""
    session.execute(text("SET LOCAL session_replication_role = replica"))
    for table, column in (("runs", "id"), ("spans", "run_id")):
        session.execute(
            text(
                f"UPDATE {table} SET updated_at = updated_at - make_interval(hours => :h) "
                f"WHERE {column} = :id"
            ),
            {"h": hours, "id": run_id},
        )
    session.execute(text("SET LOCAL session_replication_role = origin"))


def test_claude_code_summaries_take_the_prompt_and_reply_from_turns(
    db: Session, fake: FakeEmbedder
) -> None:
    from flight_recorder.search.index import embed_runs

    record(db, "session", [
        span("root", None, 0, kind="agent", name="Claude Code session"),  # no input, no output
        span("turn-1", "root", 1, name="turn", input="Fix the flaky replay test",
             output="Looking at it"),
        span("turn-2", "root", 5, name="turn", input="and the docs",
             output="Both done: the test waits for the span"),
    ], source="claude_code", agent="claude-code")  # fmt: skip
    embed_runs(db, fake)

    (summary,) = [e for e in embeddings(db) if e.kind == "run_summary"]
    assert "asked: Fix the flaky replay test" in summary.content
    assert "answered: Both done: the test waits for the span" in summary.content


def test_the_limit_is_honoured_beyond_the_candidate_pool(db: Session, fake: FakeEmbedder) -> None:
    from flight_recorder.search.index import embed_runs
    from flight_recorder.search.query import search

    for i in range(60):
        record(db, f"run-{i}", [span("root", None, 0, output=f"answer {i}")],
               started_at=at(i))  # fmt: skip
    embed_runs(db, fake)

    assert len(search(db, fake, "answer", RunFilter(limit=60), datetime.now(UTC))) == 60


def test_a_negation_only_query_doesnt_count_as_a_word_match(
    db: Session, fake: FakeEmbedder, corpus: dict[str, uuid.UUID]
) -> None:
    from flight_recorder.search.query import search

    hits = search(db, fake, "-timeout", RunFilter(), datetime.now(UTC))
    assert hits and all(h.matched == "meaning" for h in hits)


def test_filters_that_exclude_everything_say_no_matching_runs(
    cli: Callable[..., Any], db: Session
) -> None:
    record(db, "late", chain_failure("boom"))  # finished, not embedded: one is pending
    db.commit()

    result = cli("search", "boom", "--agent", "nobody")
    assert "no matching runs" in result.output and "nothing is embedded" not in result.output


def test_search_filters_by_agent_until_and_status(
    db: Session, fake: FakeEmbedder, corpus: dict[str, uuid.UUID]
) -> None:
    from flight_recorder.search.query import search

    now = datetime.now(UTC)

    def found(where: RunFilter) -> set[uuid.UUID]:
        return {h.summary.run.id for h in search(db, fake, "the", where, now)}

    assert found(RunFilter(agent="claude-code")) == {corpus["rate"]}
    assert found(RunFilter(until=at(5))) == {corpus["timeout"]}
    assert found(RunFilter(status="ok")) == set(corpus.values())  # run status, not span status


def test_unfinished_runs_are_embedded_but_running_ones_wait(
    db: Session, fake: FakeEmbedder
) -> None:
    from flight_recorder.search.index import embed_runs, pending_runs

    open_ = [span("root", None, 0, status="error", error="crashed mid-run", ended_at=None)]
    crashed = record(db, "crashed", open_, ended_at=None, status="unset")
    record(db, "running", open_, ended_at=None, status="unset")
    age_run(db, crashed, hours=2)

    assert pending_runs(db, fake.model) == 1  # the crashed one is due; the running one isn't
    stats = embed_runs(db, fake)

    assert {e.run.external_id for e in embeddings(db)} == {"crashed"}
    assert stats.open_runs == 1  # still running
    (summary,) = [e for e in embeddings(db) if e.kind == "run_summary"]
    assert "status: unfinished" in summary.content


def test_search_status_accepts_unfinished_but_not_running(cli: Callable[..., Any]) -> None:
    assert cli("search", "boom", "--status", "unfinished").exit_code == 0
    refused = cli("search", "boom", "--status", "running")
    assert refused.exit_code == 2 and "unfinished" in refused.output


def test_refused_calls_are_in_the_summary(db: Session, fake: FakeEmbedder) -> None:
    from flight_recorder.search.index import embed_runs

    record(db, "run", [
        span("root", None, 0, kind="agent", name="session"),
        span("t1", "root", 1, kind="tool", name="Bash", status="rejected",
             error="rejected by a safety check: denied"),
    ])  # fmt: skip
    embed_runs(db, fake)

    (summary,) = [e for e in embeddings(db)]
    assert "refused: tool Bash (rejected by a safety check: denied)" in summary.content


def test_failure_vectors_are_capped_per_run(db: Session, fake: FakeEmbedder) -> None:
    from flight_recorder.search.index import MAX_FAILURES, embed_runs

    spans = [span("root", None, 0, kind="agent", name="session")]
    spans += [span(f"t{i:02d}", "root", i, kind="tool", name=f"tool_{i:02d}", status="error",
                   error=f"failure {i}") for i in range(1, 31)]  # fmt: skip
    record(db, "noisy", spans)
    embed_runs(db, fake)

    failed = sorted(e.span.name for e in embeddings(db) if e.span is not None)
    assert len(failed) == MAX_FAILURES == 20
    assert failed[0] == "tool_01"  # the earliest ones


def test_a_run_with_no_spans_still_gets_a_summary(db: Session, fake: FakeEmbedder) -> None:
    from flight_recorder.search.index import embed_runs

    record(db, "bare", [], name="a run with nothing recorded")
    embed_runs(db, fake)

    (summary,) = embeddings(db)
    assert "a run with nothing recorded" in summary.content


def test_run_and_span_names_are_redacted_too(db: Session, fake: FakeEmbedder) -> None:
    from flight_recorder.search.index import embed_runs

    record(db, "leaky-names", [
        span("root", None, 0, name=f"call with {SECRET}", status="error", error="boom"),
    ], name=f"deploy {SECRET}")  # fmt: skip
    embed_runs(db, fake)

    assert all(SECRET not in e.content for e in embeddings(db))


def test_the_model_download_is_pinned_to_a_revision(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    import flight_recorder.search.embedder as embedder

    asked: dict[str, Any] = {}

    def snapshot_download(**kwargs: Any) -> str:
        asked.update(kwargs)
        return str(tmp_path)

    monkeypatch.setattr("huggingface_hub.snapshot_download", snapshot_download)
    assert embedder.model_dir(tmp_path) == tmp_path
    assert asked["repo_id"] == embedder.MODEL_REPO == "Qdrant/bge-small-en-v1.5-onnx-Q"
    assert re.fullmatch(r"[0-9a-f]{40}", asked["revision"])
    assert asked["revision"] == embedder.MODEL_REVISION


def test_a_cached_model_loads_without_the_network(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    from huggingface_hub.errors import LocalEntryNotFoundError

    import flight_recorder.search.embedder as embedder

    calls: list[bool] = []

    def cached(**kwargs: Any) -> str:
        calls.append(kwargs["local_files_only"])
        return str(tmp_path)

    monkeypatch.setattr("huggingface_hub.snapshot_download", cached)
    embedder.model_dir(tmp_path)
    assert calls == [True]  # found locally: no download attempted

    calls.clear()

    def missing(**kwargs: Any) -> str:
        calls.append(kwargs["local_files_only"])
        if kwargs["local_files_only"]:
            raise LocalEntryNotFoundError("not cached")
        return str(tmp_path)

    monkeypatch.setattr("huggingface_hub.snapshot_download", missing)
    embedder.model_dir(tmp_path)
    assert calls == [True, False]  # then downloaded, pinned
