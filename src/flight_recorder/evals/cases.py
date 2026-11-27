"""Eval cases written from recorded runs (#59), shared by `afr case` and the analyst (#22).

Cases go into git, and the repo is public, so the safety rules live here, not in a command:

- suite and case ids match `ID`, can't be `local`, `suite` or a Windows device name, and the file
  must resolve inside the cases folder; an existing case is never overwritten;
- the case is redacted again with today's patterns (a run recorded before a pattern was
  added still holds what it missed), then a redacted value (`[REDACTED:kind:fingerprint]`)
  anywhere in it stops the write until it's swapped for the fixed fake of its kind (`faked`)
  or kept without its fingerprint, with the case marked as using placeholders (`kept`);
- a case from a Claude Code session goes to `local/`, which holds its own `.gitignore`
  (`*`), so git ignores it wherever the cases folder is: redaction catches secret shapes,
  not everything personal;
- `preview` is the case as text safe to print; YAML is written with a safe dumper and read
  with a safe loader that also refuses aliases.

A case is `id`, `from_run`, `tags`, `input`, `expected` and `checks` (#15's format), plus
`unreviewed: true` when `expected` is only what the agent did, and `placeholders: true`. A
field's check is `equals`, or `one_of` when the recorded truth accepts other answers.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import yaml

from flight_recorder.redact.patterns import PATTERNS
from flight_recorder.safe_text import PLACEHOLDER, clean, map_strings, strip_fingerprints

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from flight_recorder.redact import Redactor
    from flight_recorder.store.models import Run
    from flight_recorder.store.read import SpanNode

ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
LOCAL = "local"  # the git-ignored folder for cases that may hold personal content
SUITE = "suite"  # suite.yaml holds a suite's settings (afr eval), so no case can be called that
LOCAL_SOURCES = ("claude_code",)  # a session's prompts and replies are the user's own work
# Names Windows won't create or check out, whatever the extension
DEVICES = {"con", "prn", "aux", "nul", *(f"{d}{n}" for d in ("com", "lpt") for n in range(10))}
SUFFIX = ".yaml"
# YAML line breaks that only a double-quoted string keeps as they are
BLOCK_BREAKS = (chr(0x85), chr(0x2028), chr(0x2029))  # NEL, line and paragraph separators


def fake(kind: str) -> str:
    """The fixed fake for a redacted value of this kind: no pattern or scanner takes it for
    a secret on its own."""
    return f"fake-{kind.replace('_', '-')}"


FAKES = {kind: fake(kind) for kind in sorted({p.name for p in PATTERNS})}


class CaseError(ValueError):
    pass


def check_id(value: str, what: str) -> str:
    """`value` when it's a safe file name and not reserved; CaseError otherwise."""
    if not ID.fullmatch(value):
        raise CaseError(f"{what} {value!r} must be 1 to 64 of a-z, 0-9, _ and -, "
                        "starting with a letter or digit")  # fmt: skip
    if value == LOCAL:
        raise CaseError(f"{what} can't be {LOCAL!r}: it's the folder git ignores")
    if value == SUITE:
        raise CaseError(f"{what} can't be {SUITE!r}: suite.yaml holds a suite's settings")
    if value in DEVICES:
        raise CaseError(f"{what} can't be {value!r}: Windows reserves it")
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


def suite_dir(root: Path, suite: str, *, local: bool) -> Path:
    """A suite's folder: `<root>/[local/]<suite>`, inside `root` even after following links."""
    base = root.resolve()
    path = base.joinpath(*([LOCAL] if local else []), check_id(suite, "suite"))
    if not path.resolve().is_relative_to(base):
        raise CaseError(f"{path} resolves outside {base}")
    return path


# --- placeholders -------------------------------------------------------------------


def _matches(case: dict[str, Any]) -> list[re.Match[str]]:
    found: list[re.Match[str]] = []

    def collect(text: str) -> str:
        found.extend(PLACEHOLDER.finditer(text))
        return text

    map_strings(case, collect)
    return found


def placeholders(case: dict[str, Any]) -> list[str]:
    """The redacted values in the case that still carry a fingerprint, each once."""
    return sorted({m[0] for m in _matches(case) if m["fp"]})


def problems(case: dict[str, Any]) -> list[str]:
    """Why the case can't be written yet: each fingerprinted placeholder, or bare ones in a
    case not marked as using placeholders."""
    if found := placeholders(case):
        return found
    if case.get("placeholders") is not True:
        return sorted({m[0] for m in _matches(case)})
    return []


def faked(case: dict[str, Any]) -> dict[str, Any]:
    """Each placeholder swapped for the fixed fake of its kind."""

    def swap(text: str) -> str:
        return PLACEHOLDER.sub(lambda m: fake(m["kind"]), text)

    return cast("dict[str, Any]", map_strings(case, swap))


def kept(case: dict[str, Any]) -> dict[str, Any]:
    """Placeholders kept without their fingerprints, and the case marked as using them
    (only if it has any)."""
    out = cast("dict[str, Any]", strip_fingerprints(case))
    return out | {"placeholders": True} if _matches(out) else out


# --- YAML ---------------------------------------------------------------------------


class _Dumper(yaml.SafeDumper):
    """Safe YAML for people to edit: no anchors and aliases for a value used twice (expected
    and its check), and multi-line text as a `|` block. The emitter quotes it instead when a
    block can't hold it (trailing spaces, tabs)."""

    def ignore_aliases(self, data: Any) -> bool:
        return True


def _text(dumper: yaml.SafeDumper, value: str) -> yaml.ScalarNode:
    # escaped when it holds a break any other style reads back as a newline or a space
    style = '"' if any(b in value for b in BLOCK_BREAKS) else "|" if "\n" in value else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", value, style)  # pyright: ignore[reportUnknownMemberType]


_Dumper.add_representer(str, _text)


def dump(case: dict[str, Any]) -> str:
    return yaml.dump(case, Dumper=_Dumper, sort_keys=False, allow_unicode=True, width=100)


def preview(case: dict[str, Any]) -> str:
    """The case as written, safe to print: control and invisible characters escaped."""
    return clean(dump(case))


class _NoAliases(yaml.SafeLoader):
    """Safe YAML without anchors and aliases: a few hundred bytes of nested aliases expand
    to gigabytes, and the dumper here never writes them."""

    def compose_node(self, parent: Any, index: Any) -> Any:
        if self.check_event(yaml.AliasEvent):
            raise yaml.YAMLError("aliases (*name) aren't allowed in a case")
        return super().compose_node(parent, index)  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]


def load(text: str) -> dict[str, Any]:
    try:
        case = yaml.load(text, Loader=_NoAliases)  # a SafeLoader subclass
    except yaml.YAMLError as error:
        raise CaseError(f"not a case: {error}") from error
    except RecursionError as error:
        raise CaseError("not a case: nested too deep") from error
    if not isinstance(case, dict):
        raise CaseError("not a case: expected a mapping")
    return cast("dict[str, Any]", case)


# --- drafting and writing a case ----------------------------------------------------------


@dataclass(frozen=True)
class Draft:
    case: dict[str, Any]
    suite: str
    local: bool  # from a Claude Code session: goes to the git-ignored local/ folder

    def path(self, root: Path) -> Path:
        return case_path(root, self.suite, str(self.case.get("id", "")), local=self.local)

    def with_case(self, case: dict[str, Any]) -> Draft:
        """The same draft with its case changed (faked, kept); the routing stays."""
        return replace(self, case=case)


def write_case(root: Path, draft: Draft) -> Path:
    """Writes the draft's case under `root` and returns its path. Refuses a case with
    placeholders (see `problems`), a bad id, or one that exists."""
    if found := problems(draft.case):
        raise CaseError("the case has redacted values: " + ", ".join(found))
    path = draft.path(root)
    try:
        if draft.local:
            folder = root.resolve() / LOCAL
            folder.mkdir(parents=True, exist_ok=True)
            ignore = folder / ".gitignore"
            if not ignore.exists():
                ignore.write_text("# cases that may hold personal content (afr case)\n*\n",
                                  encoding="utf-8")  # fmt: skip
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as error:  # a file where a folder goes, a device name, no permission
        raise CaseError(f"can't write {path}: {error.strerror or error}") from error
    try:
        with path.open("x", encoding="utf-8", newline="\n") as file:
            file.write(dump(draft.case))
    except FileExistsError as error:
        raise CaseError(f"{path} exists; cases are never overwritten") from error
    except OSError as error:
        raise CaseError(f"can't write {path}: {error.strerror or error}") from error
    return path


def _input(asked: Any) -> dict[str, Any]:
    if isinstance(asked, dict):
        return cast("dict[str, Any]", asked)
    return {"prompt": asked} if isinstance(asked, str) else {"input": asked}


def _decoded(fields: dict[str, Any]) -> dict[str, Any]:
    """Recorded fields with the recorder's "nothing" as null, at the top level."""
    from flight_recorder.payloads import none_as_null

    return {k: none_as_null(v) for k, v in fields.items()}


def _as_expected(output: Any) -> dict[str, Any]:
    if output is None or output == "":
        return {}
    return cast("dict[str, Any]", output) if isinstance(output, dict) else {"answer": output}


def agent_output(session: Session, run: Run, roots: list[SpanNode]) -> dict[str, Any]:
    """What the agent answered, as recorded, not as a summary shows it: the entry span's
    output; for a Claude Code session, its last reply; for a LangChain run whose entry span
    recorded none, the newest model call (by time) that answered: its structured output (a
    tool call no tool span ran) as its arguments, else what it said, in full. A call whose
    tool calls all ran is a step, not an answer."""
    from flight_recorder.payloads import ai, as_dict, message_text, tool_calls
    from flight_recorder.search.summary import conversation
    from flight_recorder.store.read import span_content

    if not roots:
        return {}
    if run.source in LOCAL_SOURCES:
        return _as_expected(conversation(session, run, roots)[1])
    output = span_content(session, roots[0].id).output
    if output is not None and output != "":
        return _as_expected(output)
    nodes = sorted(
        (n for r in roots for n in r.walk()), key=lambda n: (n.started_at, n.external_id)
    )
    tools = {n.name for n in nodes if n.kind == "tool"}
    for node in reversed([n for n in nodes if n.kind == "llm"]):  # newest first
        if node.status == "error":  # a failed call answered nothing
            continue
        result = span_content(session, node.id).output
        calls = tool_calls(result)
        for call in reversed(calls):
            found = as_dict(call)
            if found is None:
                continue
            name = found.get("name")
            # A recorded name is any JSON value. Only a string can be a tool span's name, and
            # looking a list or an object up in the set would raise.
            if not (isinstance(name, str) and name in tools):
                return as_dict(found.get("args")) or {}
        if not calls:
            return _as_expected(message_text((ai(result) or {}).get("content")) or None)
    return {}


def accepted_value(truth: Any, entry: Any, decided: Any = None) -> Any:
    """One accepted alternative in the field's shape: a list field's entry is one string
    joined by ", " (the triager's format), and `(none)` is nothing. A field is a list field
    when its truth or the agent's decision is a list (a truth of nothing is recorded as
    `(none)`, which says neither)."""
    from flight_recorder.payloads import NONE_TEXT

    if (isinstance(truth, list) or isinstance(decided, list)) and isinstance(entry, str):
        return [] if entry == NONE_TEXT else entry.split(", ")
    return None if entry == NONE_TEXT else entry


def truth_checks(
    truth: dict[str, Any], accepted: dict[str, list[Any]], decided: dict[str, Any] | None = None
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """The expected fields and their checks from a recorded truth: `equals` the truth, or
    `one_of` the truth then each accepted alternative not already listed. `decided` (the
    agent's decisions) tells a list field whose truth is nothing from a single-value one."""
    from flight_recorder.evals.checks import same

    expected = dict(sorted(_decoded(truth).items()))
    checks: list[dict[str, Any]] = []
    for field, value in expected.items():
        values = [value]
        for entry in accepted.get(field, []):
            alternative = accepted_value(truth[field], entry, (decided or {}).get(field))
            if not any(same(alternative, v) for v in values):
                values.append(alternative)
        if len(values) == 1:
            checks.append({"type": "equals", "field": field, "value": value})
        else:
            checks.append({"type": "one_of", "field": field, "values": values})
    return expected, checks


def draft_case(
    session: Session,
    run_id: uuid.UUID,
    *,
    suite: str,
    redactor: Redactor,
    case_id: str | None = None,
) -> Draft:
    """The case a recorded run makes: its input; the right answer from its labels, else the
    recorded truth (`<prefix>.expected.*`, with its accepted alternatives as `one_of`) unless a
    human marked that truth as bad, else what the agent did (`unreviewed`); its failure modes
    as tags; all redacted again by `redactor`. Raises CaseError for a bad id or a run with no
    recorded input."""
    from flight_recorder.search.summary import conversation
    from flight_recorder.store.labels import (
        HUMAN,
        accepted_fields,
        answer_of,
        decided_fields,
        expected_fields,
        has_truth_problem,
        labels_of,
    )
    from flight_recorder.store.models import Run
    from flight_recorder.store.read import span_tree

    check_id(suite, "suite")
    case_id = check_id(case_id or f"run-{str(run_id)[:8]}", "case id")
    run = session.get(Run, run_id)
    if run is None:
        raise CaseError(f"no run {run_id}")
    roots = span_tree(session, run.id)
    asked, _ = conversation(session, run, roots)
    if asked is None or asked == "":
        raise CaseError(f"run {str(run_id)[:8]} has no recorded input to replay")
    answer = answer_of(session, run.id)
    attributes = run.attributes or {}
    truth = expected_fields(attributes)
    checks: list[dict[str, Any]] | None = None
    unreviewed = False
    if answer is not None:
        found = answer.get("fields")
        expected = (
            _decoded(cast("dict[str, Any]", found))
            if found is not None
            else {"answer": answer.get("text")}
        )
    elif truth and not has_truth_problem(session, run.id):
        expected, checks = truth_checks(truth, accepted_fields(attributes),
                                        decided_fields(attributes))  # fmt: skip
    else:
        unreviewed = True
        decided = decided_fields(attributes)
        expected = _decoded(decided) if decided else agent_output(session, run, roots)
    tags = list(dict.fromkeys(
        label.failure_mode for label in labels_of(session, run.id)
        if label.source == HUMAN and label.span_id is None
    ))  # fmt: skip
    # the recorded parts, again: not the ids, which a pattern may take for a secret
    recorded = redactor.redact({"input": _input(asked), "expected": expected, "tags": tags,
                                "checks": checks})  # fmt: skip
    parts = cast("dict[str, Any]", recorded.value)
    expected = dict(sorted(parts["expected"].items()))  # JSONB keeps its own key order
    case: dict[str, Any] = {"id": case_id, "from_run": run.external_id, "tags": parts["tags"]}
    if unreviewed:
        case["unreviewed"] = True  # expected is what the agent did, not the right answer
    case |= {
        "input": parts["input"],
        "expected": expected,
        "checks": parts["checks"]
        or [{"type": "equals", "field": k, "value": v} for k, v in expected.items()],
    }
    return Draft(case=case, suite=suite, local=run.source in LOCAL_SOURCES)
