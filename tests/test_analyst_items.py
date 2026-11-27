"""The analyst's items and signatures (#22, docs/designs/analyst.md §2)."""

from __future__ import annotations

import random
from typing import Any

import pytest

from flight_recorder.analyst.items import error_signature, graded_items, signature

RUN = "11111111-2222-3333-4444-555555555555"
STARTED = "2026-11-12T09:00:00+00:00"


def items(attributes: dict[str, Any]) -> list[dict[str, Any]]:
    return graded_items(RUN, STARTED, attributes)


@pytest.mark.parametrize(
    ("decided", "expected", "wanted"),
    [
        (["a", "b"], ["a"], "labels:extra"),  # added, missed none
        (["a"], ["a", "b"], "labels:missed"),  # missed, added none
        (["a", "c"], ["a", "b"], "labels:wrong"),  # both
        (["a"], None, "labels:extra"),  # the truth is empty
        (None, ["a"], "labels:missed"),  # the agent's value is empty
        (["b"], ["a"], "labels:wrong"),
    ],
)
def test_a_list_fields_signature_says_what_the_agent_added_or_missed(
    decided: Any, expected: Any, wanted: str
) -> None:
    assert signature("labels", decided, expected) == wanted


@pytest.mark.parametrize(
    ("decided", "expected", "wanted"),
    [
        ("25", None, "duplicate_of:extra"),
        (None, "25", "duplicate_of:missed"),
        ("25", "7", "duplicate_of:wrong"),
    ],
)
def test_a_single_fields_signature_says_extra_missed_or_wrong(
    decided: Any, expected: Any, wanted: str
) -> None:
    assert signature("duplicate_of", decided, expected) == wanted


def test_a_graded_run_gives_one_item_per_wrong_field_in_the_recorded_formats() -> None:
    found = items({
        "triager.decided.labels": ["evals", "infra"], "triager.expected.labels": ["evals"],
        "triager.decided.duplicate_of": "25", "triager.expected.duplicate_of": "(none)",
        "triager.decided.milestone": "Week 3", "triager.expected.milestone": "Week 3",
        "triager.grade": "wrong",
    })  # fmt: skip

    assert found == [
        {"id": f"{RUN}#duplicate_of", "run": RUN, "started": STARTED,
         "signature": "duplicate_of:extra", "text": "duplicate_of: decided 25; expected (none)"},
        {"id": f"{RUN}#labels", "run": RUN, "started": STARTED, "signature": "labels:extra",
         "text": "labels: decided evals, infra; expected evals"},
    ]  # fmt: skip


def test_the_grade_key_decides_which_fields_are_wrong_when_present() -> None:
    found = items({
        "triager.decided.labels": ["evals", "infra"], "triager.expected.labels": ["evals"],
        "triager.grade.labels": "ok",  # matched an accepted answer
        "triager.decided.milestone": "Week 2", "triager.expected.milestone": "Week 2",
        "triager.grade.milestone": "wrong",
    })  # fmt: skip

    assert [i["signature"] for i in found] == ["milestone:wrong"]


def test_without_a_grade_key_a_decision_matching_an_accepted_answer_is_not_wrong() -> None:
    found = items({
        "triager.decided.labels": ["evals", "infra"], "triager.expected.labels": ["evals"],
        "triager.accepted.labels": ["evals, infra"],
        "triager.decided.labels_none": "(none)", "triager.expected.labels_none": [],
    })  # fmt: skip

    assert found == []  # accepted, and (none) is the same nothing as []


def test_a_failed_run_with_no_decision_gives_no_graded_items() -> None:
    assert items({"triager.expected.labels": ["evals"], "triager.grade": "failed"}) == []


def test_the_error_signature_keeps_the_class_and_drops_the_numbers() -> None:
    assert error_signature("llm", "ChatOpenAI", "RateLimitError: 429 retry in 12s") == (
        "error:llm:ChatOpenAI:RateLimitError"
    )
    timeout = error_signature("tool", "fetch", "timeout after 30s")
    assert timeout == "error:tool:fetch:timeout after 00s"
    assert error_signature("tool", "fetch", None) == "error:tool:fetch:unknown"
    assert len(error_signature("tool", "f", "x" * 100)) == len("error:tool:f:") + 40


def test_items_are_the_same_every_time_and_never_carry_unrecorded_text() -> None:
    """Property, seeded: graded items depend only on the attributes given, come out the same
    on every call, and name only the fields and values recorded."""
    rng = random.Random(22)
    values: list[Any] = ["a", "b", "(none)", ["a"], ["a", "b"], [], "Week 3"]
    for _ in range(300):
        attributes: dict[str, Any] = {}
        for field in rng.sample(["labels", "milestone", "duplicate_of"], rng.randint(1, 3)):
            attributes[f"p.decided.{field}"] = rng.choice(values)
            attributes[f"p.expected.{field}"] = rng.choice(values)
            if rng.random() < 0.3:
                attributes[f"p.grade.{field}"] = rng.choice(["ok", "wrong"])
        first = items(dict(attributes))
        assert first == items(dict(reversed(list(attributes.items()))))
        for item in first:
            field = item["id"].split("#", 1)[1]
            assert item["signature"].split(":", 1)[0] == field
            assert attributes.get(f"p.grade.{field}", "wrong") == "wrong"


def test_a_list_decision_matching_an_accepted_answer_to_an_empty_truth_is_not_wrong() -> None:
    assert items({
        "t.decided.labels": ["bug", "ui"], "t.expected.labels": "(none)",
        "t.accepted.labels": ["bug, ui"],
    }) == []  # fmt: skip


def test_item_text_is_redacted_again_cut_and_shows_hidden_characters() -> None:
    from flight_recorder.analyst.items import TEXT_CHARS, _text  # pyright: ignore[reportPrivateUsage]
    from flight_recorder.redact import Redactor

    token = "ghp_" + "Z9y8" * 9
    text = _text(f"labels: decided {token}\u202e " + "x" * 2000, Redactor(b"k" * 32))

    assert token not in text and "\u202e" not in text and "\\u202e" in text
    assert len(text) == TEXT_CHARS


def test_a_signature_is_one_safe_line_and_different_signatures_stay_different() -> None:
    from flight_recorder.analyst.items import SIGNATURE_CHARS, safe_signature
    from flight_recorder.redact import Redactor

    redactor = Redactor(b"k" * 32)
    names = ["\u641c\u7d22", "\u7ffb\u8bd1", "a b", "a_b", "a%20b", "a\nb", "a\tb", "x" * 300,
             "x" * 299 + "y", "~redacted"]  # fmt: skip
    signatures = [safe_signature(f"error:agent:{n}:KeyError", redactor) for n in names]

    assert len(set(signatures)) == len(names)
    for sig in signatures:
        assert len(sig) <= SIGNATURE_CHARS and sig.isprintable() and "\n" not in sig
        assert sig.startswith("error:agent:")
        assert sig.endswith(":KeyError") or "~" in sig  # a long one ends in its hash
    assert signatures[0] == "error:agent:\u641c\u7d22:KeyError"  # letters stay readable


def test_a_signature_part_that_holds_a_secret_or_a_placeholder_becomes_redacted() -> None:
    from flight_recorder.analyst.items import safe_signature
    from flight_recorder.redact import Redactor

    redactor = Redactor(b"k" * 32)
    token = "ghp_" + "Z9y8" * 9

    assert safe_signature(f"error:tool:{token}:KeyError", redactor) == (
        "error:tool:~redacted:KeyError"
    )
    recorded = "error:tool:get [REDACTED:github_token:1a2b3c4d] now:KeyError"
    assert safe_signature(recorded, redactor) == "error:tool:get%20~redacted%20now:KeyError"
    issues, repo = (safe_signature(f"error:tool:fetch_{n} using {token}:KeyError", redactor)
                    for n in ("issues", "repo"))  # fmt: skip
    assert issues != repo  # the text around a secret still tells failures apart
    assert safe_signature("token:extra", redactor) == "token:extra"  # field and direction kept


def test_an_error_items_name_and_text_are_redacted_whole_before_the_signature_splits() -> None:
    """`password: hunter2xyz` spans a `:`: redacted part by part, neither part is a secret."""
    from types import SimpleNamespace

    from flight_recorder.analyst.items import error_item, safe_signature
    from flight_recorder.redact import Redactor

    redactor = Redactor(b"k" * 32)
    node = SimpleNamespace(kind="tool", name="login password: hunter2xyz", status="error",
                           error="password: hunter2xyz rejected", input=None,
                           output=None)  # fmt: skip

    item = error_item("r1", "2026-11-27T09:00:00+00:00", node, redactor)  # type: ignore[arg-type]
    signature = safe_signature(item["signature"], redactor)

    assert "hunter2xyz" not in item["signature"] and "hunter2xyz" not in signature
    assert signature.startswith("error:tool:login%20password")
