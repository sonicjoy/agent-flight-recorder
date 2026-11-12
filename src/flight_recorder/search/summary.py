"""The texts that get embedded: a short summary of each finished run, and one text for each
root-cause failure in it.

Everything comes from columns that were redacted at ingest. bge-small reads 512 tokens, about
2,000 characters of English, so each text is kept within MAX_CHARS, most telling parts
first.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any, Literal, cast

from sqlalchemy.orm import Session

from flight_recorder.store.models import Run
from flight_recorder.store.read import SpanNode, span_content, span_tree

MAX_CHARS = 2_000
INPUT_CHARS = 500
OUTPUT_CHARS = 400
ERROR_CHARS = 300
MAX_FAILURES_IN_SUMMARY = 3
MAX_TOOLS = 10

Kind = Literal["run_summary", "failed_span"]


@dataclass(frozen=True)
class Document:
    kind: Kind
    span: SpanNode | None
    content: str


def root_failures(roots: list[SpanNode]) -> list[SpanNode]:
    """Error spans with no failed child, in time order. One API error usually marks the
    llm, its step and its agent; only the deepest says what happened. Rejected calls
    (refused permission) aren't failures."""
    failures = [
        node
        for root in roots
        for node in root.walk()
        if node.status == "error" and not any(c.status == "error" for c in node.children)
    ]
    return sorted(failures, key=lambda n: (n.started_at, n.external_id))


def _strings(value: Any) -> Iterator[str]:
    """The text inside a recorded input or output: its strings, depth first."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in cast("dict[str, Any]", value).values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in cast("list[Any]", value):
            yield from _strings(item)


def _text(value: Any, limit: int) -> str:
    """A recorded value as one line of plain text, cut to `limit` characters."""
    joined = " ".join(" ".join(s.split()) for s in _strings(value) if s.strip())
    return joined if len(joined) <= limit else joined[: limit - 1] + "…"


def _cap(lines: list[str]) -> str:
    return "\n".join(line for line in lines if line)[:MAX_CHARS]


def _failure_line(node: SpanNode) -> str:
    return f"{node.kind} {node.name} failed: {_text(node.error, ERROR_CHARS)}"


def documents(session: Session, run: Run, state: str) -> list[Document]:
    """The run's summary first, then one document per root-cause failure."""
    roots = span_tree(session, run.id)
    entry = roots[0] if roots else None
    asked, answered = (None, None)
    if entry is not None:
        found = span_content(session, entry.id)
        asked, answered = found.input, found.output
    failures = root_failures(roots)
    tools = Counter(n.name for root in roots for n in root.walk() if n.kind == "tool")
    agent = run.agent or run.source
    summary = _cap(
        [
            f"{agent} run: {_text(run.name, 200)}",
            f"status: {state}",
            f"asked: {_text(asked, INPUT_CHARS)}" if asked else "",
            f"answered: {_text(answered, OUTPUT_CHARS)}" if answered else "",
            *(_failure_line(n) for n in failures[:MAX_FAILURES_IN_SUMMARY]),
            f"tools: {', '.join(name for name, _ in tools.most_common(MAX_TOOLS))}"
            if tools
            else "",
        ]
    )
    docs = [Document("run_summary", None, summary)]
    for node in failures:
        given = span_content(session, node.id).input
        docs.append(
            Document(
                "failed_span",
                node,
                _cap(
                    [
                        _failure_line(node),
                        f"in {agent} run: {_text(run.name, 200)}",
                        f"input: {_text(given, INPUT_CHARS)}" if given else "",
                    ]
                ),
            )
        )
    return docs
