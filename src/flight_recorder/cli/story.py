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
from typing import Any, cast

from rich.text import Text

from flight_recorder.cli.render import clean, one_line, text
from flight_recorder.search.summary import strings
from flight_recorder.store.read import SpanContent, SpanNode

Content = Callable[[uuid.UUID], SpanContent]

TASK_LINES = 12
TASK_CHARS = 1_200
MAX_STEPS = 12
VALUE_CHARS = 400  # room for a rationale
CALL_CHARS = 1_000
ANSWER_CHARS = 400
RESULT_ITEMS = 5
STEP_KINDS = ("llm", "tool")


def _flat(value: Any, limit: int) -> str:
    """A value's strings as one line, cut to `limit` characters."""
    joined = " ".join(" ".join(s.split()) for s in strings(value) if s.strip())
    return joined if len(joined) <= limit else joined[: limit - 1] + "…"


def _dict(value: Any) -> dict[str, Any] | None:
    return cast("dict[str, Any]", value) if isinstance(value, dict) else None


def _list(value: Any) -> list[Any] | None:
    return cast("list[Any]", value) if isinstance(value, list) else None


def _message_text(content: Any) -> str:
    """A message's content: a string, or text blocks joined."""
    if isinstance(content, str):
        return content
    blocks = _list(content) or []
    parts = [str(b.get("text", "")) for block in blocks if (b := _dict(block)) is not None]
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


def _cut(value: str, limit: int) -> str:
    return value if len(value) <= limit else value[: limit - 1] + "…"


def _value(value: Any) -> str:
    items = _list(value)
    if items is not None:
        return _cut(", ".join(str(v) for v in items), VALUE_CHARS)
    return _cut(" ".join(str(value).split()), VALUE_CHARS)


def _call(tool_call: Any) -> str:
    found = _dict(tool_call) or {}
    args = _dict(found.get("args")) or {}
    shown = "; ".join(f"{name}: {_value(v)}" for name, v in args.items() if v is not None)
    return _cut(f"{found.get('name', '?')}({shown})", CALL_CHARS)


def _model_answer(output: Any) -> str:
    kwargs = _ai(output)
    if kwargs is not None:
        calls = _list(kwargs.get("tool_calls")) or []
        if calls:
            return ", ".join(_call(c) for c in calls)
        said = " ".join(_message_text(kwargs.get("content")).split())
        if said:
            return f'"{_cut(said, ANSWER_CHARS)}"'
    return _flat(output, ANSWER_CHARS)


def _record(item: dict[str, Any]) -> str:
    parts = [f"#{item['id']}" if "id" in item else "", str(item.get("title", ""))]
    labels = _list(item.get("labels"))
    detail = [", ".join(str(label) for label in labels) if labels else "",
              str(item.get("milestone") or "")]  # fmt: skip
    head = " ".join(p for p in parts if p)
    tail = "; ".join(d for d in detail if d)
    return " ".join((f"{head} ({tail})" if tail else head).split())


def _tool_result(output: Any) -> str:
    content = (_dict((_dict(output) or {}).get("data")) or {}).get("content", output)
    if isinstance(content, str):
        try:
            content = json.loads(content)
        except ValueError:
            return _cut(" ".join(content.split()), ANSWER_CHARS)
    items = _list(content)
    if items and all(isinstance(i, dict) for i in items):
        records = [_record(cast("dict[str, Any]", i)) for i in items[:RESULT_ITEMS]]
        more = f" · … {len(items) - RESULT_ITEMS} more" if len(items) > RESULT_ITEMS else ""
        return _cut(" · ".join(r for r in records if r), CALL_CHARS) + more
    return _flat(content, ANSWER_CHARS)


def _steps(roots: Sequence[SpanNode]) -> list[SpanNode]:
    nodes = [node for root in roots for node in root.walk() if node.kind in STEP_KINDS]
    return sorted(nodes, key=lambda n: (n.started_at, n.external_id))


def task(roots: Sequence[SpanNode], content: Content) -> Text | None:
    """What the agent was asked: the first model call's last human message, or else the
    entry span's input; at most TASK_LINES lines and TASK_CHARS characters."""
    asked: str | None = None
    first_model = next((n for n in _steps(roots) if n.kind == "llm"), None)
    if first_model is not None:
        asked = _human(content(first_model.id).input)
    if asked is None and roots:
        asked = _flat(content(roots[0].id).input, TASK_CHARS) or None
    if not asked:
        return None
    lines = clean(asked.strip())[:TASK_CHARS].splitlines()[:TASK_LINES]
    return Text("\n".join(f"  {line}" for line in lines))


def steps(roots: Sequence[SpanNode], content: Content) -> Text | None:
    """The model and tool calls in time order: what each was given, returned or answered."""
    nodes = _steps(roots)
    if not nodes:
        return None
    lines = Text()
    for number, node in enumerate(nodes[:MAX_STEPS], 1):
        found = content(node.id)
        lines.append(f"  {number:>2} ", style="dim")
        if node.kind == "llm":
            lines.append("model → ", style="dim")
            lines.append_text(text(one_line(_model_answer(found.output), CALL_CHARS)))
        else:
            given = _flat(found.input, VALUE_CHARS)
            lines.append("tool ", style="dim")
            lines.append_text(text(f"{one_line(node.name, 80)}({one_line(given, VALUE_CHARS)})"))
            lines.append(" → ", style="dim")
            lines.append_text(text(one_line(_tool_result(found.output), CALL_CHARS + 40)))
        if node.error:
            lines.append_text(text(f"  {one_line(node.error, 200)}", "red"))
        lines.append("\n")
    if len(nodes) > MAX_STEPS:
        lines.append(f"  … {len(nodes) - MAX_STEPS} more\n", style="dim")
    lines.rstrip()
    return lines
