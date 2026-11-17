"""Case outcomes, the baseline and the gate behind `afr eval` (#15)."""

from __future__ import annotations

from typing import Any

import pytest

from flight_recorder.evals.checks import check_key
from flight_recorder.evals.grade import (
    Changes,
    StaleError,
    SuiteResult,
    baseline_from,
    compare,
    exit_code,
    grade_suite,
    input_hash,
)

TYPE_BUG = {"type": "equals", "field": "type", "value": "bug"}
LABELS_BUG = {"type": "contains", "field": "labels", "value": "bug"}
T, L = check_key(TYPE_BUG), check_key(LABELS_BUG)


def case(id: str, **extra: Any) -> dict[str, Any]:
    return {"id": id, "input": {"title": id}, "checks": [TYPE_BUG, LABELS_BUG]} | extra


def line(c: dict[str, Any], output: Any = None, error: str | None = None) -> dict[str, Any]:
    return {"case": c["id"], "input_hash": input_hash(c), "output": output, "error": error}


GOOD = {"type": "bug", "labels": ["bug"]}
HALF = {"type": "bug", "labels": []}


def grade(cases: list[dict[str, Any]], lines: list[dict[str, Any]], **kw: Any) -> SuiteResult:
    return grade_suite(
        cases, {x["case"]: x for x in lines}, include_unreviewed=kw.get("inc", False)
    )


def outcomes(result: SuiteResult) -> dict[str, tuple[str, str | None]]:
    return {c.id: (c.outcome, c.note) for c in result.cases}


# --- input_hash -----------------------------------------------------------------------


def test_input_hash_covers_only_the_input() -> None:
    a = case("a")
    b = a | {"checks": [TYPE_BUG], "expected": {"type": "docs"}, "tags": ["x"]}
    assert input_hash(a) == input_hash(b)
    assert input_hash(a) != input_hash(a | {"input": {"title": "other"}})
    assert input_hash(a).startswith("sha256:") and len(input_hash(a)) == 7 + 64


# --- outcomes -------------------------------------------------------------------------


def test_each_outcome() -> None:
    cases = [
        case("pass"),
        case("fail"),
        case("error"),
        case("stale"),
        case("missing"),
        case("draft", unreviewed=True),
        case("empty", checks=[]),
    ]
    lines = [
        line(cases[0], GOOD),
        line(cases[1], HALF),
        line(cases[2], error="ValueError: boom"),
        line(cases[3], GOOD) | {"input_hash": "sha256:old"},
    ]
    result = grade(cases, lines)
    assert outcomes(result) == {
        "draft": ("skipped", "unreviewed"),
        "empty": ("skipped", "no checks"),
        "error": ("error", None),
        "fail": ("fail", None),
        "missing": ("stale", "no output"),
        "pass": ("pass", None),
        "stale": ("stale", "input changed"),
    }
    assert [c.id for c in result.cases] == sorted(c["id"] for c in cases)
    err = next(c for c in result.cases if c.id == "error")
    assert err.error == "ValueError: boom"
    assert [r.key for r in err.checks] == [T, L]


def test_unreviewed_cases_are_graded_with_include_unreviewed_and_marked() -> None:
    draft = case("draft", unreviewed=True)
    result = grade([draft], [line(draft, GOOD)], inc=True)
    assert outcomes(result) == {"draft": ("pass", None)}
    assert result.cases[0].unreviewed


def test_an_unreviewed_case_with_no_line_is_skipped_not_stale() -> None:
    draft = case("draft", unreviewed=True)
    result = grade([draft], [], inc=True)
    assert outcomes(result) == {"draft": ("skipped", "no output: run it with --include-unreviewed")}


def test_lines_for_cases_that_are_gone_are_orphaned() -> None:
    a = case("a")
    result = grade([a], [line(a, GOOD), line(case("gone"), GOOD)])
    assert result.orphaned == ["gone"]


def test_per_field_counts_count_an_error_as_failed_and_skip_the_rest() -> None:
    cases = [case("a"), case("b"), case("c"), case("d", checks=[]), case("e")]
    lines = [line(cases[0], GOOD), line(cases[1], HALF), line(cases[2], error="X: y")]
    result = grade(cases, lines)  # e is stale, d skipped
    assert result.fields == {"type": (2, 3), "labels": (1, 3)}


# --- compare and the gate -------------------------------------------------------------


def _changes(cases: list[dict[str, Any]], lines: list[dict[str, Any]], base: Any) -> Changes:
    return compare(grade(cases, lines), base)


def test_no_baseline_compares_nothing_but_stale() -> None:
    a, b = case("a"), case("b")
    changes = _changes([a, b], [line(a, HALF)], None)
    assert changes == Changes(stale=["b"])
    assert exit_code(changes, "grade") == 1
    assert exit_code(_changes([a], [line(a, HALF)], None), "grade") == 0


@pytest.mark.parametrize(
    ("before", "output", "field", "run", "grade_"),
    [
        ("pass", HALF, "regressions", 1, 1),
        ("error", GOOD, "improvements", 0, 1),
        ("fail", GOOD, "improvements", 0, 1),
    ],
)
def test_check_states(before: str, output: Any, field: str, run: int, grade_: int) -> None:
    a = case("a")
    changes = _changes([a], [line(a, output)], {"a": {T: "pass", L: before}})
    assert getattr(changes, field) == [f"a {L}"]
    assert (exit_code(changes, "run"), exit_code(changes, "grade")) == (run, grade_)


def test_an_error_regresses_every_check_that_answered() -> None:
    a = case("a")
    changes = _changes([a], [line(a, error="X: y")], {"a": {T: "pass", L: "fail"}})
    assert changes.regressions == sorted([f"a {T}", f"a {L}"])  # fail -> error is worse too


def test_a_regression_on_another_field_in_a_failing_case_is_caught() -> None:
    a = case("a")
    out = {"type": "docs", "labels": []}  # fails both; it failed only labels before
    changes = _changes([a], [line(a, out)], {"a": {T: "pass", L: "fail"}})
    assert changes.regressions == [f"a {T}"]


def test_an_edited_check_is_changed_checks_and_fails_both_commands() -> None:
    a = case("a", checks=[TYPE_BUG, LABELS_BUG | {"value": "docs"}])
    changes = _changes([a], [line(a, GOOD)], {"a": {T: "pass", L: "pass"}})
    assert changes.changed == ["a"]
    assert (exit_code(changes, "run"), exit_code(changes, "grade")) == (1, 1)


def test_new_and_removed_cases_fail_only_grade() -> None:
    a, b = case("a"), case("b")
    changes = _changes([a, b], [line(a, GOOD), line(b, GOOD)], {"a": {T: "pass", L: "pass"},
                                                                 "c": {T: "pass"}})  # fmt: skip
    assert (changes.new, changes.removed) == (["b"], ["c"])
    assert (exit_code(changes, "run"), exit_code(changes, "grade")) == (0, 1)


def test_a_baselined_case_now_skipped_fails_neither() -> None:
    a = case("a", unreviewed=True)
    changes = _changes([a], [], {"a": {T: "pass", L: "pass"}})
    assert changes.skipped_in_baseline == ["a"]
    assert (exit_code(changes, "run"), exit_code(changes, "grade")) == (0, 0)


def test_a_stale_case_fails_both_and_its_keys_arent_compared() -> None:
    a = case("a")
    changes = _changes([a], [line(a, GOOD) | {"input_hash": "sha256:old"}],
                       {"a": {T: "pass", L: "pass"}})  # fmt: skip
    assert changes == Changes(stale=["a"])
    assert (exit_code(changes, "run"), exit_code(changes, "grade")) == (1, 1)


def test_unreviewed_cases_graded_on_request_arent_compared() -> None:
    a = case("a", unreviewed=True)
    result = grade([a], [line(a, HALF)], inc=True)
    assert compare(result, {}) == Changes()


def test_no_difference_exits_0() -> None:
    a = case("a")
    changes = _changes([a], [line(a, HALF)], {"a": {T: "pass", L: "fail"}})
    assert changes == Changes()
    assert (exit_code(changes, "run"), exit_code(changes, "grade")) == (0, 0)


# --- baseline_from --------------------------------------------------------------------


def test_baseline_from_writes_reviewed_graded_cases_only() -> None:
    cases = [case("a"), case("b"), case("c", unreviewed=True), case("d", checks=[])]
    lines = [line(cases[0], HALF), line(cases[1], error="X: y"), line(cases[2], GOOD)]
    assert baseline_from(grade(cases, lines, inc=True)) == {
        "a": {T: "pass", L: "fail"},
        "b": {T: "error", L: "error"},
    }


def test_baseline_from_refuses_while_a_case_is_stale() -> None:
    a, b = case("a"), case("b")
    with pytest.raises(StaleError) as raised:
        baseline_from(grade([a, b], [line(a, GOOD)]))
    assert raised.value.ids == ["b"]


def test_fail_to_error_is_a_regression_and_error_to_fail_an_improvement() -> None:
    """grade fails on any difference: a check moving between fail and error is one."""
    a = case("a")
    worse = _changes([a], [line(a, error="X: y")], {"a": {T: "pass", L: "fail"}})
    assert worse.regressions == sorted([f"a {T}", f"a {L}"])
    better = _changes([a], [line(a, HALF)], {"a": {T: "error", L: "error"}})
    assert better.improvements == sorted([f"a {T}", f"a {L}"])
    assert exit_code(better, "grade") == 1
