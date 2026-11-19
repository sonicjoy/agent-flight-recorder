"""The committed judge suite is what `afr eval judge-suite` builds from the committed items and
rubric: an item or the rubric edited without regenerating the suite fails here."""

from __future__ import annotations

from pathlib import Path

from flight_recorder.evals.judge_suite import build_suite, load_items, load_rubric

ROOT = Path(__file__).resolve().parents[2]
JUDGE = ROOT / "evals" / "judges" / "triager-comment"
SUITE = ROOT / "evals" / "cases" / "comment-judge"


def test_the_committed_suite_is_built_from_the_committed_items_and_rubric() -> None:
    built = build_suite(load_items(JUDGE / "items.yaml"), load_rubric(JUDGE / "rubric.yaml"))

    committed = {path.name: path.read_bytes() for path in SUITE.glob("*.yaml")}

    assert sorted(committed) == sorted(built)
    different = [name for name in built if committed[name] != built[name].encode("utf-8")]
    assert different == []


def test_the_judge_suite_has_no_baseline() -> None:
    assert not (SUITE / "baseline.json").exists()
