"""A run's story for the label screen (#14): what the agent was asked, what it called and got
back, what it answered. So a labeller can judge why a run went wrong without opening spans.

Reads LangChain's serialised payloads (the OTel runs of LangChain and LangGraph agents) and
falls back to a run's plain text for anything else. Pure: give it the span tree and a way to
read a span's content (store.read.span_content), so it's tested without a database.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Any, cast

from rich.text import Text

from flight_recorder.cli.render import clean, one_line, text
from flight_recorder.payloads import (
    ANSWER_CHARS,
    CALL_CHARS,
    VALUE_CHARS,
    as_dict,
    as_list,
    cut,
    flat,
    flat_text,
    human,
    model_answer,
    plain,
)
from flight_recorder.store.read import SpanContent, SpanNode

Content = Callable[[uuid.UUID], SpanContent]

TASK_LINES = 12
TASK_CHARS = 1_200
HEAD_STEPS = 8  # shown from the start of a long run,
TAIL_STEPS = 4  # and from its end, where the decision usually is
RESULT_ITEMS = 5
JSON_CHARS = 100_000  # longer tool results aren't parsed, only shown as text
STEP_KINDS = ("llm", "tool")
NO_RESULTS = "(no results)"
NOTHING = "(nothing recorded)"  # no output at all, e.g. a refused call
LAST = datetime.max.replace(tzinfo=UTC)


def _record(item: dict[str, Any]) -> str:
    """A search hit or similar: `#id title (labels; milestone)`, from the fields it has."""
    number = f"#{flat(str(item['id']))}" if item.get("id") is not None else ""
    head = " ".join(p for p in (number, flat(str(item.get("title") or ""))) if p)
    labels = ", ".join(flat(str(x)) for x in as_list(item.get("labels")) or [] if x is not None)
    tail = "; ".join(d for d in (labels, flat(str(item.get("milestone") or ""))) if d)
    if not head and not tail:
        return plain(item)
    return f"{head} ({tail})".strip() if tail else head


def _parsed(content: str) -> Any:
    """A tool's text result as JSON when it is some (and not too long or deep), else as is."""
    if len(content) > JSON_CHARS:
        return content
    try:
        return json.loads(content)
    except (ValueError, RecursionError):
        return content


def _tool_result(output: Any) -> str:
    if output is None:
        return NOTHING
    data = as_dict((as_dict(output) or {}).get("data"))
    content = data["content"] if data is not None and "content" in data else output
    if isinstance(content, str):
        content = _parsed(content)
        if isinstance(content, str):
            return cut(flat(content), ANSWER_CHARS) or NO_RESULTS
    items = as_list(content)
    if items is not None and items and all(isinstance(i, dict) for i in items):
        more = f" · … {len(items) - RESULT_ITEMS} more" if len(items) > RESULT_ITEMS else ""
        records = [_record(cast("dict[str, Any]", i)) for i in items[:RESULT_ITEMS]]
        joined = " · ".join(r for r in records if r)
        return cut(joined, CALL_CHARS - len(more)) + more if joined else NO_RESULTS
    shown = flat_text(content, ANSWER_CHARS) or cut(plain(content), ANSWER_CHARS)
    return shown or NO_RESULTS


def _order(node: SpanNode) -> tuple[datetime, datetime, int, str]:
    """Start time; on a tie (Windows clocks tie often) the one that ended first."""
    return node.started_at, node.ended_at or LAST, node.depth, node.external_id


def _steps(roots: Sequence[SpanNode]) -> list[SpanNode]:
    return sorted((n for root in roots for n in root.walk() if n.kind in STEP_KINDS), key=_order)


def task(roots: Sequence[SpanNode], content: Content) -> Text | None:
    """What the agent was asked: the first model call's last human message, or else the
    entry span's input; at most TASK_LINES lines and TASK_CHARS characters, ending in …
    when cut."""
    asked: str | None = None
    first_model = next((n for n in _steps(roots) if n.kind == "llm"), None)
    if first_model is not None:
        asked = human(content(first_model.id).input)
    if asked is None and roots:
        asked = flat_text(content(roots[0].id).input, TASK_CHARS + 1) or None
    if not asked:
        return None
    raw = asked.strip()
    was_cut = len(raw) > TASK_CHARS
    # cut before cleaning (the text may be huge); split on newlines only, so other
    # separators (\u2028, \x0c...) are shown as escapes by clean()
    lines = raw[:TASK_CHARS].replace("\r\n", "\n").split("\n")
    if len(lines) > TASK_LINES:
        lines, was_cut = lines[:TASK_LINES], True
    if was_cut:
        lines[-1] += "…"
    return Text("\n".join(f"  {clean(line)}" for line in lines))


def _step(number: int, node: SpanNode, content: Content) -> Text:
    found = content(node.id)
    line = Text(f"  {number:>2} ", style="dim")
    if node.kind == "llm":
        line.append("model → ", style="dim")
        line.append_text(text(one_line(model_answer(found.output), CALL_CHARS)))
    else:
        given = flat_text(found.input, VALUE_CHARS) or cut(plain(found.input), VALUE_CHARS)
        line.append("tool ", style="dim")
        line.append_text(text(f"{one_line(flat(node.name), 80)}({one_line(given, VALUE_CHARS)})"))
        line.append(" → ", style="dim")
        line.append_text(text(one_line(_tool_result(found.output), CALL_CHARS)))
    if node.error:
        line.append_text(text(f"  {one_line(flat(node.error), 200)}", "red"))
    return line


def steps(roots: Sequence[SpanNode], content: Content) -> Text | None:
    """The model and tool calls in time order: what each was given, returned or answered.
    A long run shows its first HEAD_STEPS and last TAIL_STEPS, so its decision stays."""
    nodes = _steps(roots)
    if not nodes:
        return None
    numbered = list(enumerate(nodes, 1))
    folded = len(nodes) > HEAD_STEPS + TAIL_STEPS
    shown = numbered[:HEAD_STEPS] + numbered[-TAIL_STEPS:] if folded else numbered
    lines = Text()
    for number, node in shown:
        if folded and number == len(nodes) - TAIL_STEPS + 1:
            lines.append(f"  … {len(nodes) - HEAD_STEPS - TAIL_STEPS} more\n", style="dim")
        lines.append_text(_step(number, node, content))
        lines.append("\n")
    lines.rstrip()
    return lines
