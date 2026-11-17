"""`afr eval run` and `afr eval grade` on a copy of the smoke suite (#15).

Written from docs/designs/eval-harness.md before the CLI.
"""

from __future__ import annotations

import json
import shutil
import sys
import types
from collections.abc import Callable, Generator
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from flight_recorder.cli import app
from flight_recorder.evals import agent
from flight_recorder.evals.example import triage

runner = CliRunner()
SMOKE = Path(__file__).parent.parent / "evals" / "cases" / "smoke"


@pytest.fixture
def root(tmp_path: Path) -> Path:
    """The smoke suite's cases, without its saved outputs and baseline."""
    folder = tmp_path / "cases" / "smoke"
    folder.mkdir(parents=True)
    for path in SMOKE.glob("*.yaml"):
        shutil.copy(path, folder / path.name)
    return tmp_path / "cases"


@pytest.fixture
def afr(root: Path, clean_env: pytest.MonkeyPatch, tmp_path: Path) -> Callable[..., Any]:
    clean_env.setenv("AFR_HOME", str(tmp_path / "home"))  # a fresh key, never the real one
    clean_env.setenv("COLUMNS", "200")

    def invoke(command: str, *args: str) -> Any:
        return runner.invoke(app, ["eval", command, "--suite", "smoke", "--root", str(root),
                                   *args])  # fmt: skip

    return invoke


@pytest.fixture
def accepted(afr: Callable[..., Any]) -> Callable[..., Any]:
    result = afr("run", "--accept")
    assert result.exit_code == 0, result.output
    return afr


@pytest.fixture
def agents() -> Generator[dict[str, Any]]:
    module = types.ModuleType("evalclitest")
    sys.modules["evalclitest"] = module
    yield module.__dict__
    del sys.modules["evalclitest"]


def edit(root: Path, case_id: str, change: Callable[[dict[str, Any]], None]) -> None:
    path = root / "smoke" / f"{case_id}.yaml"
    case = yaml.safe_load(path.read_text(encoding="utf-8"))
    change(case)
    path.write_text(yaml.safe_dump(case), encoding="utf-8")


def outputs(root: Path) -> list[str]:
    return (root / "smoke" / "outputs.jsonl").read_text(encoding="utf-8").splitlines()


def test_run_then_grade_pass_and_a_rerun_changes_only_the_header(
    accepted: Callable[..., Any], root: Path
) -> None:
    before = outputs(root)
    run, grade = accepted("run"), accepted("grade")
    assert (run.exit_code, grade.exit_code) == (0, 0), run.output + grade.output
    assert "5/8 passed · 2 failed · 0 error · 0 stale · 1 skipped" in grade.output
    assert outputs(root)[1:] == before[1:]
    assert (root / "smoke" / "baseline.json").exists()


def test_with_no_baseline_the_report_says_to_accept(afr: Callable[..., Any]) -> None:
    result = afr("run")
    assert result.exit_code == 0
    assert "rerun with --accept" in result.output


def test_an_edited_input_makes_grade_report_stale_and_fail(
    accepted: Callable[..., Any], root: Path
) -> None:
    edit(root, "smoke-01", lambda c: c["input"].update(title="App crash on exit"))
    result = accepted("grade")
    assert result.exit_code == 1
    assert "stale: smoke-01" in result.output


def test_an_edited_check_fails_both_until_accepted(
    accepted: Callable[..., Any], root: Path
) -> None:
    edit(root, "smoke-03", lambda c: c["checks"].append(
        {"type": "contains", "field": "labels", "value": "bug"}))  # fmt: skip
    grade, run = accepted("grade"), accepted("run")
    assert (grade.exit_code, run.exit_code) == (1, 1)
    assert "changed checks: smoke-03" in grade.output
    assert accepted("run", "--accept").exit_code == 0
    assert (accepted("run").exit_code, accepted("grade").exit_code) == (0, 0)


def test_a_check_that_starts_passing_fails_grade_but_not_run(
    accepted: Callable[..., Any], root: Path, agents: dict[str, Any]
) -> None:
    def better(input: dict[str, Any]) -> dict[str, Any]:
        answer = triage(input)
        return answer | {"type": answer["type"] or "performance"} if "Timeout" in input[
            "title"] else answer  # fmt: skip

    agents["better"] = agent(better)
    run = accepted("run", "--agent", "evalclitest:better")
    assert run.exit_code == 0, run.output
    assert "improvements: smoke-05 type/" in run.output
    grade = accepted("grade")
    assert grade.exit_code == 1  # the saved outputs now differ from the baseline
    assert "improvements: smoke-05" in grade.output
    assert "warning: outputs.jsonl was made by evalclitest:better" in grade.output


def test_a_new_case_and_a_removed_case_fail_only_grade(
    accepted: Callable[..., Any], root: Path
) -> None:
    shutil.copy(root / "smoke" / "smoke-01.yaml", root / "smoke" / "smoke-09.yaml")
    edit(root, "smoke-09", lambda c: c.update(id="smoke-09"))
    (root / "smoke" / "smoke-07.yaml").unlink()
    run = accepted("run")
    assert run.exit_code == 0, run.output
    grade = accepted("grade")
    assert grade.exit_code == 1
    assert "new cases: smoke-09" in grade.output
    assert "removed cases: smoke-07" in grade.output


def test_accept_refuses_while_a_case_is_stale_and_writes_nothing(
    accepted: Callable[..., Any], root: Path
) -> None:
    baseline = (root / "smoke" / "baseline.json").read_bytes()
    edit(root, "smoke-02", lambda c: c["input"].update(body="more"))
    result = accepted("grade", "--accept")
    assert result.exit_code == 1
    assert "not accepted: stale cases (smoke-02)" in result.output
    assert (root / "smoke" / "baseline.json").read_bytes() == baseline


def test_json_prints_one_object_with_the_documented_keys(accepted: Callable[..., Any]) -> None:
    result = accepted("grade", "--json")
    assert result.exit_code == 0
    report = json.loads(result.stdout)
    assert set(report) == {"suite", "header", "counts", "fields", "cases", "changes", "exit_code"}
    assert report["counts"] == {"pass": 5, "fail": 2, "error": 0, "stale": 0, "skipped": 1}
    assert report["fields"]["labels"] == {"passed": 4, "graded": 5}
    assert set(report["changes"]) == {"regressions", "improvements", "changed", "new", "removed",
                                      "skipped_in_baseline", "stale", "orphaned"}  # fmt: skip
    smoke05 = next(c for c in report["cases"] if c["id"] == "smoke-05")
    assert smoke05["outcome"] == "fail"
    assert smoke05["checks"][0]["reason"] == "wrong value"


def test_unreviewed_cases_are_skipped_unless_asked_and_never_baselined(
    accepted: Callable[..., Any], root: Path
) -> None:
    baseline = json.loads((root / "smoke" / "baseline.json").read_text(encoding="utf-8"))
    assert "smoke-08" not in baseline
    result = accepted("run", "--include-unreviewed", "--accept")
    assert result.exit_code == 0
    assert "smoke-08 (unreviewed) type/" in result.output  # graded, and failing
    baseline = json.loads((root / "smoke" / "baseline.json").read_text(encoding="utf-8"))
    assert "smoke-08" not in baseline
    assert accepted("grade").exit_code == 0  # its line is there, but it's skipped again


def test_an_unreviewed_case_with_no_line_is_skipped_when_graded_on_request(
    accepted: Callable[..., Any],
) -> None:
    result = accepted("grade", "--include-unreviewed")
    assert result.exit_code == 0
    assert "smoke-08 (unreviewed): skipped: no output: run it with --include-unreviewed" in (
        result.output
    )


def test_a_usage_error_exits_2_with_escaped_text(afr: Callable[..., Any], root: Path) -> None:
    result = runner.invoke(app, ["eval", "grade", "--suite", "a\x1b[31mb", "--root", str(root)])
    assert result.exit_code == 2
    assert "\x1b" not in result.output
    (root / "smoke" / "bad.yml").write_text("id: bad\n", encoding="utf-8")
    result = afr("grade")
    assert result.exit_code == 2
    assert "use .yaml" in result.output


def test_a_damaged_outputs_file_exits_2(accepted: Callable[..., Any], root: Path) -> None:
    (root / "smoke" / "outputs.jsonl").write_text("not json\n", encoding="utf-8")
    result = accepted("grade")
    assert result.exit_code == 2
    assert "outputs.jsonl is damaged" in result.output


def test_neither_command_opens_the_store(
    afr: Callable[..., Any], clean_env: pytest.MonkeyPatch
) -> None:
    clean_env.setenv("AFR_DATABASE_URL", "postgresql+psycopg://afr:afr@127.0.0.1:1/afr")
    assert afr("run", "--accept").exit_code == 0
    assert afr("grade").exit_code == 0


def test_a_path_in_an_error_reaches_the_terminal_escaped(tmp_path: Path, afr: Any) -> None:
    root = tmp_path / "odd\x1b[31mroot"
    result = runner.invoke(app, ["eval", "grade", "--suite", "smoke", "--root", str(root)])
    assert result.exit_code == 2
    assert "\x1b" not in result.output
    assert "\\x1b" in result.output


def test_json_stays_one_ascii_object_whatever_the_agent_prints_or_answers(
    accepted: Callable[..., Any], agents: dict[str, Any]
) -> None:
    def odd(input: dict[str, Any]) -> dict[str, Any]:
        print("progress: 50%")
        answer = triage(input)  # a type no check expects, so it's printed as the actual value
        return answer | {"type": answer["type"] or "\u009b31m \u202e\u200b"}

    agents["odd"] = agent(odd)
    result = accepted("run", "--agent", "evalclitest:odd", "--json")
    assert result.stdout.isascii()
    assert "\\u009b31m \\u202e\\u200b" in result.stdout
    report = json.loads(result.stdout)
    smoke05 = next(c for c in report["cases"] if c["id"] == "smoke-05")
    assert smoke05["checks"][0]["actual"] == "\u009b31m \u202e\u200b"


def test_a_bad_redaction_key_exits_2(
    afr: Callable[..., Any], clean_env: pytest.MonkeyPatch
) -> None:
    clean_env.setenv("AFR_REDACTION_KEY", "short")
    result = afr("run")
    assert result.exit_code == 2
    assert "AFR_REDACTION_KEY must be at least" in result.output
