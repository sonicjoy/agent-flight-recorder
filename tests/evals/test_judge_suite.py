"""Judge suites from an items file and a rubric (#17, docs/designs/llm-judge.md)."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest
import yaml
from judge_fixtures import items, rubric

from flight_recorder.evals.cases import CaseError
from flight_recorder.evals.judge_suite import (
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
