"""Reading recorded payloads: LangChain's serialised prompts and results, and any value as
plain text. Pure, so the label screen (cli/story.py) and search's summaries
(search/summary.py) read a run the same way (#14).
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any, cast

VALUE_CHARS = 400  # room for a rationale
CALL_CHARS = 1_000
ANSWER_CHARS = 400
PLAIN_DEPTH = 20


def cut(value: str, width: int) -> str:
    """`value` shortened to `width` characters, ending in … when it was longer."""
    return value if len(value) <= width else value[: width - 1] + "…"


def flat(value: str) -> str:
    """All of `value` on one line: every run of whitespace, newlines too, is one space."""
    return " ".join(value.split())


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


def flat_text(value: Any, limit: int) -> str:
    """A recorded value as one line of plain text, cut to `limit` characters."""
    return cut(" ".join(flat(s) for s in _strings(value) if s.strip()), limit)


def as_dict(value: Any) -> dict[str, Any] | None:
    return cast("dict[str, Any]", value) if isinstance(value, dict) else None


def as_list(value: Any) -> list[Any] | None:
    return cast("list[Any]", value) if isinstance(value, list) else None


def plain(value: Any, depth: int = 0) -> str:
    """Any value as words: keys and values, numbers too; nothing for None."""
    if value is None or depth > PLAIN_DEPTH:
        return ""
    found = as_dict(value)
    if found is not None:
        pairs = (
            f"{flat(str(key))} {plain(v, depth + 1)}".strip()
            for key, v in found.items()
            if v is not None
        )
        return " ".join(pairs)
    items = as_list(value)
    if items is not None:
        return ", ".join(p for v in items if (p := plain(v, depth + 1)))
    return flat(str(value))


def message_text(content: Any) -> str:
    """A message's content: a string, or text blocks joined."""
    if isinstance(content, str):
        return content
    blocks = as_list(content) or []
    parts = [str(b.get("text") or "") for block in blocks if (b := as_dict(block)) is not None]
    return "\n".join(part for part in parts if part)


def human(prompt: Any) -> str | None:
    """The last human message of a LangChain chat prompt: {"messages": [[message, ...]]}."""
    batches = as_list((as_dict(prompt) or {}).get("messages"))
    messages = as_list(batches[0]) if batches else None
    for item in reversed(messages or []):
        kwargs = as_dict((as_dict(item) or {}).get("kwargs")) or {}
        if kwargs.get("type") == "human":
            return message_text(kwargs.get("content")) or None
    return None


def ai(result: Any) -> dict[str, Any] | None:
    """The message kwargs of a LangChain LLMResult: generations[0][0].message.kwargs."""
    generations = as_list((as_dict(result) or {}).get("generations"))
    first = as_list(generations[0]) if generations else None
    generation = as_dict(first[0]) if first else None
    message = as_dict((generation or {}).get("message"))
    return as_dict((message or {}).get("kwargs"))


def _value(value: Any) -> str:
    items = as_list(value)
    if items is not None:
        return cut(", ".join(flat(str(v)) for v in items if v is not None), VALUE_CHARS)
    return cut(flat(str(value)), VALUE_CHARS)


def call_line(tool_call: Any) -> str:
    found = as_dict(tool_call) or {}
    args = as_dict(found.get("args")) or {}
    shown = "; ".join(
        f"{flat(str(name))}: {_value(v)}" for name, v in args.items() if v is not None
    )
    return cut(f"{flat(str(found.get('name') or '?'))}({shown})", CALL_CHARS)


def tool_calls(output: Any) -> list[Any]:
    """The tool calls a LangChain model call made (empty if none, or not LangChain)."""
    kwargs = ai(output)
    return (as_list(kwargs.get("tool_calls")) or []) if kwargs is not None else []


def langchain_answer(output: Any) -> str | None:
    """A LangChain model call's answer: its tool calls, else its text quoted; None if the
    output isn't LangChain's or says nothing (a failed call)."""
    kwargs = ai(output)
    if kwargs is None:
        return None
    calls = tool_calls(output)
    if calls:
        return ", ".join(call_line(c) for c in calls)
    said = flat(message_text(kwargs.get("content")))
    return f'"{cut(said, ANSWER_CHARS)}"' if said else None


def model_answer(output: Any) -> str:
    """Any model call's answer, for a reader: LangChain's, or else the output as text."""
    return (
        langchain_answer(output)
        or flat_text(output, ANSWER_CHARS)
        or cut(plain(output), ANSWER_CHARS)
    )
