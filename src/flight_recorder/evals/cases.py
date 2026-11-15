"""Eval cases written from recorded runs (#59), shared by `afr case` and the analyst (#22).

Cases go into git, and the repo is public, so the safety rules live here, not in a command:

- suite and case ids match `ID` and can't be `local`, and the file must resolve inside the
  cases folder; an existing case is never overwritten;
- a redacted value (`[REDACTED:kind:fingerprint]`) anywhere in a case stops the write until
  it's swapped for the fixed fake of its kind (`faked`) or kept without its fingerprint, with
  the case marked as using placeholders (`kept`);
- a case from a Claude Code session goes to `local/`, which git ignores: redaction catches
  secret shapes, not everything personal;
- YAML is written with `safe_dump` and read with `safe_load`.

A case is `id`, `from_run`, `tags`, `input`, `expected` and `checks` (#15's format), plus
`unreviewed: true` when `expected` is only what the agent did, and `placeholders: true`.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import yaml

from flight_recorder.redact.patterns import PATTERNS

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
LOCAL = "local"  # the git-ignored folder for cases that may hold personal content
LOCAL_SOURCES = ("claude_code",)  # a session's prompts and replies are the user's own work
PLACEHOLDER = re.compile(r"\[REDACTED:(?P<kind>[a-z0-9_]+)(?::(?P<fp>[0-9a-f]{8}))?\]")
FAKES = {kind: f"fake-{kind.replace('_', '-')}" for kind in sorted({p.name for p in PATTERNS})}
SUFFIX = ".yaml"


class CaseError(ValueError):
    pass


def check_id(value: str, what: str) -> str:
    """`value` when it's a safe file name and not reserved; CaseError otherwise."""
    if not ID.fullmatch(value):
        raise CaseError(f"{what} {value!r} must be 1 to 64 of a-z, 0-9, _ and -, "
                        "starting with a letter or digit")  # fmt: skip
    if value == LOCAL:
        raise CaseError(f"{what} can't be {LOCAL!r}: it's the folder git ignores")
    return value


def case_path(root: Path, suite: str, case_id: str, *, local: bool) -> Path:
    """Where the case goes: `<root>/[local/]<suite>/<case_id>.yaml`, inside `root` even
    after following links."""
    base = root.resolve()
    path = base.joinpath(*([LOCAL] if local else []), check_id(suite, "suite"),
                         check_id(case_id, "case id") + SUFFIX)  # fmt: skip
    if not path.resolve().is_relative_to(base):
        raise CaseError(f"{path} resolves outside {base}")
    return path


# --- placeholders -------------------------------------------------------------------


def _strings(value: Any, change: Callable[[str], str]) -> Any:
    """`value` with `change` applied to every string at any depth, dict keys included."""
    if isinstance(value, str):
        return change(value)
    if isinstance(value, dict):
        items = cast("dict[Any, Any]", value)
        return {_strings(k, change): _strings(v, change) for k, v in items.items()}
    if isinstance(value, list | tuple):
        return [_strings(v, change) for v in cast("list[Any]", value)]
    return value


def _matches(case: dict[str, Any]) -> list[re.Match[str]]:
    found: list[re.Match[str]] = []

    def collect(text: str) -> str:
        found.extend(PLACEHOLDER.finditer(text))
        return text

    _strings(case, collect)
    return found


def placeholders(case: dict[str, Any]) -> list[str]:
    """The redacted values in the case that still carry a fingerprint, each once."""
    return sorted({m[0] for m in _matches(case) if m["fp"]})


def problems(case: dict[str, Any]) -> list[str]:
    """Why the case can't be written yet: each fingerprinted placeholder, or bare ones in a
    case not marked as using placeholders."""
    if placeholders(case):
        return placeholders(case)
    if case.get("placeholders") is not True:
        return sorted({m[0] for m in _matches(case)})
    return []


def faked(case: dict[str, Any]) -> dict[str, Any]:
    """Each placeholder swapped for the fixed fake of its kind, which no scanner or redactor
    mistakes for a real secret."""

    def fake(text: str) -> str:
        return PLACEHOLDER.sub(lambda m: FAKES.get(m["kind"], f"fake-{m['kind']}"), text)

    return cast("dict[str, Any]", _strings(case, fake))


def kept(case: dict[str, Any]) -> dict[str, Any]:
    """Placeholders kept without their fingerprints, and the case marked as using them."""

    def bare(text: str) -> str:
        return PLACEHOLDER.sub(lambda m: f"[REDACTED:{m['kind']}]", text)

    return cast("dict[str, Any]", _strings(case, bare)) | {"placeholders": True}


# --- YAML ---------------------------------------------------------------------------


def dump(case: dict[str, Any]) -> str:
    return yaml.safe_dump(case, sort_keys=False, allow_unicode=True, width=100)


def load(text: str) -> dict[str, Any]:
    try:
        case = yaml.safe_load(text)
    except yaml.YAMLError as error:
        raise CaseError(f"not a case: {error}") from error
    if not isinstance(case, dict):
        raise CaseError("not a case: expected a mapping")
    return cast("dict[str, Any]", case)


def write_case(root: Path, suite: str, case: dict[str, Any], *, local: bool) -> Path:
    """Writes the case under `root` and returns its path. Refuses a case with placeholders
    (see `problems`), a bad id, or one that exists."""
    if found := problems(case):
        raise CaseError("the case has redacted values: " + ", ".join(found))
    path = case_path(root, suite, str(case.get("id", "")), local=local)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf-8", newline="\n") as file:
            file.write(dump(case))
    except FileExistsError as error:
        raise CaseError(f"{path} exists; cases are never overwritten") from error
    return path


# --- drafting a case from a run --------------------------------------------------------


@dataclass(frozen=True)
class Draft:
    case: dict[str, Any]
    suite: str
    local: bool  # goes to the git-ignored local/ folder


def _bare(fields: dict[str, Any]) -> dict[str, Any]:
    """`<prefix>.<field>` keys as `<field>`, unless two prefixes share a field name."""
    names = [key.rsplit(".", 1)[-1] for key in fields]
    return {
        (name if names.count(name) == 1 else key): value
        for key, name, value in zip(fields, names, fields.values(), strict=True)
    }


def _input(asked: Any) -> dict[str, Any]:
    if isinstance(asked, dict):
        return cast("dict[str, Any]", asked)
    return {"prompt": asked} if isinstance(asked, str) else {"input": asked}


def draft_case(
    session: Session, run_id: uuid.UUID, *, suite: str, case_id: str | None = None
) -> Draft:
    """The case a recorded run makes: its input; the right answer from its labels, else what
    the agent did (`unreviewed`); an `equals` check per expected field; its failure modes as
    tags. Raises CaseError for a bad id or a run with no recorded input."""
    from flight_recorder.search.summary import conversation
    from flight_recorder.store.labels import HUMAN, answer_of, decided_fields, labels_of
    from flight_recorder.store.models import Run
    from flight_recorder.store.read import span_tree

    check_id(suite, "suite")
    case_id = check_id(case_id or f"run-{str(run_id)[:8]}", "case id")
    run = session.get(Run, run_id)
    if run is None:
        raise CaseError(f"no run {run_id}")
    asked, answered = conversation(session, run, span_tree(session, run.id))
    if asked is None or asked == "":
        raise CaseError(f"run {str(run_id)[:8]} has no recorded input to replay")
    answer = answer_of(session, run.id)
    decided = decided_fields(run.attributes or {})
    if answer is not None:
        expected = _bare(answer["fields"]) if "fields" in answer else {"answer": answer["text"]}
    elif decided:
        expected = _bare(decided)
    elif isinstance(answered, dict):
        expected = cast("dict[str, Any]", answered)
    else:
        expected = {} if answered is None else {"answer": answered}
    tags: list[str] = []
    for label in labels_of(session, run.id):
        if label.source == HUMAN and label.span_id is None and label.failure_mode not in tags:
            tags.append(label.failure_mode)
    case: dict[str, Any] = {"id": case_id, "from_run": run.external_id, "tags": tags}
    if answer is None:
        case["unreviewed"] = True  # expected is what the agent did, not the right answer
    case |= {
        "input": _input(asked),
        "expected": expected,
        "checks": [{"type": "equals", "field": k, "value": v} for k, v in expected.items()],
    }
    return Draft(case=case, suite=suite, local=run.source in LOCAL_SOURCES)
