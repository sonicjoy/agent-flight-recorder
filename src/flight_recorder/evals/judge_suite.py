"""Judge suites: cases for the LLM judge, written from an items file and a rubric (#17).

The judge (`flight_recorder.evals.judge:judge`) is an agent under test: each case holds one item
to judge in its input, and the owner's hand grade as its only check, so the suite's pass rate
is the judge's agreement with the owner. The design is docs/designs/llm-judge.md.

This module never imports the LLM layer, so `afr eval judge-suite` runs with no key and
without loading LangChain. It holds the judge's input rules (`check_input`), which the adapter
applies again before each call.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from flight_recorder.evals.cases import ID, SUFFIX, CaseError, check_id, dump, load

VERSION = 1
"""The judge's version, copied into every case input: raise it when the prompt, the reply
schema or the grading rule changes, so every saved output goes stale."""

AGENT = "flight_recorder.evals.judge:judge"
INPUT_KEYS = ("judge_version", "model", "rubric", "item")
RUBRIC_KEYS = ("instructions", "criteria", "examples")
RUBRIC_FILE_KEYS = ("model", "instructions", "criteria", "examples")
ITEM_KEYS = ("id", "tag", "item", "grade", "targets", "author", "note")
TAGS = ("corpus", "broken", "holdout")
GRADES = ("pass", "fail")
RESERVED = "reason"  # the reply's own field: no criterion can be called that


class ItemsError(ValueError):
    """Items that can't become cases, every one with its problem."""

    def __init__(self, problems: list[tuple[str, str]]) -> None:
        super().__init__("; ".join(f"{item}: {problem}" for item, problem in problems))
        self.problems = problems


def _text(value: Any) -> bool:
    return isinstance(value, str)


def check_input(input: Any) -> None:
    """The judge's input rules. Raises ValueError naming the first problem."""
    if not isinstance(input, dict) or set(cast("dict[str, Any]", input)) != set(INPUT_KEYS):
        raise ValueError(f"the input must have exactly the keys {', '.join(INPUT_KEYS)}")
    data = cast("dict[str, Any]", input)
    version = data["judge_version"]
    if type(version) is not int or version != VERSION:
        raise ValueError(f"judge_version must be {VERSION}, got {version!r}: regenerate the suite")
    if not _text(data["model"]):
        raise ValueError("model must be text")
    check_rubric(data["rubric"])
    if not isinstance(data["item"], dict):
        raise ValueError("item must be a mapping")


def check_rubric(rubric: Any) -> list[str]:
    """The rubric part of an input; returns its criterion ids. Raises ValueError."""
    if not isinstance(rubric, dict) or set(cast("dict[str, Any]", rubric)) != set(RUBRIC_KEYS):
        raise ValueError(f"rubric must have exactly the keys {', '.join(RUBRIC_KEYS)}")
    data = cast("dict[str, Any]", rubric)
    if not _text(data["instructions"]):
        raise ValueError("rubric instructions must be text")
    criteria = data["criteria"]
    if not isinstance(criteria, list) or not criteria:
        raise ValueError("rubric criteria must be a non-empty list")
    ids: list[str] = []
    for criterion in cast("list[Any]", criteria):
        if not isinstance(criterion, dict) or set(cast("dict[str, Any]", criterion)) != {
            "id",
            "pass",
            "fail",
        }:
            raise ValueError("each criterion must be {id, pass, fail}")
        entry = cast("dict[str, Any]", criterion)
        if not all(_text(entry[k]) for k in ("id", "pass", "fail")):
            raise ValueError("a criterion's id, pass and fail must be text")
        if not ID.fullmatch(entry["id"]) or entry["id"] == RESERVED:
            raise ValueError(f"criterion id {entry['id']!r} must match {ID.pattern} "
                             f"and not be {RESERVED!r}")  # fmt: skip
        if entry["id"] in ids:
            raise ValueError(f"criterion id {entry['id']!r} repeats")
        ids.append(entry["id"])
    examples = data["examples"]
    if not isinstance(examples, list):
        raise ValueError("rubric examples must be a list")
    for example in cast("list[Any]", examples):
        if not isinstance(example, dict) or set(cast("dict[str, Any]", example)) != {
            "item",
            "criteria",
            "reason",
        }:
            raise ValueError("each example must be {item, criteria, reason}")
        entry = cast("dict[str, Any]", example)
        verdicts = entry["criteria"]
        if (
            not isinstance(entry["item"], dict)
            or not _text(entry["reason"])
            or not isinstance(verdicts, dict)
            or set(cast("dict[str, Any]", verdicts)) != set(ids)
            or any(v not in GRADES for v in cast("dict[str, Any]", verdicts).values())
        ):
            raise ValueError("an example needs an item mapping, a pass or fail for every "
                             "criterion, and a text reason")  # fmt: skip
    return ids


def load_rubric(path: Path) -> dict[str, Any]:
    """The rubric file: model, instructions, criteria and optional examples. Raises CaseError."""
    rubric = load(_read(path))
    unknown = sorted(set(rubric) - set(RUBRIC_FILE_KEYS))
    missing = [k for k in ("model", "instructions", "criteria") if k not in rubric]
    if unknown or missing:
        raise CaseError(f"{path.name}: unknown keys {unknown}, missing keys {missing}")
    return {"examples": [], **rubric}


def load_items(path: Path) -> list[dict[str, Any]]:
    """The items file, `{items: [...]}`. Raises CaseError for any other shape."""
    data = load(_read(path))
    items = data.get("items")
    if set(data) != {"items"} or not isinstance(items, list):
        raise CaseError(f"{path.name}: must hold only `items:`, a list")
    return cast("list[dict[str, Any]]", items)


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise CaseError(f"{path.name}: can't read it: {error}") from error


def _item_problems(raw: Any, ids: list[str], seen: set[str]) -> tuple[str, list[str]]:
    if not isinstance(raw, dict):
        return "?", ["not a mapping"]
    entry = cast("dict[str, Any]", raw)
    name = entry.get("id")
    label = name if isinstance(name, str) else "?"
    problems: list[str] = []
    if not isinstance(name, str):
        problems.append("id must be text")
    else:
        try:
            check_id(name, "item id")
        except CaseError as error:
            problems.append(str(error))
        if name in seen:
            problems.append("id repeats")
        seen.add(name)
    if unknown := sorted(set(entry) - set(ITEM_KEYS)):
        problems.append(f"unknown keys {unknown}")
    if entry.get("tag") not in TAGS:
        problems.append(f"tag must be one of {', '.join(TAGS)}")
    if entry.get("grade") not in GRADES:
        problems.append("grade must be pass or fail")
    item = entry.get("item")
    if (
        not isinstance(item, dict)
        or set(cast("dict[str, Any]", item)) != {"comment", "title", "decision"}
        or not _text(cast("dict[str, Any]", item)["comment"])
        or not _text(cast("dict[str, Any]", item)["title"])
        or not isinstance(cast("dict[str, Any]", item)["decision"], dict)
    ):
        problems.append("item must be {comment: text, title: text, decision: mapping}")
    if "targets" in entry and entry["targets"] not in ids:
        problems.append(f"targets must be a criterion id: {', '.join(ids)}")
    for key in ("author", "note"):
        if key in entry and not _text(entry[key]):
            problems.append(f"{key} must be text")
    return label, problems


def _note(entry: dict[str, Any]) -> str | None:
    parts = [f"targets: {entry['targets']}" if "targets" in entry else None,
             f"author: {entry['author']}" if "author" in entry else None,
             entry.get("note")]  # fmt: skip
    joined = "; ".join(p for p in parts if p)
    return joined or None


def build_suite(items: list[dict[str, Any]], rubric: dict[str, Any]) -> dict[str, str]:
    """File name to file contents for the suite. Raises ItemsError listing every bad item, or
    CaseError for a rubric the judge would refuse."""
    part = {k: rubric[k] for k in RUBRIC_KEYS}
    try:
        ids = check_rubric(part)
        check_input({"judge_version": VERSION, "model": rubric["model"], "rubric": part,
                     "item": {}})  # fmt: skip
    except ValueError as error:
        raise CaseError(f"rubric: {error}") from error
    problems: list[tuple[str, str]] = []
    seen: set[str] = set()
    for raw in items:
        label, found = _item_problems(raw, ids, seen)
        problems += [(label, p) for p in found]
    if problems:
        raise ItemsError(problems)
    files = {f"suite{SUFFIX}": dump({"agent": AGENT, "model": rubric["model"]})}
    for entry in items:
        case: dict[str, Any] = {"id": entry["id"], "tags": [entry["tag"]]}
        if (note := _note(entry)) is not None:
            case["note"] = note
        case["input"] = {"judge_version": VERSION, "model": rubric["model"], "rubric": part,
                         "item": entry["item"]}  # fmt: skip
        case["checks"] = [{"type": "equals", "field": "grade", "value": entry["grade"]}]
        files[f"{entry['id']}{SUFFIX}"] = dump(case)
    return files


def write_suite(folder: Path, files: dict[str, str]) -> None:
    """Writes every file (replacing what's there) and deletes the folder's other case files.
    outputs.jsonl is left alone, and no baseline.json is ever written: the suite never gates."""
    folder.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        with (folder / name).open("w", encoding="utf-8", newline="\n") as file:
            file.write(text)
    for stray in folder.glob(f"*{SUFFIX}"):
        if stray.name not in files:
            stray.unlink()
