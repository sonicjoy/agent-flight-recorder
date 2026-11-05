import subprocess
from typing import Any

import pytest
from issue_triager import export
from issue_triager.fixtures import (
    AREAS,
    ISSUES_FILE,
    WEEKS,
    GroundTruth,
    IssueExport,
    TriageCase,
    dump_export,
    load_cases,
    load_real,
    load_synthetic,
)

REAL = {c.id: c for c in load_real()}
SYNTHETIC = load_synthetic()


def gh_issue(number: int, labels: list[str], milestone: str | None, **extra: Any) -> dict[str, Any]:
    return {
        "number": number,
        "title": f"Issue {number}",
        "body": "Body\r\nline two\n",
        "labels": [{"name": name} for name in labels],
        "milestone": {"title": milestone} if milestone else None,
        "state": "OPEN",
        "stateReason": "",
        "comments": [],
        **extra,
    }


# ---- Committed fixtures ----------------------------------------------------------


def test_real_export_has_known_duplicate() -> None:
    assert REAL["3"].truth.duplicate_of == "2"
    assert REAL["3"].state == "closed"
    assert REAL["2"].truth.duplicate_of is None


def test_case_ids_are_unique() -> None:
    ids = [c.id for c in load_cases()]

    assert len(ids) == len(set(ids))


@pytest.mark.parametrize("case", load_cases(), ids=lambda c: c.id)
def test_ground_truth_is_valid(case: TriageCase) -> None:
    assert case.truth.areas, "every case needs at least one area"
    assert case.truth.areas <= AREAS
    assert case.truth.week is None or case.truth.week in WEEKS
    assert case.truth.duplicate_of in {None, *REAL}
    assert case.truth.duplicate_of != case.id


@pytest.mark.parametrize("case", [c for c in load_cases() if c.truth.regression_of], ids=str)
def test_regression_points_at_delivered_work(case: TriageCase) -> None:
    original = REAL[str(case.truth.regression_of)]

    assert original.state == "closed", "only finished work can regress"
    assert original.truth.duplicate_of is None, "a closed duplicate delivered nothing"
    assert case.truth.week == original.truth.week


def test_synthetic_set_separates_duplicates_from_regressions() -> None:
    regressions = [c for c in SYNTHETIC if c.truth.regression_of]
    dupes_of_done_work = [
        c
        for c in SYNTHETIC
        if c.truth.duplicate_of and REAL[c.truth.duplicate_of].state == "closed"
    ]

    assert len(regressions) >= 3
    assert len(dupes_of_done_work) >= 3


def test_duplicate_and_regression_are_exclusive() -> None:
    with pytest.raises(ValueError, match="not both"):
        GroundTruth(
            areas=frozenset({"ui"}), stretch=False, week=1, duplicate_of="2", regression_of="2"
        )


def test_about_sixty_synthetic_cases_of_both_kinds() -> None:
    kinds = {c.kind for c in SYNTHETIC}

    assert 55 <= len(SYNTHETIC) <= 70
    assert kinds == {"paraphrase", "tricky"}
    assert all(c.source == "synthetic" and c.id.startswith("s") for c in SYNTHETIC)


@pytest.mark.parametrize("case", [c for c in SYNTHETIC if c.kind == "paraphrase"], ids=str)
def test_paraphrase_matches_its_source_issue(case: TriageCase) -> None:
    assert case.based_on in REAL
    source = REAL[case.based_on]

    assert case.truth.duplicate_of == case.based_on
    assert case.truth.model_dump(exclude={"duplicate_of"}) == source.truth.model_dump(
        exclude={"duplicate_of"}
    )
    assert case.title != source.title


def test_tricky_cases_explain_themselves() -> None:
    tricky = [c for c in SYNTHETIC if c.kind == "tricky"]

    assert all(c.note for c in tricky)
    assert all(c.based_on is None for c in tricky)


def test_agent_input_hides_the_answer() -> None:
    case = REAL["6"]  # labelled stretch, week 1

    seen = case.agent_input().model_dump()

    assert set(seen) == {"id", "title", "body"}


def test_committed_file_is_in_canonical_form() -> None:
    """Re-exporting unchanged issues must produce no diff."""
    text = ISSUES_FILE.read_text(encoding="utf-8")

    assert dump_export(IssueExport.model_validate_json(text)) == text


# ---- Exporter --------------------------------------------------------------------


def test_export_maps_labels_and_milestone_to_ground_truth() -> None:
    issues = [gh_issue(6, ["demo-agent", "stretch"], "Week 1: Demo agent and foundations")]

    (case,) = export.build_export(issues).issues

    assert case.truth.areas == {"demo-agent"}
    assert case.truth.stretch is True
    assert case.truth.week == 1
    assert case.body == "Body\nline two"


def test_export_reads_duplicate_from_closing_comment() -> None:
    dupe = gh_issue(
        3, ["demo-agent"], None, state="CLOSED", comments=[{"body": "Duplicate of #2 (oops)."}]
    )

    (case,) = export.build_export([dupe]).issues

    assert case.truth.duplicate_of == "2"
    assert case.truth.week is None
    assert case.state == "closed"


def test_export_reads_regression_from_body() -> None:
    bug = gh_issue(40, ["infra"], None, body="CI broke again.\n\nRegression of #1\n")

    (case,) = export.build_export([bug]).issues

    assert case.truth.regression_of == "1"
    assert case.truth.duplicate_of is None


def test_export_sorts_by_issue_number() -> None:
    issues = [gh_issue(n, ["ui"], None) for n in (10, 2, 33)]

    assert [c.id for c in export.build_export(issues).issues] == ["2", "10", "33"]


def test_export_rejects_unknown_labels() -> None:
    with pytest.raises(ValueError, match="wontfix"):
        export.build_export([gh_issue(1, ["infra", "wontfix"], None)])


def test_export_only_reads_from_github(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def fake_run(args: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, stdout="[]")

    monkeypatch.setattr(subprocess, "run", fake_run)

    export.fetch_issues()

    assert [args[:3] for args in calls] == [["gh", "issue", "list"]]
