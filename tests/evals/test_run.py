"""Suites, calling the agent, and the outputs file behind `afr eval` (#15)."""

from __future__ import annotations

import json
import subprocess
import sys
import types
from collections.abc import Callable, Generator
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path
from typing import Any

import pytest
import yaml
from fake_secrets import secrets_by_kind
from pydantic import BaseModel

from flight_recorder.evals.cases import CaseError
from flight_recorder.evals.grade import input_hash
from flight_recorder.evals.run import (
    Suite,
    agent_version,
    git_sha,
    import_agent,
    load_suite,
    read_baseline,
    read_outputs,
    run_suite,
    write_baseline,
    write_outputs,
)
from flight_recorder.redact import Redactor

REDACTOR = Redactor(b"test-key-0123456789abcdef0123456789abcdef")
NOW = datetime(2026, 11, 17, 12, 0, tzinfo=UTC)
CHECK = {"type": "equals", "field": "type", "value": "bug"}


def write(folder: Path, name: str, data: Any) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_text(yaml.safe_dump(data), encoding="utf-8")


def case(id: str, **extra: Any) -> dict[str, Any]:
    return {"id": id, "input": {"title": id}, "checks": [CHECK]} | extra


@pytest.fixture
def root(tmp_path: Path) -> Path:
    folder = tmp_path / "cases" / "s"
    write(folder, "suite.yaml", {"agent": "evalagents:echo", "model": "m1"})
    write(folder, "a.yaml", case("a"))
    write(folder, "b.yaml", case("b"))
    return tmp_path / "cases"


@pytest.fixture(autouse=True)
def agents() -> Generator[dict[str, Callable[[dict[str, Any]], Any]]]:
    """A module `evalagents` whose functions the tests set."""
    module = types.ModuleType("evalagents")
    module.echo = lambda input: {"type": "bug", "seen": input}  # pyright: ignore[reportAttributeAccessIssue]
    module.not_callable = 3  # pyright: ignore[reportAttributeAccessIssue]
    sys.modules["evalagents"] = module
    yield module.__dict__
    del sys.modules["evalagents"]


def run(root: Path, *, inc: bool = False, agent: str | None = None):
    return run_suite(load_suite(root, "s", agent=agent), redactor=REDACTOR,
                     include_unreviewed=inc, now=NOW)  # fmt: skip


# --- the suite folder -----------------------------------------------------------------


def test_load_suite_reads_settings_and_cases_in_id_order(root: Path) -> None:
    suite = load_suite(root, "s")
    assert isinstance(suite, Suite)
    assert (suite.name, suite.agent, suite.model) == ("s", "evalagents:echo", "m1")
    assert suite.folder == (root / "s").resolve()
    assert [c["id"] for c in suite.cases] == ["a", "b"]
    assert load_suite(root, "s", agent="x:y").agent == "x:y"


def test_load_suite_ignores_subfolders_and_other_files(root: Path) -> None:
    write(root / "s" / "sub", "c.yaml", case("c"))
    (root / "s" / "notes.txt").write_text("x", encoding="utf-8")
    assert [c["id"] for c in load_suite(root, "s").cases] == ["a", "b"]


def test_a_suite_works_without_suite_yaml(root: Path) -> None:
    (root / "s" / "suite.yaml").unlink()
    assert load_suite(root, "s").agent is None


@pytest.mark.parametrize(
    ("setup", "message"),
    [
        (lambda r: None, "no suite 'nope'"),
        (lambda r: write(r / "local" / "s", "a.yaml", case("a")), "in both"),
        (lambda r: write(r / "s", "c.yml", case("c")), "use .yaml"),
        (lambda r: write(r / "s", "c.yaml", case("other")), "must be 'c'"),
        (lambda r: write(r / "s", "c.yaml", case("c") | {"id": 3}), "must be 'c'"),
        (lambda r: write(r / "s", "suite.yaml", {"agent": "a:b", "x": 1}), "unknown key 'x'"),
        (lambda r: write(r / "s", "suite.yaml", {"agent": 3}), "'agent' must be text"),
        (lambda r: write(r / "s", "c.yaml", case("c", input="text")), "'input' must be a mapping"),
        (lambda r: write(r / "s", "c.yaml", case("c", checks={})), "checks must be a list"),
        (lambda r: write(r / "s", "c.yaml", case("c", unreviewed="yes")), "true or false"),
        (lambda r: write(r / "s", "c.yaml", case("c", checks=[{"type": "x"}])), "unknown type"),
        (lambda r: write(r / "s", "BAD.yaml", case("BAD")), "case id 'BAD'"),
        (lambda r: (r / "s" / "c.yaml").write_text("[1, 2]", encoding="utf-8"), "c.yaml"),
        (lambda r: (r / "s" / "c.yaml").write_text("a: [", encoding="utf-8"), "c.yaml"),
    ],
)
def test_suite_folder_errors(root: Path, setup: Callable[[Path], Any], message: str) -> None:
    setup(root)
    with pytest.raises(CaseError, match=message.replace("(", r"\(")):
        load_suite(root, "nope" if "nope" in message else "s")


def test_unreviewed_cases_are_validated_too(root: Path) -> None:
    write(root / "s", "c.yaml", case("c", unreviewed=True, checks=[{"type": "equals"}]))
    with pytest.raises(CaseError, match="needs 'field'"):
        load_suite(root, "s")


def test_a_local_suite_is_read_from_local_and_written_there(root: Path) -> None:
    write(root / "local" / "mine", "a.yaml", case("a"))
    suite = load_suite(root, "mine", agent="evalagents:echo")
    assert suite.folder == (root / "local" / "mine").resolve()
    header, lines = run_suite(suite, redactor=REDACTOR, include_unreviewed=False, now=NOW)
    write_outputs(suite.folder, header, lines)
    assert (root / "local" / "mine" / "outputs.jsonl").exists()
    assert not (root / "mine").exists()


# --- calling the agent ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("spec", "message"),
    [
        ("no_colon", "module:function"),
        ("missing_module_xyz:f", "can't import missing_module_xyz"),
        ("evalagents:nope", "has no 'nope'"),
        ("evalagents:not_callable", "isn't callable"),
    ],
)
def test_import_agent_errors(spec: str, message: str) -> None:
    with pytest.raises(CaseError, match=message):
        import_agent(spec)


def test_run_needs_an_agent(root: Path) -> None:
    (root / "s" / "suite.yaml").unlink()
    with pytest.raises(CaseError, match="no agent"):
        run(root)


def test_each_case_is_called_once_with_its_input_and_hashed(root: Path) -> None:
    header, lines = run(root)
    assert sorted(lines) == ["a", "b"]
    assert lines["a"] == {
        "case": "a",
        "error": None,
        "input_hash": input_hash(case("a")),
        "output": {"seen": {"title": "a"}, "type": "bug"},
    }
    assert header == {
        "agent": "evalagents:echo",
        "agent_version": None,
        "git_sha": None,
        "model": "m1",
        "ran_at": "2026-11-17T12:00:00Z",
    }


def test_skipped_cases_arent_called(root: Path, agents: dict[str, Any]) -> None:
    write(root / "s", "c.yaml", case("c", unreviewed=True))
    write(root / "s", "d.yaml", case("d", checks=[]))
    assert sorted(run(root)[1]) == ["a", "b"]
    assert sorted(run(root, inc=True)[1]) == ["a", "b", "c"]


class Answer(BaseModel):
    type: str


def test_agent_failures_become_error_lines_and_the_run_goes_on(
    root: Path, agents: dict[str, Any]
) -> None:
    def flaky(input: dict[str, Any]) -> Any:
        if input["title"] == "a":
            raise ValueError("no title")
        return ["not", "a", "dict"]

    agents["flaky"] = flaky
    lines = run(root, agent="evalagents:flaky")[1]
    assert (lines["a"]["output"], lines["a"]["error"]) == (None, "ValueError: no title")
    assert (lines["b"]["output"], lines["b"]["error"]) == (None, "output not a dict: list")


def test_an_output_json_cant_hold_is_an_error(root: Path, agents: dict[str, Any]) -> None:
    agents["odd"] = lambda input: {"x": object()}
    assert run(root, agent="evalagents:odd")[1]["a"]["error"] == "output not JSON: object"


def test_a_pydantic_model_counts_as_a_dict(root: Path, agents: dict[str, Any]) -> None:
    agents["model"] = lambda input: Answer(type="bug")
    assert run(root, agent="evalagents:model")[1]["a"]["output"] == {"type": "bug"}


def test_an_agent_that_changes_its_input_changes_nothing_else(
    root: Path, agents: dict[str, Any]
) -> None:
    def greedy(input: dict[str, Any]) -> dict[str, Any]:
        input["title"] = "changed"
        return {"type": "bug"}

    agents["greedy"] = greedy
    suite = load_suite(root, "s", agent="evalagents:greedy")
    _, lines = run_suite(suite, redactor=REDACTOR, include_unreviewed=False, now=NOW)
    assert suite.cases[0]["input"] == {"title": "a"}
    assert lines["a"]["input_hash"] == input_hash(case("a"))


def test_a_secret_in_output_is_redacted_without_a_fingerprint(
    root: Path, agents: dict[str, Any]
) -> None:
    secret = secrets_by_kind()["anthropic_key"]
    agents["leaky"] = lambda input: {"type": "bug", "note": f"key {secret}"}
    agents["leaky_error"] = lambda input: (_ for _ in ()).throw(RuntimeError(secret))
    out = run(root, agent="evalagents:leaky")[1]["a"]
    err = run(root, agent="evalagents:leaky_error")[1]["a"]
    assert out["output"] == {"note": "key [REDACTED:anthropic_key]", "type": "bug"}
    assert err["error"] == "RuntimeError: [REDACTED:anthropic_key]"
    assert secret not in json.dumps([out, err])
    assert "placeholders" not in out["output"]


# --- the header -----------------------------------------------------------------------


def test_agent_version_is_the_installed_distribution_of_its_package(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "flight_recorder.evals.run._distributions", lambda: {"evalagents": ["PyYAML"]}
    )
    assert agent_version("evalagents:echo") == version("PyYAML")


def test_agent_version_of_the_smoke_agent_and_of_an_unknown_one() -> None:
    assert agent_version("flight_recorder.evals.example:triage") == version("agent-flight-recorder")
    assert agent_version("evalagents:echo") is None


def test_agent_version_falls_back_to_the_packages_version(agents: dict[str, Any]) -> None:
    agents["__version__"] = "2.5.0"
    assert agent_version("evalagents:echo") == "2.5.0"


def _git(folder: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(folder), *args], check=True, capture_output=True)


def test_git_sha_is_short_clean_dirty_or_null(tmp_path: Path) -> None:
    assert git_sha(tmp_path) is None
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "f.txt").write_text("1", encoding="utf-8")
    _git(tmp_path, "add", "f.txt")
    _git(tmp_path, "commit", "-q", "-m", "one")
    sha = git_sha(tmp_path)
    assert sha is not None and len(sha) >= 7 and not sha.endswith("-dirty")
    (tmp_path / "new.txt").write_text("untracked", encoding="utf-8")
    assert git_sha(tmp_path) == sha  # untracked files don't count
    (tmp_path / "f.txt").write_text("2", encoding="utf-8")
    assert git_sha(tmp_path) == f"{sha}-dirty"


# --- the outputs file and the baseline ------------------------------------------------


def test_outputs_are_written_in_case_order_as_canonical_json(root: Path) -> None:
    folder = root / "s"
    header, lines = run(root)
    write_outputs(folder, header, {"b": lines["b"], "a": lines["a"]})
    raw = (folder / "outputs.jsonl").read_bytes()
    assert b"\r" not in raw and raw.endswith(b"\n")
    rows = raw.decode("utf-8").splitlines()
    assert rows[0] == json.dumps({"run": header}, sort_keys=True, separators=(",", ":"))
    assert [json.loads(r)["case"] for r in rows[1:]] == ["a", "b"]
    assert read_outputs(folder) == (header, lines)


def test_no_outputs_file_reads_as_nothing(tmp_path: Path) -> None:
    assert read_outputs(tmp_path) == (None, {})


def test_an_interrupted_write_leaves_the_old_outputs(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = root / "s"
    header, lines = run(root)
    write_outputs(folder, header, lines)
    before = (folder / "outputs.jsonl").read_bytes()

    def broken(*args: Any) -> None:
        raise OSError("disk full")

    monkeypatch.setattr("flight_recorder.evals.run.os.replace", broken)
    with pytest.raises(OSError, match="disk full"):
        write_outputs(folder, header | {"model": "m2"}, lines)
    assert (folder / "outputs.jsonl").read_bytes() == before
    assert sorted(p.name for p in folder.iterdir() if p.suffix not in (".yaml",)) == [
        "outputs.jsonl"
    ]


@pytest.mark.parametrize(
    "text",
    [
        '{"case":"a","input_hash":"x","output":null,"error":null}\n',
        '{"run":{}}\nnot json\n',
        '{"run":{}}\n{"case":"a"}\n{"case":"a"}\n',
        "",
    ],
)
def test_a_damaged_outputs_file_is_an_error(tmp_path: Path, text: str) -> None:
    (tmp_path / "outputs.jsonl").write_text(text, encoding="utf-8")
    with pytest.raises(CaseError, match=r"outputs.jsonl is damaged: run the suite again"):
        read_outputs(tmp_path)


def test_the_baseline_round_trips_with_sorted_keys_and_a_final_newline(tmp_path: Path) -> None:
    assert read_baseline(tmp_path) is None
    baseline = {"b": {"x/2": "fail"}, "a": {"y/1": "pass", "x/1": "error"}}
    write_baseline(tmp_path, baseline)
    raw = (tmp_path / "baseline.json").read_bytes()
    assert raw == (json.dumps(baseline, sort_keys=True, indent=2) + "\n").encode()
    assert read_baseline(tmp_path) == baseline


def test_a_damaged_baseline_is_an_error(tmp_path: Path) -> None:
    (tmp_path / "baseline.json").write_text('{"a": ["pass"]}', encoding="utf-8")
    with pytest.raises(CaseError, match=r"baseline.json is damaged"):
        read_baseline(tmp_path)


# --- one case can't stop the run, and every bad input is a CaseError ------------------


def test_no_answer_an_agent_gives_stops_the_run(root: Path, agents: dict[str, Any]) -> None:
    """The property: whatever one case's call does short of Ctrl-C, the run goes on and the
    case gets an error line."""
    cyclic: dict[str, Any] = {}
    cyclic["self"] = cyclic
    deep: dict[str, Any] = {}
    node = deep
    for _ in range(5000):
        node["n"] = {}
        node = node["n"]
    near_limit: dict[str, Any] = {}
    node = near_limit
    for _ in range(900):  # convert copes; redaction or storage may not
        node["n"] = {}
        node = node["n"]

    def bad(input: dict[str, Any]) -> Any:
        title = input["title"]
        if title == "cyclic":
            return cyclic
        if title == "deep":
            return deep
        if title == "near":
            return near_limit
        if title == "exit":
            raise SystemExit(0)
        print("noise on stdout")
        return {"type": "bug"}

    agents["bad"] = bad
    for name in ("cyclic", "deep", "near", "exit"):
        write(root / "s", f"{name}.yaml", case(name))
    header, lines = run(root, agent="evalagents:bad")
    assert lines["a"]["output"] == {"type": "bug"}
    assert lines["cyclic"]["error"] == "output not JSON: nested too deep"
    assert lines["deep"]["error"] == "output not JSON: nested too deep"
    assert lines["exit"]["error"] == "SystemExit: 0"
    assert lines["near"]["error"] in (None, "output not storable: RecursionError")
    write_outputs(root / "s", header, lines)  # every line can be stored
    assert read_outputs(root / "s")[1].keys() == lines.keys()


def test_what_an_agent_prints_goes_to_stderr(
    root: Path, agents: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    agents["chatty"] = lambda input: print("thinking...") or {"type": "bug"}
    run(root, agent="evalagents:chatty")
    out, err = capsys.readouterr()
    assert "thinking..." not in out
    assert "thinking..." in err


@pytest.mark.parametrize("spec", ["subprocess:run", "os:system", "builtins:eval", "json:loads"])
def test_an_agent_from_the_standard_library_is_refused(spec: str) -> None:
    with pytest.raises(CaseError, match=r"not an agent's module|not a function"):
        import_agent(spec)


def test_an_agent_module_that_fails_to_import_in_any_way_is_a_case_error() -> None:
    sys.modules.pop("evalbroken", None)
    source = "raise KeyError('AGENT_KEY')"
    import importlib.abc
    import importlib.util

    class Finder(importlib.abc.MetaPathFinder, importlib.abc.Loader):
        def find_spec(self, name: str, path: Any, target: Any = None) -> Any:
            return importlib.util.spec_from_loader(name, self) if name == "evalbroken" else None

        def exec_module(self, module: types.ModuleType) -> None:
            exec(source, module.__dict__)

    finder = Finder()
    sys.meta_path.insert(0, finder)
    try:
        with pytest.raises(CaseError, match="can't import evalbroken: KeyError"):
            import_agent("evalbroken:f")
    finally:
        sys.meta_path.remove(finder)
        sys.modules.pop("evalbroken", None)


def test_an_input_json_cant_hold_is_refused_when_the_suite_loads(root: Path) -> None:
    (root / "s" / "c.yaml").write_text(
        "id: c\ninput: {1: one}\nchecks: [{type: equals, field: x, value: 1}]\n",
        encoding="utf-8",
    )
    with pytest.raises(CaseError, match="'input' can't be stored as JSON"):
        load_suite(root, "s")


@pytest.mark.parametrize("name", ["c.YAML", "c.Yml", "c.YML"])
def test_a_case_file_with_an_odd_extension_is_refused_not_skipped(root: Path, name: str) -> None:
    write(root / "s", name, case("c"))
    with pytest.raises(CaseError, match=r"use .yaml"):
        load_suite(root, "s")


def test_a_yaml_date_in_a_check_compares_as_the_text_an_output_holds(
    root: Path, agents: dict[str, Any]
) -> None:
    from flight_recorder.evals.grade import grade_suite

    (root / "s" / "a.yaml").write_text(
        "id: a\ninput: {title: a}\nchecks: [{type: equals, field: due, value: 2024-01-02}]\n",
        encoding="utf-8",
    )
    agents["dated"] = lambda input: {"due": datetime(2024, 1, 2).date()}
    suite = load_suite(root, "s", agent="evalagents:dated")
    _, lines = run_suite(suite, redactor=REDACTOR, include_unreviewed=False, now=NOW)
    result = grade_suite(suite.cases, lines, include_unreviewed=False)
    assert next(c for c in result.cases if c.id == "a").outcome == "pass"
    assert suite.cases[0]["checks"][0]["value"] == "2024-01-02"


@pytest.mark.parametrize(
    "line",
    [
        '{"case":"a","input_hash":"x","output":[1],"error":null}',
        '{"case":"a","input_hash":"x","output":null,"error":3}',
        '{"case":"a","input_hash":null,"output":null,"error":null}',
    ],
)
def test_an_outputs_line_of_the_wrong_shape_is_damage(tmp_path: Path, line: str) -> None:
    (tmp_path / "outputs.jsonl").write_text('{"run":{}}\n' + line + "\n", encoding="utf-8")
    with pytest.raises(CaseError, match=r"outputs\.jsonl is damaged"):
        read_outputs(tmp_path)


def test_no_failure_inside_one_case_stops_the_run(root: Path, agents: dict[str, Any]) -> None:
    """The property at the one place every case passes: anything short of Ctrl-C."""
    import asyncio

    class Unprintable(Exception):
        def __str__(self) -> str:
            raise RuntimeError("no")

    def bad(input: dict[str, Any]) -> Any:
        title = input["title"]
        if title == "surrogate":
            return {"type": "\ud83d"}
        if title == "raised_surrogate":
            raise ValueError("\ud83d")
        if title == "unprintable":
            raise Unprintable
        if title == "cancelled":
            raise asyncio.CancelledError
        return {"type": "bug"}

    agents["bad2"] = bad
    names = ("surrogate", "raised_surrogate", "unprintable", "cancelled")
    for name in names:
        write(root / "s", f"{name}.yaml", case(name))
    header, lines = run(root, agent="evalagents:bad2")
    assert lines["a"]["output"] == {"type": "bug"}
    assert lines["surrogate"]["error"] == "output not storable: UnicodeEncodeError"
    assert lines["raised_surrogate"]["error"] == "output not storable: UnicodeEncodeError"
    assert lines["unprintable"]["error"] == "Unprintable: (no message)"
    assert lines["cancelled"]["error"].startswith("CancelledError")
    write_outputs(root / "s", header, lines)
    assert read_outputs(root / "s")[1].keys() == lines.keys()


def test_ctrl_c_still_stops_the_run(root: Path, agents: dict[str, Any]) -> None:
    def interrupted(input: dict[str, Any]) -> Any:
        raise KeyboardInterrupt

    agents["interrupted"] = interrupted
    with pytest.raises(KeyboardInterrupt):
        run(root, agent="evalagents:interrupted")


def test_a_print_while_the_agent_imports_goes_to_stderr(
    root: Path, agents: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    import importlib.abc
    import importlib.util

    class Finder(importlib.abc.MetaPathFinder, importlib.abc.Loader):
        def find_spec(self, name: str, path: Any, target: Any = None) -> Any:
            return importlib.util.spec_from_loader(name, self) if name == "evalloud" else None

        def exec_module(self, module: types.ModuleType) -> None:
            exec("print('loading...')\ndef agent(input):\n    return {'type': 'bug'}\n",
                 module.__dict__)  # fmt: skip

    finder = Finder()
    sys.meta_path.insert(0, finder)
    try:
        run(root, agent="evalloud:agent")
    finally:
        sys.meta_path.remove(finder)
        sys.modules.pop("evalloud", None)
    out, err = capsys.readouterr()
    assert "loading..." not in out
    assert "loading..." in err


@pytest.mark.parametrize(
    "spec", ["flight_recorder.cli:app", "flight_recorder.evals.run:Suite", "evalagents:partial"]
)
def test_an_agent_that_isnt_a_function_is_refused(spec: str, agents: dict[str, Any]) -> None:
    import functools

    agents["partial"] = functools.partial(lambda input, x: {}, x=1)
    with pytest.raises(CaseError, match="not a function"):
        import_agent(spec)


def test_deep_nesting_in_a_case_or_an_outputs_line_is_a_case_error(root: Path) -> None:
    (root / "s" / "c.yaml").write_text("x: " + "[" * 5000 + "]" * 5000 + "\n", encoding="utf-8")
    with pytest.raises(CaseError, match="nested too deep"):
        load_suite(root, "s")
    folder = root / "deep"
    folder.mkdir()
    deep = '{"run":{}}\n{"case":"a","input_hash":"x","error":null,"output":' + '{"n":' * 5000
    (folder / "outputs.jsonl").write_text(deep + "1" + "}" * 5001 + "\n", encoding="utf-8")
    with pytest.raises(CaseError, match=r"outputs\.jsonl is damaged"):
        read_outputs(folder)


def test_an_outputs_line_holding_a_lone_surrogate_is_damage(tmp_path: Path) -> None:
    line = '{"case":"a","input_hash":"x","error":null,"output":{"t":"\\ud83d"}}'
    (tmp_path / "outputs.jsonl").write_text('{"run":{}}\n' + line + "\n", encoding="utf-8")
    with pytest.raises(CaseError, match=r"outputs\.jsonl is damaged"):
        read_outputs(tmp_path)
