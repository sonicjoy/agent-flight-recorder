"""The case library's safety rules (#59): ids, paths, placeholders, YAML. No database.

Written from issue #59 before the code: cases go into git, and the repo goes public.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

from flight_recorder.evals.cases import (
    FAKES,
    CaseError,
    case_path,
    check_id,
    dump,
    faked,
    kept,
    load,
    placeholders,
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
        write_case(tmp_path, "smoke", case, local=False)

    assert key in str(error.value)
    assert not list(tmp_path.rglob("*.yaml"))


def test_write_refuses_a_bare_placeholder_unless_the_case_is_marked(tmp_path: Path) -> None:
    case = _case(expected={"answer": "[REDACTED:anthropic_key]"})

    with pytest.raises(CaseError):
        write_case(tmp_path, "smoke", case, local=False)
    path = write_case(tmp_path, "smoke", case | {"placeholders": True}, local=False)

    assert path.is_file()


def test_write_never_overwrites_a_case(tmp_path: Path) -> None:
    path = write_case(tmp_path, "smoke", _case(), local=False)
    before = path.read_bytes()

    with pytest.raises(CaseError, match="exists"):
        write_case(tmp_path, "smoke", _case(expected={"labels": ["other"]}), local=False)

    assert path.read_bytes() == before


def test_write_checks_the_case_id_too(tmp_path: Path) -> None:
    with pytest.raises(CaseError):
        write_case(tmp_path, "smoke", _case(id="../escape"), local=False)
    with pytest.raises(CaseError):
        write_case(tmp_path, "local", _case(), local=False)


def test_a_written_case_reads_back_the_same_with_safe_load(tmp_path: Path) -> None:
    case = _case(input={"prompt": "line one\nline two: with a colon", "n": 3})

    path = write_case(tmp_path, "smoke", case, local=False)

    assert load(path.read_text(encoding="utf-8")) == case
    assert path.read_text(encoding="utf-8") == dump(case)
    assert list(case) == list(load(dump(case)))  # key order kept: id first


def test_load_refuses_python_tags() -> None:
    with pytest.raises(CaseError):
        load("!!python/object/apply:os.system ['echo hi']")
