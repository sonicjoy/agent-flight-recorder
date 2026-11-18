"""Judge suites from an items file and a rubric (#17, docs/designs/llm-judge.md)."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest
import yaml
from judge_fixtures import ITEM, items, rubric

from flight_recorder.evals.cases import CaseError
from flight_recorder.evals.judge_suite import (
    AGENT,
    VERSION,
    ItemsError,
    build_suite,
    load_items,
    load_rubric,
    write_suite,
)
from flight_recorder.evals.run import load_suite


def test_each_item_becomes_a_case_with_its_hand_grade_as_the_only_check(tmp_path: Path) -> None:
    files = build_suite(items(), rubric())
    write_suite(tmp_path / "j", files)

    suite = load_suite(tmp_path, "j")
    assert suite.agent == "flight_recorder.evals.judge:judge"
    assert suite.model == "anthropic:claude-haiku-5-5"
    broken = next(c for c in suite.cases if c["id"] == "b-1")
    assert list(yaml.safe_load(files["b-1.yaml"])) == ["id", "tags", "note", "input", "checks"]
    assert broken["tags"] == ["broken"]
    assert broken["note"] == "targets: suggests-not-settles; author: owner"
    assert broken["checks"] == [{"type": "equals", "field": "grade", "value": "fail"}]
    assert broken["input"]["judge_version"] == VERSION
    assert broken["input"]["model"] == "anthropic:claude-haiku-5-5"
    assert set(broken["input"]["rubric"]) == {"instructions", "criteria", "examples"}
    plain = yaml.safe_load(files["r-0001.yaml"])
    assert "note" not in plain


def test_every_bad_item_is_reported_at_once() -> None:
    bad = [
        {"id": "Bad Id", "tag": "corpus", "item": items()[0]["item"], "grade": "pass"},
        {"id": "r-0001", "tag": "other", "item": items()[0]["item"], "grade": "Pass"},
        {"id": "x", "tag": "broken", "item": {"comment": "c"}, "grade": "fail",
         "targets": "nope", "colour": "red"},
    ]  # fmt: skip

    with pytest.raises(ItemsError) as caught:
        build_suite(items(*bad), rubric())

    problems = caught.value.problems
    assert {item for item, _ in problems} == {"Bad Id", "r-0001", "x"}
    text = " ".join(p for _, p in problems)
    for expected in ("must be 1 to 64", "repeats", "tag must be", "grade must be",
                     "item must be", "targets must be", "unknown keys"):  # fmt: skip
        assert expected in text


def test_a_rubric_the_judge_would_refuse_is_an_error_not_bad_items() -> None:
    bad = rubric() | {"criteria": [{"id": "reason", "pass": "p", "fail": "f"}]}

    with pytest.raises(CaseError, match="rubric"):
        build_suite(items(), bad)


def test_writing_replaces_files_deletes_stray_cases_and_keeps_outputs(tmp_path: Path) -> None:
    folder = tmp_path / "j"
    folder.mkdir()
    (folder / "suite.yaml").write_text(f"agent: {AGENT}\n", encoding="utf-8")
    (folder / "old.yaml").write_text("id: old\n", encoding="utf-8")
    (folder / "outputs.jsonl").write_text("{}\n", encoding="utf-8")
    (folder / "b-1.yaml").write_text("stale\n", encoding="utf-8")

    write_suite(folder, build_suite(items(), rubric()))

    names = sorted(p.name for p in folder.iterdir())
    assert names == ["b-1.yaml", "outputs.jsonl", "r-0001.yaml", "suite.yaml"]
    assert (folder / "outputs.jsonl").read_text(encoding="utf-8") == "{}\n"
    assert b"\r\n" not in (folder / "b-1.yaml").read_bytes()


def _write(path: Path, data: Any) -> Path:
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def test_files_must_have_the_documented_shape(tmp_path: Path) -> None:
    assert load_items(_write(tmp_path / "i.yaml", {"items": items()})) == items()
    assert load_rubric(_write(tmp_path / "r.yaml", rubric() | {"examples": []}))["examples"] == []
    no_examples = {k: v for k, v in rubric().items() if k != "examples"}
    assert load_rubric(_write(tmp_path / "r2.yaml", no_examples))["examples"] == []
    for bad_items in ([1, 2], {"items": "x"}, {"items": [], "more": 1}):
        with pytest.raises(CaseError):
            load_items(_write(tmp_path / "bad.yaml", bad_items))
    for bad_rubric in ({"model": "m"}, rubric() | {"colour": 1}):
        with pytest.raises(CaseError):
            load_rubric(_write(tmp_path / "bad.yaml", bad_rubric))
    with pytest.raises(CaseError):
        load_items(tmp_path / "missing.yaml")


def test_building_a_suite_never_loads_the_llm_layer() -> None:
    import subprocess

    code = (
        "import sys, flight_recorder.evals.judge_suite, flight_recorder.cli.eval; "
        "print(any(m.startswith(('langchain', 'flight_recorder.llm')) for m in sys.modules))"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)

    assert out.stdout.strip() == "False"


@pytest.mark.parametrize(
    ("changes", "problem"),
    [
        ({"item": ITEM | {"decision": "dup"}}, "item must be"),
        ({"author": 3}, "author must be text"),
        ({"note": ["x"]}, "note must be text"),
        ({"item": ITEM | {"decision": {"x": float("nan")}}}, "can't be stored as JSON"),
        ({1: "x", "colour": "red"}, "unknown keys"),
    ],
)
def test_each_item_rule_is_enforced(changes: dict[Any, Any], problem: str) -> None:
    with pytest.raises(ItemsError) as caught:
        build_suite([items()[0] | changes], rubric())

    assert any(problem in p for _, p in caught.value.problems)


def test_no_items_is_refused() -> None:
    with pytest.raises(CaseError, match="no items"):
        build_suite([], rubric())


def test_a_model_spec_the_judge_would_refuse_is_caught_when_writing() -> None:
    from flight_recorder.evals.judge_suite import PROVIDERS
    from flight_recorder.llm import PROVIDERS as LLM_PROVIDERS

    assert set(PROVIDERS) == set(LLM_PROVIDERS)
    for bad in ("claude-haiku", "nope:model", "anthropic:"):
        with pytest.raises(CaseError, match="rubric"):
            build_suite(items(), rubric() | {"model": bad})


def test_a_file_error_names_the_file(tmp_path: Path) -> None:
    (tmp_path / "rubric.yaml").write_text("model: [unclosed\n", encoding="utf-8")

    with pytest.raises(CaseError, match=r"rubric\.yaml"):
        load_rubric(tmp_path / "rubric.yaml")


def test_another_kind_of_suite_is_never_overwritten(tmp_path: Path) -> None:
    from flight_recorder.evals.judge_suite import check_target

    other = tmp_path / "other"
    other.mkdir()
    (other / "suite.yaml").write_text("agent: flight_recorder.evals.example:triage\n",
                                      encoding="utf-8")  # fmt: skip
    loose = tmp_path / "loose"
    loose.mkdir()
    (loose / "case-1.yaml").write_text("id: case-1\n", encoding="utf-8")
    judged = tmp_path / "judged"
    write_suite(judged, build_suite(items(), rubric()))

    for folder in (other, loose):
        with pytest.raises(CaseError):
            check_target(folder)
    check_target(judged)
    check_target(tmp_path / "new")


def test_a_link_in_a_case_files_place_is_replaced_not_written_through(tmp_path: Path) -> None:
    folder, target = tmp_path / "j", tmp_path / "outside.txt"
    folder.mkdir()
    (folder / "suite.yaml").write_text(f"agent: {AGENT}\n", encoding="utf-8")
    target.write_text("keep\n", encoding="utf-8")
    try:
        (folder / "b-1.yaml").symlink_to(target)
    except OSError:
        pytest.skip("this system can't create symbolic links")

    write_suite(folder, build_suite(items(), rubric()))

    assert target.read_text(encoding="utf-8") == "keep\n"
    assert not (folder / "b-1.yaml").is_symlink()


def test_writing_never_deletes_another_suites_cases(tmp_path: Path) -> None:
    folder = tmp_path / "other"
    folder.mkdir()
    (folder / "suite.yaml").write_text(
        "agent: flight_recorder.evals.example:triage\n", encoding="utf-8"
    )
    (folder / "keep-me.yaml").write_text("id: keep-me\n", encoding="utf-8")

    with pytest.raises(CaseError):
        write_suite(folder, build_suite(items(), rubric()))

    assert (folder / "keep-me.yaml").exists()


def test_a_rubric_example_that_isnt_json_is_refused() -> None:
    bad = rubric()
    bad["examples"][0]["item"] = {"x": float("nan")}

    with pytest.raises(CaseError, match="rubric"):
        build_suite(items(), bad)


def test_odd_keys_in_a_file_are_an_error_not_a_crash(tmp_path: Path) -> None:
    (tmp_path / "r.yaml").write_text("1: x\ncolour: red\nmodel: m\n", encoding="utf-8")

    with pytest.raises(CaseError, match="unknown keys"):
        load_rubric(tmp_path / "r.yaml")


@pytest.mark.parametrize(
    "where",
    ["example", "instructions", "model", "item"],
)
def test_a_lone_surrogate_is_refused_as_load_suite_would(where: str) -> None:
    bad, entries = rubric(), items()
    if where == "example":
        bad["examples"][0]["reason"] = "\ud800"
    elif where == "instructions":
        bad["instructions"] = "grade \ud800"
    elif where == "model":
        bad["model"] = "anthropic:haiku\ud800"
    else:
        entries[0]["item"]["comment"] = "\ud800"

    with pytest.raises((CaseError, ItemsError)):
        build_suite(entries, bad)
