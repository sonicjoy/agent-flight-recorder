"""The label screen (#14): both sides named on every row, and the run's story (what the
agent was asked, what it called and got back, what it answered) so a labeller can judge why.

Written from .review/14-label-screen/spec.md before the code.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable, Generator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from rich.console import Console
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from typer.testing import CliRunner

from flight_recorder.cli import app, render
from flight_recorder.redact import Redactor
from flight_recorder.store.db import upgrade
from flight_recorder.store.read import SpanContent, SpanNode, build_tree
from flight_recorder.store.write import RunIn, SpanIn, record_run

T0 = datetime(2026, 11, 13, 9, 0, tzinfo=UTC)
ISSUE = (
    "Triage this issue.\n\nTitle: Add retries when the provider returns 503\n\nIt used to retry."
)
SYSTEM = "You triage GitHub issues. SECRET-SYSTEM-PROMPT"


def message(kind: str, content: Any) -> dict[str, Any]:
    name = {"system": "SystemMessage", "human": "HumanMessage", "ai": "AIMessage"}[kind]
    return {"id": ["langchain", "schema", "messages", name], "lc": 1, "type": "constructor",
            "kwargs": {"type": kind, "content": content}}  # fmt: skip


def prompt(*messages: dict[str, Any]) -> dict[str, Any]:
    return {"messages": [list(messages)]}


def answer(content: str = "", tool_calls: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    kwargs = {"type": "ai", "content": content, "tool_calls": tool_calls or []}
    return {"type": "LLMResult", "generations": [[{
        "text": content, "type": "ChatGeneration",
        "message": {"id": ["langchain", "schema", "messages", "AIMessage"], "lc": 1,
                    "type": "constructor", "kwargs": kwargs},
    }]]}  # fmt: skip


def call(name: str, **args: Any) -> dict[str, Any]:
    return {"id": f"call_{name}", "name": name, "args": args, "type": "tool_call"}


def tool_result(content: Any) -> dict[str, Any]:
    body = content if isinstance(content, str) else json.dumps(content)
    return {"data": {"name": "search_issues", "type": "tool", "status": "success", "content": body}}


HITS = [
    {"id": "2", "title": "Provider-switchable LLM config", "labels": ["demo-agent"],
     "milestone": "Week 1: Demo agent and foundations", "body": "long body " * 50},
    {"id": "21", "title": "Provider comparison on the golden set", "labels": ["evals", "stretch"],
     "milestone": "Week 3: Evals and guardrails"},
]  # fmt: skip
RATIONALE = "Request is to add retry/backoff behavior on transient 503s."
DECISION = call("TriageDecision", labels=["recorder", "evals", "stretch"],
                milestone="Week 3: Evals and guardrails", duplicate_of=None,
                rationale=RATIONALE)  # fmt: skip


def spans(*specs: tuple[str, str, Any, Any]) -> tuple[list[SpanNode], dict[uuid.UUID, Any]]:
    """(kind, name, input, output) under one agent root, a second apart, and their content."""
    root = node("agent", "triage", 0, None)
    nodes, contents = [root], {root.id: SpanContent(None, None)}
    for i, (kind, name, given, got) in enumerate(specs, 1):
        child = node(kind, name, i, root.id)
        nodes.append(child)
        contents[child.id] = SpanContent(given, got)
    return build_tree(nodes), contents


def node(kind: str, name: str, second: int, parent: uuid.UUID | None, **extra: Any) -> SpanNode:
    at = T0 + timedelta(seconds=second)
    return SpanNode(id=uuid.uuid4(), external_id=f"s{second}", parent_id=parent, kind=kind,
                    name=name, model=None, status=extra.get("status", "ok"),
                    error=extra.get("error"), started_at=at, ended_at=at, input_tokens=None,
                    output_tokens=None, cost_usd=None, usage={})  # fmt: skip


def shown(renderable: Any) -> str:
    out = Console(record=True, width=2_000, force_terminal=False)  # no wrapping
    out.print(renderable)
    return out.export_text()


def lookup(contents: dict[uuid.UUID, Any]) -> Callable[[uuid.UUID], SpanContent]:
    return lambda span_id: contents[span_id]


# --- agent vs truth ------------------------------------------------------------------


def test_every_row_names_the_agents_side_and_the_truth() -> None:
    rows = shown(render.decision_rows({
        "triager.decided.labels": ["evals", "recorder"], "triager.expected.labels": ["demo-agent"],
        "triager.decided.milestone": "Week 3", "triager.expected.milestone": "Week 3",
    }))  # fmt: skip

    labels = next(line for line in rows.splitlines() if "labels" in line)
    milestone = next(line for line in rows.splitlines() if "milestone" in line)
    assert (
        labels.index("agent evals, recorder") < labels.index("≠") < labels.index("truth demo-agent")
    )
    assert "agent Week 3" in milestone and "=" in milestone and "truth Week 3" in milestone
    assert "decided · expected" not in rows


def test_other_attributes_leave_out_the_pairs_and_sdk_noise() -> None:
    rest = shown(render.other_attributes({
        "triager.decided.labels": ["a"], "triager.expected.labels": ["b"],
        "triager.case": "s65", "telemetry.sdk.name": "opentelemetry",
    }))  # fmt: skip

    assert "triager.case: s65" in rest
    assert "decided" not in rest and "telemetry" not in rest


def test_a_paired_attribute_key_is_escaped() -> None:
    evil = "\x1b]0;owned\x07x"

    assert "\x1b" not in shown(
        render.decision_rows({f"{evil}.decided.f": "a", f"{evil}.expected.f": "b"})
    )


# --- task ----------------------------------------------------------------------------


def test_the_task_is_the_first_model_calls_last_human_message() -> None:
    from flight_recorder.cli import story

    roots, contents = spans(
        ("llm", "ChatOpenAI", prompt(message("system", SYSTEM), message("human", ISSUE)),
         answer(tool_calls=[call("search_issues", query="503")])),
        ("llm", "ChatOpenAI", prompt(message("human", "a later prompt")), answer("done")),
    )  # fmt: skip

    task = shown(story.task(roots, lookup(contents)))

    assert "Title: Add retries when the provider returns 503" in task
    assert "It used to retry." in task.replace("\n", " ") or "It used to retry." in task
    assert "SECRET-SYSTEM-PROMPT" not in task and "a later prompt" not in task


def test_a_human_message_in_content_blocks_is_joined() -> None:
    from flight_recorder.cli import story

    blocks = [{"type": "text", "text": "Title: blocks"}, {"type": "image", "url": "x"}]
    roots, contents = spans(("llm", "m", prompt(message("human", blocks)), answer("ok")))

    assert "Title: blocks" in shown(story.task(roots, lookup(contents)))


def test_without_a_model_call_the_task_is_the_entry_spans_input() -> None:
    from flight_recorder.cli import story

    root = node("agent", "triage", 0, None)
    roots = build_tree([root])
    contents = {root.id: SpanContent({"issue": {"title": "From the entry span"}}, None)}

    assert "From the entry span" in shown(story.task(roots, lookup(contents)))


def test_no_task_at_all_is_none() -> None:
    from flight_recorder.cli import story

    roots, contents = spans(("tool", "t", None, None))

    assert story.task(roots, lookup(contents)) is None


def test_a_long_task_is_cut() -> None:
    from flight_recorder.cli import story

    long = "\n".join(f"line {i} " + "x" * 200 for i in range(40))
    roots, contents = spans(("llm", "m", prompt(message("human", long)), answer("ok")))

    task = shown(story.task(roots, lookup(contents)))

    assert len(task) < 1_400 and "line 0" in task and "line 39" not in task


# --- steps ---------------------------------------------------------------------------


def test_steps_show_tool_calls_results_and_answers_in_order() -> None:
    from flight_recorder.cli import story

    roots, contents = spans(
        ("llm", "ChatOpenAI", prompt(message("human", ISSUE)),
         answer(tool_calls=[call("search_issues", query="503 retries provider")])),
        ("tool", "search_issues", "503 retries provider", tool_result(HITS)),
        ("llm", "ChatOpenAI", prompt(message("human", ISSUE)), answer(tool_calls=[DECISION])),
        ("llm", "ChatOpenAI", prompt(message("human", "draft")),
         answer("Triage: Please consider adding retry/backoff handling.")),
    )  # fmt: skip

    out = shown(story.steps(roots, lookup(contents)))
    lines = [line for line in out.splitlines() if line.strip()]

    assert "model → search_issues(query: 503 retries provider)" in lines[0]
    assert "tool search_issues(503 retries provider) →" in lines[1]
    assert (
        "#2 Provider-switchable LLM config (demo-agent; Week 1: Demo agent and foundations)"
        in lines[1]
    )
    assert "#21 Provider comparison on the golden set (evals, stretch; Week 3" in lines[1]
    assert "long body" not in out  # bodies of hits aren't shown
    assert "TriageDecision(labels: recorder, evals, stretch; milestone: Week 3" in lines[2]
    assert "rationale: Request is to add retry/backoff" in lines[2]
    assert '"Triage: Please consider adding retry/backoff handling."' in lines[3]
    assert [line.split()[0] for line in lines] == ["1", "2", "3", "4"]


def test_many_steps_are_folded() -> None:
    from flight_recorder.cli import story

    roots, contents = spans(*[("llm", "m", None, answer(f"answer {i}")) for i in range(20)])

    out = shown(story.steps(roots, lookup(contents)))

    assert "answer 11" in out and "answer 12" not in out
    assert "… 8 more" in out


def test_unknown_shapes_fall_back_to_their_text() -> None:
    from flight_recorder.cli import story

    roots, contents = spans(
        ("tool", "Bash", {"command": "ls -la"}, {"stdout": "file-a file-b"}),
        ("llm", "claude", None, {"said": {"text": "plain words"}}),
    )  # fmt: skip

    out = shown(story.steps(roots, lookup(contents)))

    assert "tool Bash(ls -la) → file-a file-b" in out
    assert "model → plain words" in out


def test_an_error_step_shows_its_error() -> None:
    from flight_recorder.cli import story

    root = node("agent", "triage", 0, None)
    failed = node("tool", "search_issues", 1, root.id, status="error", error="timed out")
    roots = build_tree([root, failed])
    contents = {root.id: SpanContent(None, None), failed.id: SpanContent("q", None)}

    assert "timed out" in shown(story.steps(roots, lookup(contents)))


def test_no_steps_is_none() -> None:
    from flight_recorder.cli import story

    root = node("agent", "triage", 0, None)

    assert story.steps(build_tree([root]), lookup({root.id: SpanContent(None, None)})) is None


def test_recorded_text_reaches_the_terminal_escaped() -> None:
    from flight_recorder.cli import story

    evil = "\x1b]0;owned\x07\x1b[2J"
    roots, contents = spans(
        ("llm", "m", prompt(message("human", f"Title: {evil}")),
         answer(tool_calls=[call(f"t{evil}", q=evil)])),
        ("tool", f"t{evil}", evil, tool_result([{"id": evil, "title": evil}])),
    )  # fmt: skip

    out = shown(story.task(roots, lookup(contents))) + shown(story.steps(roots, lookup(contents)))

    assert "\x1b" not in out and "\x07" not in out


# --- the whole screen ------------------------------------------------------------------


@pytest.fixture(scope="module")
def store(make_database: Callable[[], str]) -> Generator[tuple[str, uuid.UUID]]:
    url = make_database()
    upgrade(url)
    engine = create_engine(url)
    attributes = {"triager.decided.labels": ["evals"], "triager.expected.labels": ["demo-agent"],
                  "triager.case": "s65", "triager.case_source": "synthetic",
                  "triager.grade": "wrong"}  # fmt: skip
    with Session(engine) as session, session.begin():
        run = record_run(
            session,
            RunIn(
                source="otel",
                external_id="trace-story",
                agent="triager",
                name="triage s65",
                status="ok",
                started_at=T0,
                ended_at=T0 + timedelta(seconds=5),
                attributes=attributes,
            ),
            [
                SpanIn(
                    external_id="root",
                    kind="agent",
                    name="triage",
                    started_at=T0,
                    ended_at=T0 + timedelta(seconds=5),
                    status="ok",
                ),
                SpanIn(
                    external_id="llm1",
                    parent_external_id="root",
                    kind="llm",
                    name="ChatOpenAI",
                    started_at=T0 + timedelta(seconds=1),
                    status="ok",
                    input=prompt(message("system", SYSTEM), message("human", ISSUE)),
                    output=answer(tool_calls=[call("search_issues", query="503")]),
                ),
                SpanIn(
                    external_id="tool1",
                    parent_external_id="root",
                    kind="tool",
                    name="search_issues",
                    started_at=T0 + timedelta(seconds=2),
                    status="ok",
                    input="503",
                    output=tool_result(HITS),
                ),
                SpanIn(
                    external_id="llm2",
                    parent_external_id="root",
                    kind="llm",
                    name="ChatOpenAI",
                    started_at=T0 + timedelta(seconds=3),
                    status="ok",
                    input=prompt(message("human", ISSUE)),
                    output=answer(tool_calls=[DECISION]),
                ),
            ],
            redactor=Redactor(b"test-key-0123456789abcdef0123456789abcdef"),
        )
    yield url, run
    engine.dispose()


def test_the_label_screen_reads_top_to_bottom(
    store: tuple[str, uuid.UUID], clean_env: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    clean_env.setenv("AFR_DATABASE_URL", store[0])
    clean_env.setenv("AFR_HOME", str(tmp_path))
    clean_env.setenv("COLUMNS", "200")

    result = CliRunner().invoke(app, ["label", str(store[1]), "--mode", "m"])

    out = result.output
    assert result.exit_code == 0, out
    sections = ["agent vs truth", "task", "steps", "attributes"]
    assert [out.index(s) for s in sections] == sorted(out.index(s) for s in sections)
    assert "agent evals" in out and "truth demo-agent" in out
    assert "agent vs truth · truth from a synthetic case (s65)" in out
    assert "Title: Add retries when the provider returns 503" in out
    assert "#2 Provider-switchable LLM config" in out
    assert "rationale: Request is to add retry/backoff" in out
    assert "SECRET-SYSTEM-PROMPT" not in out


# --- where the truth comes from --------------------------------------------------------


def test_a_synthetic_truth_says_so_with_its_case() -> None:
    source = render.truth_source({
        "triager.decided.labels": ["a"], "triager.expected.labels": ["b"],
        "triager.case_source": "synthetic", "triager.case": "s65",
    })  # fmt: skip

    assert source is not None
    assert shown(source).strip() == "truth from a synthetic case (s65)"
    assert any("yellow" in str(span.style) for span in source.spans)


def test_a_real_truth_says_so_too() -> None:
    source = render.truth_source({
        "triager.decided.labels": ["a"], "triager.expected.labels": ["b"],
        "triager.case_source": "real", "triager.case": "28",
    })  # fmt: skip

    assert source is not None and shown(source).strip() == "truth from a real case (28)"


def test_no_case_source_no_mention() -> None:
    assert render.truth_source({"triager.decided.labels": ["a"],
                                "triager.expected.labels": ["b"]}) is None  # fmt: skip
