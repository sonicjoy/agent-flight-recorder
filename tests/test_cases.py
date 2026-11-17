"""The case library's safety rules (#59): ids, paths, placeholders, YAML. No database.

Written from issue #59 before the code: cases go into git, and the repo goes public.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from flight_recorder.evals.cases import (
    FAKES,
    CaseError,
    Draft,
    case_path,
    check_id,
    dump,
    faked,
    kept,
    load,
    placeholders,
    strip_fingerprints,
    suite_dir,
    write_case,
)
from flight_recorder.redact import Redactor

REDACTOR = Redactor(b"test-key-0123456789abcdef0123456789abcdef")


def _case(**more: Any) -> dict[str, Any]:
    return {
        "id": "run-1a2b3c4d",
        "from_run": "trace-001",
        "input": {"prompt": "triage #7"},
        "expected": {"labels": ["evals"]},
        "checks": [{"type": "equals", "field": "labels", "value": ["evals"]}],
    } | more


# --- ids and paths ------------------------------------------------------------------


@pytest.mark.parametrize("good", ["a", "run-1a2b3c4d", "smoke_2", "0", "a" * 64])
def test_ids_that_match_the_pattern_are_kept(good: str) -> None:
    assert check_id(good, "case id") == good


@pytest.mark.parametrize(
    "bad",
    ["", "../x", "..", "a/b", "a\\b", "/abs", "C:", "-a", "_a", "A", "a.b", "a b", "a" * 65,
     "a\n", "local"],
)  # fmt: skip
def test_ids_that_could_leave_the_folder_or_take_a_reserved_name_are_refused(bad: str) -> None:
    with pytest.raises(CaseError):
        check_id(bad, "suite")


def test_a_case_path_is_under_the_root_and_local_cases_under_local(tmp_path: Path) -> None:
    root = tmp_path / "evals" / "cases"

    shared = case_path(root, "smoke", "run-1", local=False)
    private = case_path(root, "smoke", "run-1", local=True)

    assert shared == root.resolve() / "smoke" / "run-1.yaml"
    assert private == root.resolve() / "local" / "smoke" / "run-1.yaml"


def test_a_suite_folder_is_under_the_root_and_local_suites_under_local(tmp_path: Path) -> None:
    root = tmp_path / "evals" / "cases"

    assert suite_dir(root, "smoke", local=False) == root.resolve() / "smoke"
    assert suite_dir(root, "smoke", local=True) == root.resolve() / "local" / "smoke"
    with pytest.raises(CaseError):
        suite_dir(root, "../x", local=False)


def test_a_suite_folder_refuses_a_link_that_leaves_the_root(tmp_path: Path) -> None:
    root, outside = tmp_path / "cases", tmp_path / "elsewhere"
    root.mkdir()
    outside.mkdir()
    try:
        (root / "smoke").symlink_to(outside, target_is_directory=True)
    except OSError:
        if sys.platform != "win32":
            pytest.skip("this OS user can't make symlinks")
        import _winapi  # a junction needs no privilege; resolve() follows it like a link

        _winapi.CreateJunction(str(outside), str(root / "smoke"))

    with pytest.raises(CaseError, match="outside"):
        suite_dir(root, "smoke", local=False)


def test_strip_fingerprints_leaves_bare_placeholders_and_adds_no_marker() -> None:
    key = REDACTOR.redact("sk-ant-" + "a" * 30).value
    value = {"labels": [key], "note": f"used {key}"}

    out = strip_fingerprints(value)

    assert out == {"labels": ["[REDACTED:anthropic_key]"], "note": "used [REDACTED:anthropic_key]"}
    assert "placeholders" not in out


def test_a_case_path_refuses_a_root_reached_through_a_link_outside_it(tmp_path: Path) -> None:
    root = tmp_path / "cases"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        (root / "smoke").symlink_to(outside, target_is_directory=True)
    except OSError:
        if sys.platform != "win32":
            pytest.skip("this OS user can't make symlinks")
        import _winapi  # a junction needs no privilege; resolve() follows it like a link

        _winapi.CreateJunction(str(outside), str(root / "smoke"))

    with pytest.raises(CaseError, match="outside"):
        case_path(root, "smoke", "run-1", local=False)


# --- placeholders -------------------------------------------------------------------


def test_placeholders_are_found_anywhere_in_the_case_expected_and_keys_included() -> None:
    key = REDACTOR.redact("use sk-ant-" + "a" * 30).value
    case = _case(
        input={"prompt": key, REDACTOR.redact("sk-ant-" + "b" * 30).value: 1},
        expected={"answer": "token " + REDACTOR.redact("ghp_" + "c" * 36).value},
    )

    found = placeholders(case)

    assert len(found) == 3
    assert all(p.startswith("[REDACTED:") for p in found)
    assert any(":anthropic_key:" in p for p in found)


def test_faked_swaps_each_placeholder_for_the_fixed_fake_of_its_kind() -> None:
    first = REDACTOR.redact("sk-ant-" + "a" * 30).value
    second = REDACTOR.redact("sk-ant-" + "b" * 30).value
    case = _case(input={"prompt": f"{first} then {second}", "other": "[REDACTED:mystery:1a2b3c4d]"})

    out = faked(case)

    assert out["input"]["prompt"] == f"{FAKES['anthropic_key']} then {FAKES['anthropic_key']}"
    assert out["input"]["other"] == "fake-mystery"
    assert placeholders(out) == []
    assert "placeholders" not in out


def test_fakes_are_not_realistic_so_the_redactor_and_scanners_leave_them_alone() -> None:
    for kind, fake in FAKES.items():
        assert REDACTOR.redact(fake).value == fake, kind
        assert "[REDACTED" not in fake


def test_kept_strips_fingerprints_and_marks_the_case() -> None:
    key = REDACTOR.redact("sk-ant-" + "a" * 30).value
    case = _case(expected={"answer": key})

    out = kept(case)

    assert out["expected"]["answer"] == "[REDACTED:anthropic_key]"
    assert out["placeholders"] is True
    assert placeholders(out) == []  # none with a fingerprint left


# --- writing ------------------------------------------------------------------------


def test_write_refuses_a_case_with_placeholders_and_lists_them(tmp_path: Path) -> None:
    key = REDACTOR.redact("sk-ant-" + "a" * 30).value
    case = _case(expected={"answer": key})

    with pytest.raises(CaseError) as error:
        write_case(tmp_path, Draft(case, "smoke", local=False))

    assert key in str(error.value)
    assert not list(tmp_path.rglob("*.yaml"))


def test_write_refuses_a_bare_placeholder_unless_the_case_is_marked(tmp_path: Path) -> None:
    case = _case(expected={"answer": "[REDACTED:anthropic_key]"})

    with pytest.raises(CaseError):
        write_case(tmp_path, Draft(case, "smoke", local=False))
    path = write_case(tmp_path, Draft(case | {"placeholders": True}, "smoke", local=False))

    assert path.is_file()


def test_write_never_overwrites_a_case(tmp_path: Path) -> None:
    path = write_case(tmp_path, Draft(_case(), "smoke", local=False))
    before = path.read_bytes()

    with pytest.raises(CaseError, match="exists"):
        write_case(tmp_path, Draft(_case(expected={"labels": ["other"]}), "smoke", local=False))

    assert path.read_bytes() == before


def test_write_checks_the_case_id_too(tmp_path: Path) -> None:
    with pytest.raises(CaseError):
        write_case(tmp_path, Draft(_case(id="../escape"), "smoke", local=False))
    with pytest.raises(CaseError):
        write_case(tmp_path, Draft(_case(), "local", local=False))


def test_a_written_case_reads_back_the_same_with_safe_load(tmp_path: Path) -> None:
    case = _case(input={"prompt": "line one\nline two: with a colon", "n": 3})

    path = write_case(tmp_path, Draft(case, "smoke", local=False))

    assert load(path.read_text(encoding="utf-8")) == case
    assert path.read_text(encoding="utf-8") == dump(case)
    assert list(case) == list(load(dump(case)))  # key order kept: id first


def test_load_refuses_python_tags() -> None:
    with pytest.raises(CaseError):
        load("!!python/object/apply:os.system ['echo hi']")


def test_a_value_shared_by_expected_and_checks_is_written_twice_not_as_an_alias() -> None:
    labels = ["infra", "recorder"]
    case = _case(expected={"labels": labels}, checks=[{"type": "equals", "field": "labels",
                                                       "value": labels}])  # fmt: skip

    text = dump(case)

    assert "&id" not in text and "*id" not in text
    assert load(text) == case


def test_multi_line_text_is_written_as_a_block_people_can_edit() -> None:
    case = _case(input={"prompt": "Triage this issue.\n\nTitle: auth\n"})

    text = dump(case)

    assert "prompt: |" in text
    assert "    Title: auth" in text
    assert load(text) == case
    awkward = _case(input={"prompt": "trailing space \n\tand a tab\n"})
    assert load(dump(awkward)) == awkward  # falls back to a quoted string when a block can't


# --- review fixes (.review/59-afr-case) -----------------------------------------------


@pytest.mark.parametrize("device", ["con", "prn", "aux", "nul", "com1", "lpt9"])
def test_windows_device_names_are_refused(device: str) -> None:
    with pytest.raises(CaseError, match="Windows"):
        check_id(device, "suite")


def test_a_local_case_is_ignored_by_git_wherever_the_cases_folder_is(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    root = tmp_path / "somewhere" / "else"  # no .gitignore rule names it

    path = write_case(root, Draft(_case(), "smoke", local=True))
    shared = write_case(root, Draft(_case(id="shared"), "smoke", local=False))

    def ignored(file: Path) -> bool:
        found = subprocess.run(["git", "-C", str(tmp_path), "check-ignore", "-q", str(file)])
        return found.returncode == 0

    assert path.parent.parent == root.resolve() / "local"
    assert ignored(path)
    assert not ignored(shared)
    assert (root / "local" / ".gitignore").read_text(encoding="utf-8").splitlines()[-1] == "*"


def test_the_git_test_leaves_alone_the_repo_whose_hook_runs_it(tmp_path: Path) -> None:
    """A commit hook in a linked worktree runs pytest with GIT_DIR naming that worktree."""
    repo, worktree = tmp_path / "repo", tmp_path / "worktree"
    # Its own git calls drop GIT_* too, so a broken fixture can't make this test commit elsewhere,
    # and --no-verify keeps a pre-commit hook from running the suite again.
    own = {name: value for name, value in os.environ.items() if not name.startswith("GIT_")}
    who = ["-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false"]
    for args in (
        ["init", "-q", str(repo)],
        ["-C", str(repo), *who, "commit", "-q", "--no-verify", "--allow-empty", "-m", "x"],
        ["-C", str(repo), "worktree", "add", "-q", "--detach", str(worktree)],
    ):
        subprocess.run(["git", *args], env=own, check=True)
    git_dir = repo / ".git" / "worktrees" / worktree.name
    hook_env = {**own, "GIT_DIR": str(git_dir), "GIT_INDEX_FILE": str(git_dir / "index")}
    config = (repo / ".git" / "config").read_text(encoding="utf-8")

    test = f"{__file__}::test_a_local_case_is_ignored_by_git_wherever_the_cases_folder_is"
    done = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", test],
        cwd=Path(__file__).parents[1],
        env=hook_env,
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert done.returncode == 0, done.stdout + done.stderr
    assert (repo / ".git" / "config").read_text(encoding="utf-8") == config


def test_a_write_the_os_refuses_is_a_case_error(tmp_path: Path) -> None:
    root = tmp_path / "file"
    root.write_text("not a folder", encoding="utf-8")

    with pytest.raises(CaseError, match="can't write"):
        write_case(root, Draft(_case(), "smoke", local=False))


def test_kept_marks_only_a_case_that_has_placeholders() -> None:
    assert "placeholders" not in kept(_case())


def test_line_breaks_a_block_would_change_survive_a_write() -> None:
    for brk in (0x85, 0x2028, 0x2029):  # NEL, line and paragraph separators
        case = _case(input={"prompt": "a" + chr(brk) + "b" + chr(10) + "c"})
        assert load(dump(case)) == case


def test_the_preview_escapes_what_yaml_leaves_raw() -> None:
    from flight_recorder.evals.cases import preview

    rlo, zwsp = chr(0x202E), chr(0x200B)  # right-to-left override, zero-width space
    text = preview(_case(input={"prompt": f"hi {rlo}evil {zwsp}there"}))

    assert rlo not in text and zwsp not in text
    assert "\\u202e" in text  # as an escape
