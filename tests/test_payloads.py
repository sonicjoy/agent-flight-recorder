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
