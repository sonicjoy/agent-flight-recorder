"""`safe`: what the MCP server hands a model is cleaned, capped and plain JSON (#24)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from flight_recorder.safe_text import KEY_CHARS, clean, safe

HIDDEN = "\x1b[2J ‮ ​ \U000e0041"


def test_hidden_characters_in_values_and_keys_come_back_escaped() -> None:
    out = safe({HIDDEN: [HIDDEN, {"nested": HIDDEN}]})

    [(key, value)] = out.items()
    for text in (key, value[0], value[1]["nested"]):
        assert text == clean(HIDDEN)
        assert "\\x1b" in text and "\\u202e" in text and "\\u200b" in text
        assert "\x1b" not in text and "‮" not in text


def test_the_cap_holds_after_escapes_lengthen_the_text() -> None:
    out = safe("\x1b" * 50, 20)  # 50 escapes would be 200 characters

    assert len(out) == 20 and out.endswith("…")


def test_keys_have_their_own_cap() -> None:
    (key,) = safe({"k" * 500: 1})
    assert len(key) == KEY_CHARS


def test_fingerprints_are_removed() -> None:
    assert safe("key [REDACTED:api_key:0a1b2c3d] used") == "key [REDACTED:api_key] used"


def test_deeply_nested_values_are_cleaned_throughout() -> None:
    value: Any = "\x1b"
    for i in range(200):
        value = {"k": value} if i % 2 else [value]

    out = safe(value)
    for i in range(200):
        out = out["k"] if (199 - i) % 2 else out[0]
    assert out == "\\x1b"


def test_running_it_twice_changes_nothing() -> None:
    value = {
        HIDDEN: ["x" * 5_000, "[REDACTED:email:0a1b2c3d]", "…already cut", "\\x1b literal"],
        "n": 3,
    }
    once = safe(value)
    assert safe(once) == once
    assert safe(safe(value, 10), 10) == safe(value, 10)


def test_other_values_become_json_ready() -> None:
    at = datetime(2026, 11, 18, 9, 30, tzinfo=UTC)
    run = uuid.uuid4()

    out = safe({"id": run, "at": at, "cost": Decimal("0.01"), "n": 2, "ok": True, "none": None})

    assert out == {
        "id": str(run),
        "at": "2026-11-18T09:30:00+00:00",
        "cost": "0.01",
        "n": 2,
        "ok": True,
        "none": None,
    }
    assert safe(("a", "b")) == ["a", "b"]


def test_a_placeholder_cut_inside_its_fingerprint_loses_what_is_left_of_it() -> None:
    assert safe("mail [REDACTED:email:0d8b") == "mail [REDACTED:email"
    assert safe("mail [REDACTED:email:") == "mail [REDACTED:email"
    assert safe("[REDACTED:email:0a1b2c3d] and [REDACTED:api_key:0a") == (
        "[REDACTED:email] and [REDACTED:api_key"
    )


def test_cleaning_only_the_shown_part_gives_the_same_text() -> None:
    import random

    from flight_recorder.payloads import cut
    from flight_recorder.safe_text import strip_text

    rng = random.Random(7)
    pool = [
        "\x1b",
        "\r",
        "\n",
        "\r\n",
        "\ufe0f",
        "\u202e",
        "a",
        "\xe9",
        "\U0001f600",
        "[REDACTED:k:0a1b2c3d]",
    ]
    for _ in range(20_000):
        text = "".join(rng.choice(pool) for _ in range(rng.randrange(0, 80)))
        limit = rng.randrange(2, 30)
        assert safe(text, limit) == cut(clean(strip_text(text)), limit), (text, limit)
