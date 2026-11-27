"""Items and signatures (docs/designs/analyst.md §2): one item per wrong field of a graded run,
and one per error run, each with a signature saying exactly how it went wrong.

Plain code reads only the run's attributes and its root-cause failure, never a label's mode,
note or answer: #23 scores the groups against those labels.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from datetime import UTC
from typing import Any, cast

from sqlalchemy.orm import Session

from flight_recorder.analyst.select import Failed
from flight_recorder.evals.cases import accepted_value
from flight_recorder.evals.checks import same
from flight_recorder.payloads import NONE_TEXT, decided_expected_pairs, none_as_null, value_text
from flight_recorder.redact import Redactor
from flight_recorder.safe_text import PLACEHOLDER, clean
from flight_recorder.search.summary import failure_line, root_failures
from flight_recorder.store.labels import accepted_fields
from flight_recorder.store.read import SpanNode, span_tree

TEXT_CHARS = 600
CLASS_CHARS = 40
SIGNATURE_CHARS = 120
DIGITS = re.compile(r"[0-9]")
REDACTED_PART = "~redacted"  # `~` is never left as is by _encoded


def _as_set(value: Any) -> set[str]:
    if value is None:
        return set()
    if isinstance(value, list):
        return {str(v) for v in cast("list[Any]", value)}
    return {str(value)}


def signature(field: str, decided: Any, expected: Any) -> str:
    """`<field>:extra`, `<field>:missed` or `<field>:wrong`, from the truth's side."""
    if isinstance(decided, list) or isinstance(expected, list):
        got, want = _as_set(decided), _as_set(expected)
        if got - want and not want - got:
            return f"{field}:extra"
        if want - got and not got - want:
            return f"{field}:missed"
        return f"{field}:wrong"
    if expected is None and decided is not None:
        return f"{field}:extra"
    if decided is None and expected is not None:
        return f"{field}:missed"
    return f"{field}:wrong"


def _shown(value: Any) -> str:
    return NONE_TEXT if value is None else value_text(value)


def _wrong(attributes: dict[str, Any], prefix: str, field: str, decided: Any, expected: Any,
           accepted: list[Any]) -> bool:  # fmt: skip
    grade = attributes.get(f"{prefix}.grade.{field}")
    if grade is not None:
        return grade == "wrong"
    if same(decided, expected):
        return False
    return not any(same(decided, accepted_value(expected, entry, decided)) for entry in accepted)


def graded_items(run: str, started: str, attributes: dict[str, Any]) -> list[dict[str, Any]]:
    """One item per field the agent got wrong, in field order."""
    accepted = accepted_fields(attributes)
    found: list[dict[str, Any]] = []
    for prefix, field, decided_key, expected_key in decided_expected_pairs(attributes):
        decided = none_as_null(attributes[decided_key])
        expected = none_as_null(attributes[expected_key])
        if not _wrong(attributes, prefix, field, decided, expected, accepted.get(field, [])):
            continue
        found.append({
            "id": f"{run}#{field}",
            "run": run,
            "started": started,
            "signature": signature(field, decided, expected),
            "text": f"{field}: decided {_shown(decided)}; expected {_shown(expected)}",
        })  # fmt: skip
    return sorted(found, key=lambda item: item["id"])


def error_signature(kind: str, name: str, error: str | None) -> str:
    """`error:<kind>:<name>:<class>`: the error text before its first `:`, numbers as 0."""
    text = (error or "").strip()
    found = DIGITS.sub("0", text.split(":", 1)[0].strip())[:CLASS_CHARS] if text else ""
    return f"error:{kind}:{name}:{found or 'unknown'}"


def _text(text: str, redactor: Redactor) -> str:
    """What the model may see: hidden characters made visible, redacted again, cut."""
    return str(redactor.redact(clean(text)).value)[:TEXT_CHARS]


def _encoded(part: str) -> str:
    """`part` with every character but letters, digits and `_.-` percent-encoded (UTF-8), `%`
    and `:` included: one line, and two different parts never encode alike."""
    return "".join(
        c if c.isalnum() or c in "_.-" else "".join(f"%{b:02X}" for b in c.encode()) for c in part
    )


def safe_signature(text: str, redactor: Redactor) -> str:
    """A signature fit for a prompt, a tag and the log (an error's span name is recorded text):
    each `:`-separated part redacted again on its own (`items_for` redacts an error's span
    name and text whole before they become parts, so a secret spanning a `:` is caught
    there), each placeholder, from recording or from here, becoming `~redacted` (a
    fingerprint would depend on a machine's key, and encoded it would hide from `problems`),
    the rest encoded. Longer than SIGNATURE_CHARS, it keeps a prefix and a hash of the whole,
    so different signatures stay different."""
    marked = PLACEHOLDER.sub("\0", text)  # a placeholder holds `:`; keep it in one part
    parts: list[str] = []
    for part in marked.split(":"):
        again = PLACEHOLDER.sub("\0", str(redactor.redact(part).value))
        parts.append(REDACTED_PART.join(_encoded(piece) for piece in again.split("\0")))
    joined = ":".join(parts)
    if len(joined) > SIGNATURE_CHARS:
        digest = hashlib.sha256(joined.encode()).hexdigest()[:8]
        joined = f"{joined[: SIGNATURE_CHARS - 9]}~{digest}"
    return joined or "unknown"


def error_item(run: str, started: str, node: SpanNode, redactor: Redactor) -> dict[str, Any]:
    """An error run's one item. Its span name and error text are recorded text, redacted again
    whole here, before the signature splits them at `:`."""

    def again(value: str | None) -> str | None:
        return None if value is None else str(redactor.redact(value).value)

    name = again(node.name) or ""
    return {"id": f"{run}#error", "run": run, "started": started,
            "signature": error_signature(node.kind, name, again(node.error)),
            "text": failure_line(node)}  # fmt: skip


def items_for(
    session: Session, failed: list[Failed], redactor: Redactor
) -> tuple[list[dict[str, Any]], int]:
    """Every failed run's items, and how many runs gave none ("no difference recorded")."""
    found: list[dict[str, Any]] = []
    empty = 0
    for entry in failed:
        run = entry.run.run
        run_id = str(run.id)
        started = run.started_at.astimezone(UTC).isoformat(timespec="seconds")
        mine = graded_items(run_id, started, dict(run.attributes or {}))
        if entry.run.state == "error" or "error" in entry.reasons:
            roots = root_failures(span_tree(session, uuid.UUID(run_id)))
            if roots:
                mine.append(error_item(run_id, started, roots[0], redactor))
        if not mine:
            empty += 1
        found += [item | {"text": _text(item["text"], redactor),
                          "signature": safe_signature(item["signature"], redactor)}
                  for item in mine]  # fmt: skip
    return found, empty
