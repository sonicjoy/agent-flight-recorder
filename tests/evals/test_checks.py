"""The generic checks, equality and value conversion behind `afr eval` (#15)."""

from __future__ import annotations

import datetime as dt
import hashlib
import math
from typing import Any

import pytest
from pydantic import BaseModel

from flight_recorder.evals.checks import (
    CHECKS,
    canonical,
    check_key,
    convert,
    lookup,
    run_checks,
    same,
    validate,
)

# --- lookup ---------------------------------------------------------------------------


def test_lookup_tries_the_exact_key_before_a_dotted_path() -> None:
    output = {"triager.labels": ["bug"], "triager": {"labels": ["other"]}}
    assert lookup(output, "triager.labels") == (True, ["bug"])


def test_lookup_walks_a_dotted_path_through_nested_dicts() -> None:
    assert lookup({"meta": {"rules": 2}}, "meta.rules") == (True, 2)


def test_lookup_through_a_value_that_isnt_a_dict_is_missing() -> None:
    assert lookup({"meta": [1, 2]}, "meta.rules") == (False, None)
    assert lookup({"meta": "text"}, "meta.rules") == (False, None)


def test_lookup_of_an_absent_field_or_no_output_is_missing() -> None:
    assert lookup({"type": "bug"}, "labels") == (False, None)
    assert lookup(None, "labels") == (False, None)


def test_lookup_finds_a_field_holding_null() -> None:
    assert lookup({"type": None}, "type") == (True, None)


# --- same -----------------------------------------------------------------------------


@pytest.mark.parametrize(("a", "b"), [(None, []), ([], None), (None, None), ([], [])])
def test_null_and_the_empty_list_are_one_nothing(a: Any, b: Any) -> None:
    assert same(a, b)


@pytest.mark.parametrize("value", ["", 0, False, {}, ["x"]])
def test_nothing_doesnt_equal_a_falsy_value(value: Any) -> None:
    assert not same(None, value)
    assert not same([], value) or value == []


def test_lists_of_plain_values_compare_as_multisets() -> None:
    assert same(["performance", "bug"], ["bug", "performance"])
    assert not same(["bug", "bug"], ["bug"])
    assert not same(["bug", "bug", "docs"], ["bug", "docs", "docs"])


def test_a_boolean_never_equals_a_number_even_in_a_list() -> None:
    assert not same(True, 1)
    assert not same(False, 0)
    assert not same([True, False], [1, 0])
    assert same([1, True], [True, 1.0])


def test_lists_holding_dicts_or_lists_compare_in_order() -> None:
    assert same([{"a": 1}, {"b": 2}], [{"a": 1}, {"b": 2}])
    assert not same([{"a": 1}, {"b": 2}], [{"b": 2}, {"a": 1}])
    assert not same([[1], [2]], [[2], [1]])


def test_numbers_compare_by_value() -> None:
    assert same(1, 1.0)
    assert not same(1, 2)


def test_strings_compare_exactly() -> None:
    assert same("bug", "bug")
    assert not same("bug", "Bug")
    assert not same("1", 1)


def test_nested_dicts_use_the_same_rules_and_a_missing_key_is_nothing() -> None:
    assert same({"a": {"labels": ["x", "y"]}}, {"a": {"labels": ["y", "x"]}})
    assert same({"a": 1, "b": None}, {"a": 1})
    assert same({"a": 1, "b": []}, {"a": 1.0})
    assert not same({"a": 1, "b": 2}, {"a": 1})


# --- the checks -----------------------------------------------------------------------


def _one(check: dict[str, Any], output: dict[str, Any] | None) -> tuple[bool, str]:
    result = CHECKS[check["type"]](check, output)
    return result.passed, result.reason


def test_equals() -> None:
    check = {"type": "equals", "field": "type", "value": "bug"}
    assert _one(check, {"type": "bug"}) == (True, "ok")
    assert _one(check, {"type": "docs"}) == (False, "wrong value")
    assert _one(check, {}) == (False, "missing")


def test_equals_null_passes_on_a_missing_field_null_or_an_empty_list() -> None:
    check = {"type": "equals", "field": "labels", "value": None}
    assert _one(check, {})[0]
    assert _one(check, {"labels": None})[0]
    assert _one(check, {"labels": []})[0]
    assert not _one(check, {"labels": ["bug"]})[0]


def test_one_of() -> None:
    check = {"type": "one_of", "field": "type", "values": ["bug", "performance"]}
    assert _one(check, {"type": "performance"}) == (True, "ok")
    assert _one(check, {"type": None}) == (False, "wrong value")
    assert _one(check, {}) == (False, "missing")


def test_contains_on_text_and_on_a_list() -> None:
    check = {"type": "contains", "field": "labels", "value": "documentation"}
    assert _one(check, {"labels": ["bug", "documentation"]}) == (True, "ok")
    assert _one(check, {"labels": ["bug"]}) == (False, "wrong value")
    assert _one(check, {"labels": "needs documentation"}) == (True, "ok")
    assert _one(check, {}) == (False, "missing")


def test_contains_on_a_number_field_or_with_a_non_string_value_on_text_is_wrong() -> None:
    assert _one({"type": "contains", "field": "n", "value": "1"}, {"n": 12}) == (
        False,
        "wrong value",
    )
    assert _one({"type": "contains", "field": "t", "value": 1}, {"t": "a1"}) == (
        False,
        "wrong value",
    )


def test_not_contains() -> None:
    check = {"type": "not_contains", "field": "labels", "value": "documentation"}
    assert _one(check, {"labels": ["bug"]}) == (True, "ok")
    assert _one(check, {"labels": ["documentation"]}) == (False, "wrong value")
    assert _one(check, {"labels": "fix the documentation"}) == (False, "wrong value")


def test_not_contains_passes_on_a_missing_field() -> None:
    check = {"type": "not_contains", "field": "labels", "value": "documentation"}
    assert _one(check, {}) == (True, "ok")
    assert _one(check, None) == (True, "ok")
    assert _one(check, {"labels": None}) == (True, "ok")


def test_run_checks_gives_one_result_per_check_with_its_key() -> None:
    checks = [
        {"type": "equals", "field": "type", "value": "bug"},
        {"type": "contains", "field": "labels", "value": "bug"},
    ]
    results = run_checks(checks, {"type": "bug", "labels": []})
    assert [r.passed for r in results] == [True, False]
    assert [r.key for r in results] == [check_key(c) for c in checks]
    assert results[1].field == "labels"
    assert results[1].expected == "bug"
    assert results[1].actual == []


# --- validate -------------------------------------------------------------------------


def test_validate_accepts_good_checks_and_value_null() -> None:
    assert validate([
        {"type": "equals", "field": "labels", "value": None},
        {"type": "one_of", "field": "type", "values": ["a", "b"]},
        {"type": "contains", "field": "labels", "value": "x"},
        {"type": "not_contains", "field": "labels", "value": "y"},
    ]) == []  # fmt: skip


@pytest.mark.parametrize(
    ("checks", "problem"),
    [
        ("not a list", "checks must be a list"),
        ([["equals"]], "check 1 must be a mapping"),
        ([{"type": "judge", "field": "x", "value": 1}], "unknown type 'judge'"),
        ([{"type": "equals", "field": "x"}], "needs 'value'"),
        ([{"type": "equals", "value": 1}], "needs 'field'"),
        ([{"type": "equals", "field": 3, "value": 1}], "'field' must be text"),
        ([{"type": "one_of", "field": "x", "values": "a"}], "'values' must be a list"),
        (
            [
                {"type": "equals", "field": "x", "value": 1},
                {"field": "x", "value": 1, "type": "equals"},
            ],
            "the same key",
        ),
        ([{"type": "equals", "field": "x", "value": math.nan}], "output not JSON"),
    ],
)
def test_validate_reports_each_problem(checks: Any, problem: str) -> None:
    found = validate(checks)
    assert any(problem in p for p in found), found


# --- check_key and canonical ----------------------------------------------------------


def test_check_key_is_the_field_and_8_hex_of_the_whole_check() -> None:
    check = {"type": "equals", "field": "labels", "value": ["bug"]}
    digest = hashlib.sha256(canonical(check).encode()).hexdigest()
    assert check_key(check) == f"labels/{digest[:8]}"


def test_check_key_is_stable_under_key_order_and_changes_with_the_check() -> None:
    a = {"type": "equals", "field": "type", "value": "bug"}
    b = {"value": "bug", "field": "type", "type": "equals"}
    assert check_key(a) == check_key(b)
    assert check_key(a) != check_key(a | {"value": "docs"})


def test_canonical_is_the_fixed_formula() -> None:
    assert canonical({"b": [1, "é"], "a": None}) == '{"a":null,"b":[1,"é"]}'


# --- convert --------------------------------------------------------------------------


class _Model(BaseModel):
    labels: list[str]
    when: dt.date


def test_convert_dumps_a_pydantic_model_as_json() -> None:
    model = _Model(labels=["bug"], when=dt.date(2026, 11, 17))
    assert convert(model) == {"labels": ["bug"], "when": "2026-11-17"}
    assert convert({"m": model}) == {"m": {"labels": ["bug"], "when": "2026-11-17"}}


def test_convert_writes_dates_in_iso_form() -> None:
    assert convert(dt.date(2026, 11, 17)) == "2026-11-17"
    at = dt.datetime(2026, 11, 17, 12, 0, tzinfo=dt.UTC)
    assert convert({"at": at}) == {"at": "2026-11-17T12:00:00+00:00"}


def test_convert_turns_tuples_into_lists_and_sets_into_sorted_lists() -> None:
    assert convert((1, "a")) == [1, "a"]
    assert convert({"b", "a", "c"}) == ["a", "b", "c"]
    assert convert(frozenset({2, 1})) == [1, 2]


@pytest.mark.parametrize(
    ("value", "kind"),
    [({1: "a"}, "int"), (math.nan, "float"), (math.inf, "float"), (object(), "object")],
)
def test_convert_refuses_what_json_cant_hold(value: Any, kind: str) -> None:
    with pytest.raises(ValueError, match=f"output not JSON: {kind}"):
        convert(value)


def test_convert_leaves_json_values_alone() -> None:
    value = {"a": [1, 2.5, True, None, "x"], "b": {"c": []}}
    assert convert(value) == value
