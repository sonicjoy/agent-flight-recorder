"""`recorded_text`: a structured recorded value as one text, newlines kept (#24)."""

from __future__ import annotations

from flight_recorder.payloads import recorded_text


def test_strings_are_joined_by_lines_and_keep_their_own_newlines() -> None:
    value = {
        "messages": [
            {"role": "user", "content": "Traceback:\n  File x\nValueError"},
            {"role": "assistant", "content": "   "},
        ],
        "n": 3,
    }

    assert recorded_text(value) == "user\nTraceback:\n  File x\nValueError\nassistant"


def test_plain_text_and_nothing() -> None:
    assert recorded_text("one\ntwo") == "one\ntwo"
    assert recorded_text(None) == ""
    assert recorded_text({"n": 1}) == ""


def test_none_as_null_decodes_only_the_recorders_nothing() -> None:
    from flight_recorder.payloads import none_as_null

    assert none_as_null("(none)") is None
    assert [none_as_null(v) for v in ("", "none", ["(none)"], 0)] == ["", "none", ["(none)"], 0]


def test_decided_and_expected_pair_by_prefix_and_field() -> None:
    from flight_recorder.payloads import decided_expected_pairs

    pairs = decided_expected_pairs({
        "t.decided.labels": ["a"], "t.expected.labels": ["a"],
        "t.decided.lonely": "x",  # no truth: not a pair
        "u.expected.only": "y",  # no decision: not a pair
    })  # fmt: skip

    assert pairs == [("t", "labels", "t.decided.labels", "t.expected.labels")]
