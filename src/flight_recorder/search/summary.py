"""The texts that get embedded: a short summary of each run, and one text for each root-cause
failure in it.

Everything comes from columns that were redacted at ingest. bge-small reads 512 tokens, about
2,000 characters of English, so each text is kept within MAX_CHARS, most telling parts
first. Each part is cut on its own; the final cap is only a backstop.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Any, Literal
from uuid import UUID

from sqlalchemy.orm import Session

from flight_recorder.payloads import (
    as_dict,
    call_line,
    flat_text,
    human,
    langchain_answer,
    tool_calls,
)
from flight_recorder.store.models import Run
from flight_recorder.store.read import SpanNode, span_content, span_tree

MAX_CHARS = 2_000
NAME_CHARS = 200
INPUT_CHARS = 500
OUTPUT_CHARS = 400
ERROR_CHARS = 300
MAX_IN_SUMMARY = 3  # failures, and refused calls, listed in a run's summary
MAX_FAILURES = 20  # failure vectors per run: a runaway run can't flood the index
MAX_TOOLS = 10

Kind = Literal["run_summary", "failed_span"]


@dataclass(frozen=True)
class Document:
    kind: Kind
    span_id: UUID | None
    content: str


def _all(roots: list[SpanNode]) -> list[SpanNode]:
    nodes = [node for root in roots for node in root.walk()]
    return sorted(nodes, key=lambda n: (n.started_at, n.external_id))


def root_failures(roots: list[SpanNode]) -> list[SpanNode]:
    """Error spans with no failed child, in time order. One API error usually marks the
    llm, its step and its agent; only the deepest says what happened. Rejected calls
    (refused permission) aren't failures."""
    return [
        node
        for node in _all(roots)
        if node.status == "error" and not any(c.status == "error" for c in node.children)
    ]


def _cap(lines: list[str]) -> str:
    return "\n".join(line for line in lines if line)[:MAX_CHARS]


TURN = "turn"  # a Claude Code turn span (ingest/claude_code.py)


def _conversation(session: Session, run: Run, roots: list[SpanNode]) -> tuple[Any, Any]:
    """What the run was asked and what it answered. The entry span has them; a Claude Code
    session whose entry span lacks them (it crashed, or events were lost) has them on its
    turns: the first prompt, the last reply. Never another child: a compaction's summary or a
    tool's result isn't the run's answer."""
    if not roots:
        return None, None
    entry = roots[0]
    found = span_content(session, entry.id)
    asked, answered = found.input, found.output
    if run.source != "claude_code":
        if asked and answered:
            return asked, answered
        model_asked, model_answered = _from_model_calls(session, roots)
        return asked or model_asked, answered or model_answered
    turns = sorted(
        (c for c in entry.children if c.name == TURN), key=lambda n: (n.started_at, n.external_id)
    )
    if not asked:
        asked = next((i for t in turns if (i := span_content(session, t.id).input)), None)
    if not answered:
        answered = next(
            (o for t in reversed(turns) if (o := span_content(session, t.id).output)), None
        )
    return asked, answered


def _from_model_calls(session: Session, roots: list[SpanNode]) -> tuple[Any, Any]:
    """What a LangChain run was asked and answered, from its model calls, for when its entry
    span recorded neither (a LangGraph root records nothing): the first call's human message,
    and its structured output (a tool call no tool span ran, like a ToolStrategy decision),
    else the last call's answer."""
    nodes = _all(roots)
    models = [n for n in nodes if n.kind == "llm"]
    if not models:
        return None, None
    tools = {n.name for n in nodes if n.kind == "tool"}
    asked = human(span_content(session, models[0].id).input)
    answered: str | None = None
    for node in reversed(models):  # newest first: stop at the structured output
        if node.status == "error":  # a failed call answered nothing
            continue
        output = span_content(session, node.id).output
        for call in reversed(tool_calls(output)):
            found = as_dict(call)
            if found is not None and found.get("name") not in tools:
                return asked, call_line(found)
        answered = answered or langchain_answer(output)
    return asked, answered


def _failure_line(node: SpanNode) -> str:
    return f"{node.kind} {node.name} failed: {flat_text(node.error, ERROR_CHARS)}"


def _refused_line(node: SpanNode) -> str:
    why = f" ({flat_text(node.error, ERROR_CHARS)})" if node.error else ""
    return f"refused: {node.kind} {node.name}{why}"


def documents(session: Session, run: Run, state: str) -> list[Document]:
    """The run's summary first, then one document per root-cause failure (at most
    MAX_FAILURES, the earliest)."""
    roots = span_tree(session, run.id)
    nodes = _all(roots)
    asked, answered = _conversation(session, run, roots)
    failures = root_failures(roots)
    # A refused call's permission wait is rejected too: list the call once, not its wait
    refused = [
        n
        for n in nodes
        if n.status == "rejected" and not (n.parent is not None and n.parent.status == "rejected")
    ]
    tools = Counter(n.name for n in nodes if n.kind == "tool")
    agent = run.agent or run.source
    name = flat_text(run.name, NAME_CHARS)
    summary = _cap(
        [
            f"{agent} run: {name}",
            f"status: {state}",
            f"asked: {flat_text(asked, INPUT_CHARS)}" if asked else "",
            f"answered: {flat_text(answered, OUTPUT_CHARS)}" if answered else "",
            *(_failure_line(n) for n in failures[:MAX_IN_SUMMARY]),
            *(_refused_line(n) for n in refused[:MAX_IN_SUMMARY]),
            f"tools: {', '.join(t for t, _ in tools.most_common(MAX_TOOLS))}" if tools else "",
        ]
    )
    docs = [Document("run_summary", None, summary)]
    for node in failures[:MAX_FAILURES]:
        given = span_content(session, node.id).input
        lines = [
            _failure_line(node),
            f"in {agent} run: {name}",
            f"input: {flat_text(given, INPUT_CHARS)}" if given else "",
        ]
        docs.append(Document("failed_span", node.id, _cap(lines)))
    return docs
