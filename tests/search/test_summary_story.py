# ruff: noqa: F811  (fixtures imported from test_search)
"""Summaries of LangChain runs say what the run was asked and answered (#14), so `afr search`
can find a triager run by its issue, decision or rationale.

Written from .review/14-search-summaries/spec.md before the code.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.orm import Session
from test_search import FakeEmbedder, db, embeddings, fake, record, span  # noqa: F401

pytestmark = pytest.mark.db

SYSTEM = "You triage GitHub issues. SYSTEM-PROMPT-TEXT"
ISSUE = (
    "Triage this issue.\n\nTitle: Containerise the API and MCP server\n\nRun via docker compose."
)


def message(kind: str, content: Any) -> dict[str, Any]:
    name = {"system": "SystemMessage", "human": "HumanMessage"}[kind]
    return {"id": ["langchain", "schema", "messages", name], "lc": 1, "type": "constructor",
            "kwargs": {"type": kind, "content": content}}  # fmt: skip


def prompt(*messages: dict[str, Any]) -> dict[str, Any]:
    return {"messages": [list(messages)]}


def answer(content: str = "", calls: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    kwargs = {"type": "ai", "content": content, "tool_calls": calls or []}
    message = {"id": ["langchain", "schema", "messages", "AIMessage"], "kwargs": kwargs}
    return {"type": "LLMResult", "generations": [[{"text": content, "message": message}]]}


def call(name: str, **args: Any) -> dict[str, Any]:
    return {"id": f"c-{name}", "name": name, "args": args, "type": "tool_call"}


DECISION = call("TriageDecision", labels=["infra", "mcp"], milestone="Week 4",
                rationale="containerises the MCP server with docker compose")  # fmt: skip


def langgraph_run(db: Session, *, final_text: str = "Triage: looks like infra work") -> None:
    """A LangGraph triager run: the root records nothing; the model calls hold the story."""
    record(db, "lg-1", [
        span("root", None, 0, kind="agent", name="issue_triager"),
        span("llm1", "root", 1, kind="llm", name="ChatOpenAI",
             input=prompt(message("system", SYSTEM), message("human", ISSUE)),
             output=answer(calls=[call("search_issues", query="containerise")])),
        span("tool1", "root", 2, kind="tool", name="search_issues", input="containerise",
             output={"data": {"content": "[]"}}),
        span("llm2", "root", 3, kind="llm", name="ChatOpenAI",
             input=prompt(message("human", ISSUE)), output=answer(calls=[DECISION])),
        span("llm3", "root", 4, kind="llm", name="ChatOpenAI",
             input=prompt(message("human", "Draft a comment")), output=answer(final_text)),
    ], name="triage #26")  # fmt: skip


def summary(db: Session) -> str:
    (found,) = [e.content for e in embeddings(db) if e.kind == "run_summary"]
    return found


def test_a_langgraph_runs_summary_says_what_it_was_asked_and_decided(
    db: Session, fake: FakeEmbedder
) -> None:
    from flight_recorder.search.index import embed_runs

    langgraph_run(db)
    embed_runs(db, fake)

    text = summary(db)
    assert "asked: Triage this issue. Title: Containerise the API and MCP server" in text
    assert "answered: TriageDecision(labels: infra, mcp; milestone: Week 4;" in text
    assert "rationale: containerises the MCP server with docker compose" in text
    assert "SYSTEM-PROMPT-TEXT" not in text  # the human message, not the system prompt
    assert "Triage: looks like infra work" not in text  # the decision, not a later comment


def test_without_structured_output_the_answer_is_the_last_model_call(
    db: Session, fake: FakeEmbedder
) -> None:
    from flight_recorder.search.index import embed_runs

    record(db, "lg-2", [
        span("root", None, 0, kind="agent", name="agent"),
        span("llm1", "root", 1, kind="llm", name="m", input=prompt(message("human", "Fix it")),
             output=answer(calls=[call("search_issues", query="x")])),
        span("tool1", "root", 2, kind="tool", name="search_issues", input="x", output="none"),
        span("llm2", "root", 3, kind="llm", name="m", input=prompt(message("human", "Fix it")),
             output=answer("Done: it was a typo")),
    ])  # fmt: skip
    embed_runs(db, fake)

    assert 'answered: "Done: it was a typo"' in summary(db)


def test_an_entry_span_with_content_is_summarised_as_before(
    db: Session, fake: FakeEmbedder
) -> None:
    from flight_recorder.search.index import embed_runs

    record(db, "lg-3", [
        span("root", None, 0, kind="agent", name="agent", input={"q": "from the entry span"},
             output="entry answer"),
        span("llm1", "root", 1, kind="llm", name="m",
             input=prompt(message("human", "from the model call")), output=answer("model said")),
    ])  # fmt: skip
    embed_runs(db, fake)

    text = summary(db)
    assert "asked: from the entry span" in text and "answered: entry answer" in text
    assert "from the model call" not in text


def test_the_summary_stays_within_its_caps(db: Session, fake: FakeEmbedder) -> None:
    from flight_recorder.search.index import embed_runs
    from flight_recorder.search.summary import MAX_CHARS

    long = call("TriageDecision", rationale="word " * 2_000)
    record(db, "lg-4", [
        span("root", None, 0, kind="agent", name="agent"),
        span("llm1", "root", 1, kind="llm", name="m",
             input=prompt(message("human", "issue " * 2_000)), output=answer(calls=[long])),
    ])  # fmt: skip
    embed_runs(db, fake)

    text = summary(db)
    assert len(text) <= MAX_CHARS
    asked = next(line for line in text.splitlines() if line.startswith("asked: "))
    answered = next(line for line in text.splitlines() if line.startswith("answered: "))
    assert len(asked) <= len("asked: ") + 500
    assert len(answered) <= len("answered: ") + 400


def test_the_label_screen_and_search_share_one_parser() -> None:
    import flight_recorder.cli.story as story
    import flight_recorder.payloads as payloads

    assert story.human is payloads.human
    assert story.model_answer is payloads.model_answer


# --- review round 1 ------------------------------------------------------------------


def test_a_failed_last_model_call_isnt_the_answer(db: Session, fake: FakeEmbedder) -> None:
    from flight_recorder.search.index import embed_runs

    failed = {"type": "LLMResult", "generations": [], "llm_output": {"id": "req-123"}}
    record(db, "lg-5", [
        span("root", None, 0, kind="agent", name="agent"),
        span("llm1", "root", 1, kind="llm", name="m", input=prompt(message("human", "Fix it")),
             output=answer("Looking into it")),
        span("llm2", "root", 2, kind="llm", name="m", status="error", error="rate limited",
             input=prompt(message("human", "Fix it")), output=failed),
    ])  # fmt: skip
    embed_runs(db, fake)

    text = summary(db)
    assert 'answered: "Looking into it"' in text  # the last call that answered
    assert "req-123" not in text


def test_runs_that_arent_langchain_get_no_guessed_answer(db: Session, fake: FakeEmbedder) -> None:
    from flight_recorder.search.index import embed_runs

    raw = {"id": "resp_abc", "output": [{"type": "message", "text": "final_answer completed"}]}
    record(db, "oa-1", [
        span("root", None, 0, kind="agent", name="agent"),
        span("llm1", "root", 1, kind="llm", name="openai", input={"input": "hi"}, output=raw),
    ])  # fmt: skip
    embed_runs(db, fake)

    text = summary(db)
    assert "answered:" not in text and "resp_abc" not in text


def test_the_last_of_several_structured_outputs_wins(db: Session, fake: FakeEmbedder) -> None:
    from flight_recorder.search.index import embed_runs

    first = call("TriageDecision", labels=["evals"], rationale="first try")
    second = call("TriageDecision", labels=["recorder"], rationale="after feedback")
    record(db, "lg-6", [
        span("root", None, 0, kind="agent", name="agent"),
        span("llm1", "root", 1, kind="llm", name="m", input=prompt(message("human", "Triage")),
             output=answer(calls=[first])),
        span("llm2", "root", 2, kind="llm", name="m", input=prompt(message("human", "Again")),
             output=answer(calls=[second])),
    ])  # fmt: skip
    embed_runs(db, fake)

    text = summary(db)
    assert "rationale: after feedback" in text and "first try" not in text


def test_an_entry_with_input_but_no_output_gets_its_answer_from_the_model(
    db: Session, fake: FakeEmbedder
) -> None:
    from flight_recorder.search.index import embed_runs

    record(db, "lg-7", [
        span("root", None, 0, kind="agent", name="agent", input={"q": "entry question"}),
        span("llm1", "root", 1, kind="llm", name="m", input=prompt(message("human", "model q")),
             output=answer(calls=[DECISION])),
    ])  # fmt: skip
    embed_runs(db, fake)

    text = summary(db)
    assert "asked: entry question" in text and "model q" not in text
    assert "answered: TriageDecision(" in text
