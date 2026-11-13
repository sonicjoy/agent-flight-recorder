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

from flight_recorder.cli.render import clean, cut, flat, one_line, text
from flight_recorder.search.summary import flat_text
from flight_recorder.store.read import SpanContent, SpanNode

Content = Callable[[uuid.UUID], SpanContent]

TASK_LINES = 12
TASK_CHARS = 1_200
HEAD_STEPS = 8  # shown from the start of a long run,
TAIL_STEPS = 4  # and from its end, where the decision usually is
VALUE_CHARS = 400  # room for a rationale
CALL_CHARS = 1_000
ANSWER_CHARS = 400
RESULT_ITEMS = 5
JSON_CHARS = 100_000  # longer tool results aren't parsed, only shown as text
PLAIN_DEPTH = 20
STEP_KINDS = ("llm", "tool")
NO_RESULTS = "(no results)"
LAST = datetime.max.replace(tzinfo=UTC)


def _dict(value: Any) -> dict[str, Any] | None:
    return cast("dict[str, Any]", value) if isinstance(value, dict) else None


def _list(value: Any) -> list[Any] | None:
    return cast("list[Any]", value) if isinstance(value, list) else None


def _plain(value: Any, depth: int = 0) -> str:
    """Any value as words: keys and values, numbers too; nothing for None."""
    if value is None or depth > PLAIN_DEPTH:
        return ""
    found = _dict(value)
    if found is not None:
        pairs = (
            f"{key} {_plain(v, depth + 1)}".strip() for key, v in found.items() if v is not None
        )
        return " ".join(pairs)
    items = _list(value)
    if items is not None:
        return ", ".join(p for v in items if (p := _plain(v, depth + 1)))
    return flat(str(value))


def _message_text(content: Any) -> str:
    """A message's content: a string, or text blocks joined."""
    if isinstance(content, str):
        return content
    blocks = _list(content) or []
    parts = [str(b.get("text") or "") for block in blocks if (b := _dict(block)) is not None]
    return "\n".join(part for part in parts if part)


def _human(prompt: Any) -> str | None:
    """The last human message of a LangChain chat prompt: {"messages": [[message, ...]]}."""
    batches = _list((_dict(prompt) or {}).get("messages"))
    messages = _list(batches[0]) if batches else None
    for item in reversed(messages or []):
        kwargs = _dict((_dict(item) or {}).get("kwargs")) or {}
        if kwargs.get("type") == "human":
            return _message_text(kwargs.get("content")) or None
    return None


def _ai(result: Any) -> dict[str, Any] | None:
    """The message kwargs of a LangChain LLMResult: generations[0][0].message.kwargs."""
    generations = _list((_dict(result) or {}).get("generations"))
    first = _list(generations[0]) if generations else None
    generation = _dict(first[0]) if first else None
    message = _dict((generation or {}).get("message"))
    return _dict((message or {}).get("kwargs"))


def _value(value: Any) -> str:
    items = _list(value)
    if items is not None:
        return cut(", ".join(flat(str(v)) for v in items if v is not None), VALUE_CHARS)
    return cut(flat(str(value)), VALUE_CHARS)


def _call(tool_call: Any) -> str:
    found = _dict(tool_call) or {}
    args = _dict(found.get("args")) or {}
    shown = "; ".join(f"{name}: {_value(v)}" for name, v in args.items() if v is not None)
    return cut(f"{flat(str(found.get('name') or '?'))}({shown})", CALL_CHARS)


def _model_answer(output: Any) -> str:
    kwargs = _ai(output)
    if kwargs is not None:
        calls = _list(kwargs.get("tool_calls")) or []
        if calls:
            return ", ".join(_call(c) for c in calls)
        said = flat(_message_text(kwargs.get("content")))
        if said:
            return f'"{cut(said, ANSWER_CHARS)}"'
    return flat_text(output, ANSWER_CHARS) or cut(_plain(output), ANSWER_CHARS)


def _record(item: dict[str, Any]) -> str:
    """A search hit or similar: `#id title (labels; milestone)`, from the fields it has."""
    number = f"#{item['id']}" if item.get("id") is not None else ""
    head = " ".join(p for p in (number, flat(str(item.get("title") or ""))) if p)
    labels = ", ".join(flat(str(x)) for x in _list(item.get("labels")) or [] if x is not None)
    tail = "; ".join(d for d in (labels, flat(str(item.get("milestone") or ""))) if d)
    if not head and not tail:
        return _plain(item)
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
    data = _dict((_dict(output) or {}).get("data"))
    content = data["content"] if data is not None and "content" in data else output
    if isinstance(content, str):
        content = _parsed(content)
        if isinstance(content, str):
            return cut(flat(content), ANSWER_CHARS) or NO_RESULTS
    items = _list(content)
    if items is not None and items and all(isinstance(i, dict) for i in items):
        more = f" · … {len(items) - RESULT_ITEMS} more" if len(items) > RESULT_ITEMS else ""
        records = [_record(cast("dict[str, Any]", i)) for i in items[:RESULT_ITEMS]]
        return cut(" · ".join(r for r in records if r), CALL_CHARS - len(more)) + more
    shown = flat_text(content, ANSWER_CHARS) or cut(_plain(content), ANSWER_CHARS)
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
        asked = _human(content(first_model.id).input)
    if asked is None and roots:
        asked = flat_text(content(roots[0].id).input, TASK_CHARS + 1) or None
    if not asked:
        return None
    raw = asked.strip()
    was_cut = len(raw) > TASK_CHARS
    lines = raw[:TASK_CHARS].splitlines()  # cut before cleaning: the text may be huge
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
        line.append_text(text(one_line(_model_answer(found.output), CALL_CHARS)))
    else:
        given = flat_text(found.input, VALUE_CHARS) or cut(_plain(found.input), VALUE_CHARS)
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
