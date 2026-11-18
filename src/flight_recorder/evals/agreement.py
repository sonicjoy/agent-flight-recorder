"""How well a judge agrees with hand grades (#17): `afr eval agreement`.

Read from a graded judge suite: each counted case pairs its check's expected value (the hand
grade) with the judge's answer. Pure counting, no model and no store. `pass` is the positive
class, so the catch rate is how many hand-graded failures the judge also failed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from flight_recorder.evals.grade import SuiteResult

Pair = tuple[str, str]  # (hand grade, judge grade)
CLASSES = ("pass", "fail")
INCOMPLETE = ("stale", "error")


@dataclass(frozen=True)
class Agreement:
    n: int
    both_pass: int
    both_fail: int
    hand_pass_judge_fail: int
    hand_fail_judge_pass: int
    percent: float | None  # None: nothing counted
    kappa: float | None  # None: nothing counted, or both raters used one and the same class
    catch_rate: float | None  # hand fail, judge fail; None: no hand-graded failures
    pass_rate: float | None  # hand pass, judge pass; None: no hand-graded passes


def agreement(pairs: list[Pair]) -> Agreement:
    n = len(pairs)
    both_pass = sum(p == ("pass", "pass") for p in pairs)
    both_fail = sum(p == ("fail", "fail") for p in pairs)
    hand_pass_judge_fail = sum(p == ("pass", "fail") for p in pairs)
    hand_fail_judge_pass = sum(p == ("fail", "pass") for p in pairs)
    hand_pass, hand_fail = both_pass + hand_pass_judge_fail, both_fail + hand_fail_judge_pass
    if n == 0:
        return Agreement(0, 0, 0, 0, 0, None, None, None, None)
    observed = (both_pass + both_fail) / n
    judge_pass = both_pass + hand_fail_judge_pass
    expected = (hand_pass * judge_pass + hand_fail * (n - judge_pass)) / (n * n)
    kappa = None if expected == 1 else (observed - expected) / (1 - expected)
    return Agreement(
        n, both_pass, both_fail, hand_pass_judge_fail, hand_fail_judge_pass,
        percent=observed,
        kappa=kappa,
        catch_rate=both_fail / hand_fail if hand_fail else None,
        pass_rate=both_pass / hand_pass if hand_pass else None,
    )  # fmt: skip


def pairs_from(
    result: SuiteResult, cases: list[dict[str, Any]], field: str
) -> tuple[list[Pair], dict[str, list[Pair]], list[tuple[str, str]]]:
    """The counted pairs, the same pairs by case tag, and each case left out with its reason:
    stale, error, skipped, no single equals check on <field>, or value not pass or fail."""
    by_id = {c["id"]: c for c in cases}
    pairs: list[Pair] = []
    by_tag: dict[str, list[Pair]] = {}
    left_out: list[tuple[str, str]] = []
    for case in result.cases:
        if case.outcome not in ("pass", "fail"):
            left_out.append((case.id, case.outcome))
            continue
        checks = by_id[case.id]["checks"]
        if len(checks) != 1 or checks[0].get("type") != "equals" or checks[0].get("field") != field:
            left_out.append((case.id, f"no single equals check on {field}"))
            continue
        check = case.checks[0]
        pair = (check.expected, check.actual)
        if not all(isinstance(v, str) and v in CLASSES for v in pair):
            left_out.append((case.id, "value not pass or fail"))
            continue
        pairs.append(pair)
        for tag in dict.fromkeys(by_id[case.id].get("tags") or []):
            by_tag.setdefault(str(tag), []).append(pair)
    return pairs, by_tag, left_out


def incomplete(left_out: list[tuple[str, str]]) -> bool:
    """The numbers are partial: some case was stale or errored."""
    return any(reason in INCOMPLETE for _, reason in left_out)
