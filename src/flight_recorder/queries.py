"""The questions the MCP server answers (#24), as plain functions: a session in, data out.

No MCP, no typer and no printing here, so a CLI front end can reuse them. Every result is
final: each recorded field is turned into one text, cleaned and capped (`safe_text.safe`),
and the whole result is trimmed to MAX_RESULT_CHARS with a `cut` line per kind of item left
out (docs/designs/mcp-server.md, "Cleaning and capping").

Recorded text is untrusted: it goes only into named data fields, never into prose written
here.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable, Sequence
from datetime import datetime
from typing import Any, NamedTuple, cast

from sqlalchemy import select
from sqlalchemy.orm import Session

from flight_recorder.payloads import recorded_text, value_text
from flight_recorder.safe_text import safe
from flight_recorder.search.embedder import Embedder
from flight_recorder.search.index import has_vectors, pending_runs
from flight_recorder.search.query import SEARCHABLE_STATES, search
from flight_recorder.search.summary import (
    ERROR_CHARS,
    INPUT_CHARS,
    NAME_CHARS,
    OUTPUT_CHARS,
    conversation,
    refused_calls,
    root_failures,
)
from flight_recorder.store.labels import (
    answer_of,
    decided_fields,
    labels_of,
    mode_counts,
    runs_with_mode,
    split_note,
)
from flight_recorder.store.models import LABEL_SOURCES, Run
from flight_recorder.store.read import MAX_MATCHES as PREFIX_MATCHES
from flight_recorder.store.read import (
    AmbiguousRunError,
    RunFilter,
    RunNotFoundError,
    RunSummary,
    SpanNode,
    parse_when,
    resolve_run,
    span_content,
    span_tree,
    summaries_of,
)

SEARCH_LIMIT = 25
MODES_LIMIT = 50
MAX_EXPLAINED = 5  # failures shown with their error, path and input
MAX_REFUSED = 10
MAX_LABELS = 10
MAX_MODES_SHOWN = 3  # modes whose other runs are listed
SAME_MODE_RUNS = 5
MAX_FIELDS = 20  # decision and right-answer fields
PATH_NAMES = 6
NOTE_CHARS = 500
VALUE_CHARS = OUTPUT_CHARS  # a decision or right-answer value
MAX_MATCHES = 5  # runs listed when a reference is ambiguous
TOO_DEEP = "(nested too deeply to show)"
MAX_RESULT_CHARS = 20_000
"""A whole result as the SDK sends it as text (JSON, indented by 2): far below Claude Code's
default limit on MCP output (25,000 tokens), so Claude Code never cuts a result silently."""

Result = dict[str, Any]

# The kinds `cut` lines name: one spelling each, for caps and trims alike
FAILURES = "failures"
REFUSED = "refused calls"
LABELS = "labels"
FRAGMENTS = "note fragments from merged labels"
SAME_MODE = "same-mode runs"
MODES_UNCHECKED = "modes' other runs"  # modes past MAX_MODES_SHOWN, not looked up
DECISIONS = "decision fields"
ANSWER_FIELDS = "right-answer fields"
HITS = "hits"
MODES = "modes"


class QueryError(ValueError):
    """A bad argument, or a run reference that matches no run or several."""


class Trim(NamedTuple):
    kind: str
    """How a `cut` line names what was left out: "labels", "failures"..."""
    drop: Callable[[Result], bool]
    """Removes one item from the result; False when there is none left."""


def size(result: Result) -> int:
    """The result's length as the SDK sends it as text: `pydantic_core.to_json(indent=2)`."""
    return len(json.dumps(result, indent=2, ensure_ascii=False))


def _cut_lines(counts: dict[str, int]) -> list[str]:
    return [f"{n} {kind} left out" for kind, n in counts.items() if n > 0]


def fit(
    result: Result,
    trims: Sequence[Trim],
    counts: dict[str, int] | None = None,
    budget: int = MAX_RESULT_CHARS,
) -> Result:
    """`result` with items dropped, in the order of `trims`, until it fits `budget`, and a
    `cut` line per kind: those already left out by a cap (`counts`) and those dropped here.
    Stops when nothing is left to drop. Changes and returns `result`."""
    counts = dict(counts or {})
    result["cut"] = _cut_lines(counts)
    while size(result) > budget:
        for trim in trims:
            if trim.drop(result):
                counts[trim.kind] = counts.get(trim.kind, 0) + 1
                break
        else:
            break  # nothing left to drop
        result["cut"] = _cut_lines(counts)
    return result


def _pop(key: str, keep: int = 0) -> Callable[[Result], bool]:
    """Drops the last item of the list at `key`, keeping at least `keep`."""

    def drop(result: Result) -> bool:
        items = result.get(key)
        if isinstance(items, list) and len(cast("list[Any]", items)) > keep:
            cast("list[Any]", items).pop()
            return True
        return False

    return drop


def _pop_field(key: str, inner: str | None = None) -> Callable[[Result], bool]:
    """Drops the last field of the dict at `key` (or at `key`.`inner`)."""

    def drop(result: Result) -> bool:
        found = result.get(key)
        if inner is not None and isinstance(found, dict):
            found = cast("dict[str, Any]", found).get(inner)
        if isinstance(found, dict) and found:
            fields = cast("dict[str, Any]", found)
            del fields[next(reversed(fields))]
            return True
        return False

    return drop


def _pop_same_mode_run(result: Result) -> bool:
    groups = cast("list[dict[str, Any]]", result.get("same_mode_runs") or [])
    if not groups:  # a group is never kept empty
        return False
    groups[-1]["runs"].pop()
    if not groups[-1]["runs"]:
        groups.pop()
    return True


# --- shared pieces -----------------------------------------------------------------------


def _brief(summary: RunSummary) -> Result:
    run = summary.run
    return {
        "id": run.id,
        "external_id": safe(run.external_id, NAME_CHARS),  # recorded: an OTel sender sets it
        "source": run.source,
        "agent": safe(run.agent, NAME_CHARS) if run.agent else None,
        "name": safe(run.name, NAME_CHARS),
        "state": summary.state,
        "started_at": run.started_at,
    }


def _plain(value: str | None, name: str) -> str | None:
    """`value` unless it holds a NUL, which Postgres can't take in text."""
    if value is not None and "\x00" in value:
        raise QueryError(f"{name} can't contain a NUL character")
    return value


ATTR_KEYS = 20
ATTR_VALUES = 100


def _attrs(attrs: dict[str, list[str]]) -> dict[str, tuple[str, ...]]:
    """An attribute filter, checked: a key and at least one value each, all text without NUL,
    and capped, so a huge filter is a clear refusal, not a slow search or a driver error."""
    if len(attrs) > ATTR_KEYS:
        raise QueryError(f"attrs takes at most {ATTR_KEYS} keys")
    checked: dict[str, tuple[str, ...]] = {}
    for key, values in attrs.items():
        if not cast("str", _plain(key, "an attrs key")):
            raise QueryError("an attrs key can't be empty")
        if not values:
            raise QueryError(f"attrs {key!r} needs at least one value")
        if len(values) > ATTR_VALUES:
            raise QueryError(f"attrs takes at most {ATTR_VALUES} values per key")
        checked[key] = tuple(cast("str", _plain(v, f"attrs {key!r}")) for v in values)
    return checked


def _resolve(session: Session, ref: str, now: datetime) -> RunSummary:
    ref = cast("str", _plain(ref, "run")).strip()
    if not ref:
        raise QueryError("say which run: last, a run id prefix, or the session or trace id")
    try:
        run = resolve_run(session, ref)
    except RunNotFoundError as error:
        raise QueryError(str(error)) from None
    except AmbiguousRunError as error:
        shown = summaries_of(session, error.matches[:MAX_MATCHES], now)
        lines = [f"{s.run.id} {s.run.started_at:%Y-%m-%d %H:%M} {s.run.name or ''}" for s in shown]
        more = len(error.matches) - len(shown)
        capped = len(error.matches) >= PREFIX_MATCHES  # resolve_run stops counting there
        least = "at least " if capped else ""
        head = f"{ref!r} matches {least}{len(error.matches)} runs; use more of the id"
        tail = [f"and {least}{more} more"] if more > 0 else []
        raise QueryError("; ".join([head, *lines, *tail])) from None
    (summary,) = summaries_of(session, [run], now)
    return summary


def _text(value: Any, limit: int) -> str | None:
    """A recorded value as one capped text, or None when it has none. A value nested too
    deeply for the walk (ingest stores up to the redactor's own depth) is named, not shown."""
    try:
        text = recorded_text(value)
    except RecursionError:
        return TOO_DEEP
    return safe(text, limit) if text else None


def _fields(items: Sequence[tuple[str, Any]]) -> tuple[dict[str, str], int]:
    """At most MAX_FIELDS fields, names and values capped; and how many were left out,
    counting a name that becomes another's once capped."""
    shown: dict[str, str] = {}
    for name, value in items[:MAX_FIELDS]:
        key = cast("str", safe(name, NAME_CHARS))
        if key not in shown:
            shown[key] = cast("str", safe(value_text(value), VALUE_CHARS))
    return shown, len(items) - len(shown)


def _path(node: SpanNode) -> list[str]:
    names = [cast("str", safe(n.name, NAME_CHARS)) for n in node.path()]
    if len(names) <= PATH_NAMES:
        return names
    return [*names[:2], "…", *names[-(PATH_NAMES - 3) :]]


# --- explain_run -------------------------------------------------------------------------


def explain_run(session: Session, ref: str, now: datetime) -> Result:
    """Why a run failed: its root failures with their paths, errors and inputs, refused
    calls, what it was asked and answered, its decisions and right answer, its labels, and
    other runs with the same labelled modes."""
    summary = _resolve(session, ref, now)
    run = summary.run
    roots = span_tree(session, run.id)
    spans = sum(len(root.walk()) for root in roots)
    counts: dict[str, int] = {}

    found = root_failures(roots)
    failures = [
        {
            "span_id": node.id,
            "kind": node.kind,
            "name": safe(node.name, NAME_CHARS),
            "error": safe(node.error, ERROR_CHARS) if node.error else None,
            "path": _path(node),
            "input": _text(span_content(session, node.id).input, INPUT_CHARS),
        }
        for node in found[:MAX_EXPLAINED]
    ]
    counts[FAILURES] = len(found) - len(failures)

    refused_all = refused_calls(roots)
    refused = [
        {"span_id": n.id, "kind": n.kind, "name": safe(n.name, NAME_CHARS)}
        for n in refused_all[:MAX_REFUSED]
    ]
    counts[REFUSED] = len(refused_all) - len(refused)

    asked, answered = conversation(session, run, roots)

    decisions, counts[DECISIONS] = _fields(list(decided_fields(run.attributes or {}).items()))

    right_answer: Result | None = None
    if (answer := answer_of(session, run.id)) is not None:
        fields = answer.get("fields")
        if isinstance(fields, dict):
            shown, counts[ANSWER_FIELDS] = _fields(list(cast("dict[str, Any]", fields).items()))
            right_answer = {"fields": shown}
        else:
            right_answer = {"text": safe(str(answer.get("text", "")), VALUE_CHARS)}

    all_labels = labels_of(session, run.id)
    labels: list[Result] = []
    fragments = 0
    for label in all_labels[:MAX_LABELS]:
        own, merged = split_note(label.note)
        fragments += len(merged)
        labels.append(
            {
                "mode": safe(label.failure_mode, NAME_CHARS),
                "source": label.source,
                "on_span": label.span_id,
                "note": safe(own.strip(), NOTE_CHARS) if own.strip() else None,
                "author": safe(label.author, NAME_CHARS) if label.author else None,
                "confidence": label.confidence,
                "created_at": label.created_at,
            }
        )
    counts[LABELS] = len(all_labels) - len(labels)
    counts[FRAGMENTS] = fragments

    modes: dict[str, str] = {}  # mode key -> its spelling on the run's earliest label
    for label in all_labels:
        if label.mode_key:
            modes.setdefault(label.mode_key, label.failure_mode)
    same_mode: list[Result] = []
    for key, mode in list(modes.items())[:MAX_MODES_SHOWN]:
        others = summaries_of(session, runs_with_mode(session, key, run.id, SAME_MODE_RUNS), now)
        if others:
            runs = [
                {k: _brief(s)[k] for k in ("id", "name", "state", "started_at")} for s in others
            ]
            same_mode.append({"mode": safe(mode, NAME_CHARS), "runs": runs})
    counts[MODES_UNCHECKED] = max(len(modes) - MAX_MODES_SHOWN, 0)

    evidence = "failed_spans" if found else "labels_only" if all_labels else "none"
    result: Result = {
        "run": _brief(summary)
        | {
            "ended_at": run.ended_at,
            "spans": spans,
            "input_tokens": run.input_tokens,
            "output_tokens": run.output_tokens,
            "cost_usd": summary.known_cost,
            "cost_incomplete": summary.unpriced,
        },
        "failure_evidence": evidence,
        "failures": failures,
        "refused": refused,
        "asked": _text(asked, INPUT_CHARS),
        "answered": _text(answered, OUTPUT_CHARS),
        "decisions": decisions or None,
        "right_answer": right_answer,
        "labels": labels,
        "same_mode_runs": same_mode,
    }
    trims = [
        Trim(SAME_MODE, _pop_same_mode_run),
        Trim(LABELS, _pop("labels")),
        Trim(REFUSED, _pop("refused")),
        Trim(FAILURES, _pop("failures", keep=1)),
        Trim(DECISIONS, _pop_field("decisions")),
        Trim(ANSWER_FIELDS, _pop_field("right_answer", "fields")),
    ]
    return fit(cast("Result", safe(result)), trims, counts, MAX_RESULT_CHARS)


# --- search_runs -------------------------------------------------------------------------


def _when(value: str | None, now: datetime, name: str) -> datetime | None:
    if value is None or not value.strip():
        return None
    try:
        return parse_when(cast("str", _plain(value, name)), now)
    except ValueError as error:
        raise QueryError(f"{name}: {error}") from None


def search_runs(
    session: Session,
    embedder: Embedder,
    query: str,
    *,
    source: str | None = None,
    agent: str | None = None,
    status: str | None = None,
    since: str | None = None,
    until: str | None = None,
    limit: int = 10,
    attrs: dict[str, list[str]] | None = None,
    now: datetime,
) -> Result:
    """Runs that match a description, by meaning and by exact words, best first. `attrs`
    keeps runs whose attribute equals one of the listed values, for each key."""
    if not cast("str", _plain(query, "query")).strip():
        raise QueryError("say what to look for")
    if status is not None and status not in SEARCHABLE_STATES:
        raise QueryError(f"status {status!r}: use one of {', '.join(SEARCHABLE_STATES)}")
    if not 1 <= limit <= SEARCH_LIMIT:
        raise QueryError(f"limit is 1 to {SEARCH_LIMIT}")
    where = RunFilter(
        source=_plain(source, "source"),
        agent=_plain(agent, "agent"),
        status=status,  # type: ignore[arg-type]  # checked against SEARCHABLE_STATES
        since=_when(since, now, "since"),
        until=_when(until, now, "until"),
        limit=limit,
        attrs=_attrs(attrs or {}),
    )
    empty = not has_vectors(session, embedder.model)
    hits = [] if empty else search(session, embedder, query.strip(), where, now)
    result: Result = {
        "hits": [
            {
                "run": _brief(hit.summary),
                "similarity": round(hit.similarity, 4),
                "matched": hit.matched,
                "failed_span": (
                    {
                        "name": safe(hit.span_name, NAME_CHARS),
                        "error": safe(hit.span_error, ERROR_CHARS) if hit.span_error else None,
                    }
                    if hit.span_name
                    else None
                ),
            }
            for hit in hits
        ],
        "index_empty": empty,
        "unembedded_runs": pending_runs(session, embedder.model, now),
    }
    return fit(cast("Result", safe(result)), [Trim(HITS, _pop("hits"))], None, MAX_RESULT_CHARS)


# --- list_failure_modes ------------------------------------------------------------------


def failure_modes(session: Session, source: str = "human", limit: int = 30) -> Result:
    """The labelled failure modes with their run and label counts, most runs first."""
    if source not in LABEL_SOURCES:
        raise QueryError(f"source {source!r}: use one of {', '.join(LABEL_SOURCES)}")
    if not 1 <= limit <= MODES_LIMIT:
        raise QueryError(f"limit is 1 to {MODES_LIMIT}")
    counts = mode_counts(session, source)
    shown = counts[:limit]
    rows = session.execute(
        select(Run.id, Run.external_id).where(Run.id.in_([c.example for c in shown]))
    )
    examples: dict[uuid.UUID, str] = {row.id: row.external_id for row in rows}
    result: Result = {
        "source": source,
        "modes": [
            {
                "mode": safe(c.mode, NAME_CHARS),
                "runs": c.runs,
                "labels": c.labels,
                "example_run": {
                    "id": c.example,
                    "external_id": safe(examples.get(c.example), NAME_CHARS),
                },
            }
            for c in shown
        ],
    }
    return fit(
        cast("Result", safe(result)),
        [Trim(MODES, _pop("modes"))],
        {MODES: len(counts) - len(shown)},
        MAX_RESULT_CHARS,
    )
