"""Agreement between a judge and hand grades (#17, docs/designs/llm-judge.md)."""

from __future__ import annotations

import pytest

from flight_recorder.evals.agreement import agreement, incomplete, pairs_from
from flight_recorder.evals.grade import grade_suite
from flight_recorder.evals.grade import input_hash as hash_of

P, F = "pass", "fail"


def test_a_known_table_gives_its_known_numbers() -> None:
    # 20 items: 10 both pass, 5 both fail, 3 hand pass/judge fail, 2 hand fail/judge pass
    pairs = [(P, P)] * 10 + [(F, F)] * 5 + [(P, F)] * 3 + [(F, P)] * 2

    a = agreement(pairs)

    assert (a.n, a.both_pass, a.both_fail, a.hand_pass_judge_fail, a.hand_fail_judge_pass) == (
        20, 10, 5, 3, 2)  # fmt: skip
    assert a.percent == pytest.approx(0.75)
    # p_e = (13/20)(12/20) + (7/20)(8/20) = 0.39 + 0.14 = 0.53; kappa = 0.22 / 0.47
    assert a.kappa == pytest.approx(0.22 / 0.47)
    assert a.catch_rate == pytest.approx(5 / 7)
    assert a.pass_rate == pytest.approx(10 / 13)


def test_perfect_and_chance_agreement() -> None:
    assert agreement([(P, P), (F, F)]).kappa == pytest.approx(1.0)
    assert agreement([(P, P), (P, F), (F, P), (F, F)]).kappa == pytest.approx(0.0)


def test_kappa_is_undefined_only_when_both_raters_use_one_class() -> None:
    both_pass = agreement([(P, P)] * 4)
    assert both_pass.kappa is None and both_pass.percent == 1.0 and both_pass.catch_rate is None

    judge_mixed = agreement([(P, P), (P, P), (P, F), (P, F)])
    assert judge_mixed.kappa == pytest.approx(0.0)


def test_nothing_counted_gives_no_numbers() -> None:
    a = agreement([])

    assert a.n == 0 and a.percent is None and a.kappa is None
    assert a.catch_rate is None and a.pass_rate is None


def _case(id: str, grade: str, tags: list[str], **extra: object) -> dict[str, object]:
    checks = extra.pop("checks", [{"type": "equals", "field": "grade", "value": grade}])
    return {"id": id, "tags": tags, "input": {"n": id}, "checks": checks, **extra}


def _line(case: dict[str, object], output: object = None, error: str | None = None) -> dict:
    return {"case": case["id"], "input_hash": hash_of(case), "output": output, "error": error}


def test_pairs_are_split_by_tag_and_left_out_cases_say_why() -> None:
    cases = [
        _case("a", P, ["corpus"]),
        _case("b", F, ["broken", "corpus"]),
        _case("c", P, []),
        _case("stale", P, ["corpus"]),
        _case("err", P, ["corpus"]),
        _case("skip", P, ["corpus"], checks=[]),
        _case("two", P, ["corpus"], checks=[{"type": "equals", "field": "grade", "value": P},
                                            {"type": "equals", "field": "x", "value": 1}]),
        _case("odd", "maybe", ["corpus"]),
    ]  # fmt: skip
    by_id = {c["id"]: c for c in cases}
    lines = {
        "a": _line(by_id["a"], {"grade": P}),
        "b": _line(by_id["b"], {"grade": P}),
        "c": _line(by_id["c"], {"grade": F}),
        "stale": {**_line(by_id["stale"], {"grade": P}), "input_hash": "sha256:old"},
        "err": _line(by_id["err"], error="boom"),
        "two": _line(by_id["two"], {"grade": P, "x": 1}),
        "odd": _line(by_id["odd"], {"grade": "maybe"}),
    }
    result = grade_suite(cases, lines, include_unreviewed=False)

    pairs, by_tag, left_out = pairs_from(result, cases, "grade")

    assert pairs == [(P, P), (F, P), (P, F)]
    assert by_tag == {"corpus": [(P, P), (F, P)], "broken": [(F, P)]}
    assert dict(left_out) == {
        "stale": "stale",
        "err": "error",
        "skip": "skipped",
        "two": "no single equals check on grade",
        "odd": "value not pass or fail",
    }
    assert incomplete(left_out)
    assert incomplete([("err", "error")]) and incomplete([("stale", "stale")])
    assert not incomplete([("skip", "skipped"), ("odd", "value not pass or fail")])
