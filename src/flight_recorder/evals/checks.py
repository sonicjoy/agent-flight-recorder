"""The generic checks `afr eval` grades with (#15, docs/evals/harness.md).

A check is `{type, field, ...}` on an agent's output. Four types, all built on one equality:

- `null`, a missing field and `[]` are one "nothing";
- lists of plain values compare as multisets, lists holding dicts or lists in order;
- numbers compare by value, but a boolean never equals a number;
- strings compare exactly; dicts compare key by key under the same rules.

The value conversion and the canonical JSON live here too, because the check key and the
input hash must both use them: they are committed in `baseline.json` and `outputs.jsonl`,
so changing either formula re-baselines every suite.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, cast

# --- value conversion and canonical JSON ----------------------------------------------


def convert(value: Any) -> Any:
    """`value` as JSON can hold it: a Pydantic model dumped, dates in ISO form, tuples as
    lists, sets as sorted lists. Raises ValueError("output not JSON: <type>") otherwise."""
    dump = getattr(value, "model_dump", None)
    if callable(dump) and not isinstance(value, type):
        return convert(dump(mode="json"))
    if value is None or isinstance(value, str | bool | int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("output not JSON: float")
        return value
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in cast("dict[Any, Any]", value).items():
            if not isinstance(key, str):
                raise ValueError(f"output not JSON: {type(key).__name__}")
            out[key] = convert(item)
        return out
    if isinstance(value, list | tuple):
        return [convert(item) for item in cast("list[Any]", value)]
    if isinstance(value, set | frozenset):
        items = [convert(item) for item in cast("set[Any]", value)]
        return sorted(items, key=canonical)
    if isinstance(value, dt.date | dt.time):  # datetime is a date
        return value.isoformat()
    raise ValueError(f"output not JSON: {type(value).__name__}")


def canonical(value: Any) -> str:
    """The one serialiser for every hash and for `outputs.jsonl`."""
    return json.dumps(
        convert(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


# --- looking up a field ---------------------------------------------------------------


def lookup(output: dict[str, Any] | None, field: str) -> tuple[bool, Any]:
    """`(found, value)`: the exact key first (recorded names can hold dots), else the
    dotted path through nested dicts. A path through anything but a dict is missing."""
    if output is None:
        return False, None
    if field in output:
        return True, output[field]
    value: Any = output
    for part in field.split("."):
        if not isinstance(value, dict) or part not in value:
            return False, None
        value = cast("dict[str, Any]", value)[part]
    return True, value


# --- equality -------------------------------------------------------------------------


def _nothing(value: Any) -> bool:
    return value is None or (isinstance(value, list) and not value)


def _plain(value: Any) -> bool:
    return not isinstance(value, dict | list)


def same(a: Any, b: Any) -> bool:
    """Equality for checks; see the module docstring."""
    if _nothing(a) or _nothing(b):
        return _nothing(a) and _nothing(b)
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a == b
    if isinstance(a, int | float) and isinstance(b, int | float):
        return a == b
    if isinstance(a, str) or isinstance(b, str):
        return isinstance(a, str) and isinstance(b, str) and a == b
    if isinstance(a, dict) and isinstance(b, dict):
        x, y = cast("dict[str, Any]", a), cast("dict[str, Any]", b)
        return all(same(x.get(k), y.get(k)) for k in x.keys() | y.keys())
    if isinstance(a, list) and isinstance(b, list):
        x, y = cast("list[Any]", a), cast("list[Any]", b)
        if len(x) != len(y):
            return False
        if all(map(_plain, x)) and all(map(_plain, y)):
            return _same_multiset(x, y)
        return all(same(i, j) for i, j in zip(x, y, strict=True))
    return False


def _same_multiset(a: list[Any], b: list[Any]) -> bool:
    """Same items in any order, by `same`: a Counter would count True as 1."""
    left = list(b)
    for item in a:
        for i, other in enumerate(left):
            if same(item, other):
                del left[i]
                break
        else:
            return False
    return not left


# --- checks ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CheckResult:
    field: str
    key: str  # "<field>/<8 hex>": the check's entry in baseline.json
    expected: Any
    actual: Any
    passed: bool
    reason: str  # "ok", "wrong value" or "missing"


def check_key(check: dict[str, Any]) -> str:
    digest = hashlib.sha256(canonical(check).encode()).hexdigest()
    return f"{check['field']}/{digest[:8]}"


def _result(check: dict[str, Any], expected: Any, actual: Any, passed: bool, reason: str):
    return CheckResult(check["field"], check_key(check), expected, actual, passed,
                       "ok" if passed else reason)  # fmt: skip


def _equals(check: dict[str, Any], output: dict[str, Any] | None) -> CheckResult:
    found, actual = lookup(output, check["field"])
    passed = same(actual, check["value"])
    return _result(check, check["value"], actual, passed, "wrong value" if found else "missing")


def _one_of(check: dict[str, Any], output: dict[str, Any] | None) -> CheckResult:
    found, actual = lookup(output, check["field"])
    values = cast("list[Any]", check["values"])
    passed = any(same(actual, v) for v in values)
    return _result(check, values, actual, passed, "wrong value" if found else "missing")


def _includes(actual: Any, value: Any) -> bool | None:
    """Whether text or a list holds `value`; None when the question doesn't apply."""
    if isinstance(actual, str):
        return value in actual if isinstance(value, str) else None
    if isinstance(actual, list):
        return any(same(item, value) for item in cast("list[Any]", actual))
    return None


def _contains(check: dict[str, Any], output: dict[str, Any] | None) -> CheckResult:
    _, actual = lookup(output, check["field"])
    if actual is None:
        return _result(check, check["value"], actual, False, "missing")
    held = _includes(actual, check["value"])
    return _result(check, check["value"], actual, held is True, "wrong value")


def _not_contains(check: dict[str, Any], output: dict[str, Any] | None) -> CheckResult:
    _, actual = lookup(output, check["field"])
    if actual is None:  # nothing holds nothing
        return _result(check, check["value"], actual, True, "ok")
    held = _includes(actual, check["value"])
    return _result(check, check["value"], actual, held is False, "wrong value")


CHECKS: dict[str, Callable[[dict[str, Any], dict[str, Any] | None], CheckResult]] = {
    "equals": _equals,
    "one_of": _one_of,
    "contains": _contains,
    "not_contains": _not_contains,
}
NEEDS = {"equals": "value", "one_of": "values", "contains": "value", "not_contains": "value"}


def validate(checks: Any) -> list[str]:
    """What's wrong with a case's checks, found when the suite loads. `value: null` is
    valid: it's how "nothing" is checked."""
    if not isinstance(checks, list):
        return ["checks must be a list"]
    problems: list[str] = []
    keys: dict[str, int] = {}
    for n, check in enumerate(cast("list[Any]", checks), start=1):
        if not isinstance(check, dict):
            problems.append(f"check {n} must be a mapping")
            continue
        check = cast("dict[str, Any]", check)
        kind = check.get("type")
        if not isinstance(kind, str) or kind not in CHECKS:
            problems.append(f"check {n}: unknown type {kind!r}")
            continue
        wrong = [f"needs {k!r}" for k in ("field", NEEDS[kind]) if k not in check]
        if "field" in check and not isinstance(check["field"], str):
            wrong.append("'field' must be text")
        if kind == "one_of" and "values" in check and not isinstance(check["values"], list):
            wrong.append("'values' must be a list")
        if not wrong:
            try:
                key = check_key(check)
            except ValueError as error:
                wrong.append(str(error))
            else:
                if key in keys:
                    wrong.append(f"has the same key as check {keys[key]} ({key})")
                keys.setdefault(key, n)
        problems.extend(f"check {n}: {w}" for w in wrong)
    return problems


def run_checks(checks: list[dict[str, Any]], output: dict[str, Any] | None) -> list[CheckResult]:
    return [CHECKS[check["type"]](check, output) for check in checks]
