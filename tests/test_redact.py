"""Redaction patterns, placeholders and keys. No database needed."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fake_secrets import in_context, private_key, random_text, secrets_by_kind
from pydantic import SecretStr, ValidationError

from flight_recorder.redact import PATTERNS, RedactionKeyError, Redactor, load_key
from flight_recorder.settings import Settings
from flight_recorder.store.write import SpanIn

KEY = b"test-key-0123456789abcdef0123456789abcdef"
PLACEHOLDER = re.compile(r"\[REDACTED:([a-z_]+):([0-9a-f]{8})\]")


@pytest.fixture
def redactor() -> Redactor:
    return Redactor(KEY)


def redact(redactor: Redactor, text: str) -> tuple[str, Counter[str]]:
    counts: Counter[str] = Counter()
    return redactor.text(text, counts), counts


CASES = {kind: (secret, secret) for kind, secret in secrets_by_kind().items()} | in_context()


def test_every_pattern_has_a_case() -> None:
    assert {p.name for p in PATTERNS} == set(CASES)


@pytest.mark.parametrize("kind", sorted(CASES))
def test_pattern_replaces_only_the_secret(redactor: Redactor, kind: str) -> None:
    text, secret = CASES[kind]

    result, counts = redact(redactor, f"before {text} after")

    assert secret not in result
    assert result.startswith("before ") and result.endswith(" after")  # context kept
    assert [m.group(1) for m in PLACEHOLDER.finditer(result)] == [kind]
    assert counts == Counter({kind: 1})


NEAR_MISSES = {
    "git SHA": "commit 9f86d081884c7d659a2feaa0c55ad015a3bf4f1b",
    "UUID": "run 123e4567-e89b-12d3-a456-426614174000",
    "OTel trace id": "trace 4bf92f3577b34da6a3ce929d0e0e4736",
    "data URI": "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4",
    "file path": "src/flight_recorder/store/migrations/versions/0001_initial_run_store.py",
    "token counts": "max_tokens: 1000, input_tokens=3525, tokens: 1,234",
    "timestamp": "took 1731165600123 ms at 2026-11-09T12:00:00Z",
    "bare digits": "order 12345678901, issue #42",
    "model spec": "openai:gpt-5.4-mini and anthropic:claude-haiku-5-5",
    "version": "alembic 1.20.1, pgvector 0.8.6",
    "long identifier": "thisIsAVeryLongIdentifierWithoutAnyDigitsAtAllInIt",
    "prose": "The agent searched three times, then decided the issue was a duplicate.",
}


@pytest.mark.parametrize("text", NEAR_MISSES.values(), ids=NEAR_MISSES.keys())
def test_near_misses_are_left_alone(redactor: Redactor, text: str) -> None:
    assert redact(redactor, text) == (text, Counter())


def test_same_secret_same_placeholder_different_secrets_differ(redactor: Redactor) -> None:
    secret, other = secrets_by_kind()["openai_key"], secrets_by_kind()["openai_key"]

    first, _ = redact(redactor, f"step 3 got {secret}")
    second, _ = redact(redactor, f"step 7 sent {secret} to a tool")
    third, _ = redact(redactor, f"another {other}")

    placeholder = PLACEHOLDER.search(first)
    assert placeholder and placeholder.group(0) in second  # the flow between steps shows
    assert placeholder.group(0) not in third


def test_fingerprint_needs_the_key() -> None:
    secret = secrets_by_kind()["email"]
    a, b = Redactor(KEY), Redactor(KEY[::-1])

    assert a.fingerprint(secret) != b.fingerprint(secret)
    assert (
        a.fingerprint(secret) != hashlib.sha256(secret.encode()).hexdigest()[:8]
    )  # not a plain hash


def test_redacting_twice_changes_nothing(redactor: Redactor) -> None:
    once, _ = redact(redactor, " ".join(secret for secret, _ in CASES.values()))

    assert redact(redactor, once) == (once, Counter())


def test_unterminated_private_key_is_redacted_to_the_end(redactor: Redactor) -> None:
    truncated = private_key().split("-----END")[0]  # a prompt cut off mid-key

    result, counts = redact(redactor, f"key: {truncated}")

    assert result.startswith("key: [REDACTED:private_key:")
    assert counts == Counter({"private_key": 1})


def test_structured_values_are_redacted_at_any_depth(redactor: Redactor) -> None:
    secrets = secrets_by_kind()
    value = {
        "messages": [{"role": "user", "content": f"use {secrets['openai_key']}"}],
        secrets["email"]: "a secret used as a key",
        "gen_ai.input.messages": json.dumps([{"content": secrets["github_token"]}]),
        "count": 3,
        "ok": True,
        "missing": None,
    }

    result = redactor.redact(value)

    dumped = json.dumps(result.value)
    assert all(secrets[k] not in dumped for k in ("openai_key", "email", "github_token"))
    assert (result.value["count"], result.value["ok"], result.value["missing"]) == (3, True, None)
    assert result.counts == Counter({"openai_key": 1, "email": 1, "github_token": 1})


ADVERSARIAL = {
    "long word": "a" * 200_000,
    "many at signs": "a@" * 100_000,
    "many plus signs": "+1 " * 70_000,
    "token chars": "Ab1+" * 50_000,
    "begin markers": ("-----BEGIN " + "PRIVATE KEY----- ") * 5_000,
    "url-ish": "x://a:" * 40_000,
    "assignments": "token=" * 40_000,
}


@pytest.mark.parametrize("text", ADVERSARIAL.values(), ids=ADVERSARIAL.keys())
def test_matching_stays_fast_on_hostile_input(redactor: Redactor, text: str) -> None:
    """Redaction runs on untrusted agent output; a slow regex would stall ingest."""
    started = time.perf_counter()
    redact(redactor, text)

    assert time.perf_counter() - started < 2.0


@pytest.mark.parametrize("kind", ["secret_assignment", "aws_secret_key"])
def test_secrets_inside_a_json_string_are_redacted_and_it_still_parses(
    redactor: Redactor, kind: str
) -> None:
    """OTel sends messages as a JSON string, so quotes arrive escaped: \\"api_key\\": \\"...\\"."""
    text, secret = in_context()[kind]
    encoded = json.dumps([{"role": "user", "content": f"config {text} end"}])

    result, counts = redact(redactor, encoded)

    assert secret not in result
    assert counts[kind] == 1
    assert json.loads(result)[0]["content"].endswith(" end")  # still valid JSON


def test_negative_tokens_rejected_before_redaction() -> None:
    with pytest.raises(ValidationError):
        SpanIn(
            external_id="s",
            kind="llm",
            name="x",
            started_at=datetime(2026, 11, 9, tzinfo=UTC),
            input_tokens=-1,
        )


# ---- Keys ------------------------------------------------------------------------


def test_key_from_settings_wins(tmp_path: Path) -> None:
    key = random_text(64)
    settings = Settings(_env_file=None, AFR_REDACTION_KEY=SecretStr(key))  # pyright: ignore[reportCallIssue]

    assert load_key(settings, key_file=tmp_path / "unused.key") == key.encode()
    assert not (tmp_path / "unused.key").exists()


def test_short_key_is_rejected() -> None:
    settings = Settings(_env_file=None, AFR_REDACTION_KEY=SecretStr("too-short"))  # pyright: ignore[reportCallIssue]

    with pytest.raises(RedactionKeyError, match="at least 32"):
        load_key(settings)
    with pytest.raises(ValueError, match="at least 16 bytes"):
        Redactor(b"short")


def test_key_file_is_generated_once_and_reused(
    tmp_path: Path, clean_env: pytest.MonkeyPatch
) -> None:
    settings = Settings(_env_file=None)  # pyright: ignore[reportCallIssue]
    key_file = tmp_path / ".afr" / "redaction.key"

    first = load_key(settings, key_file=key_file)
    second = load_key(settings, key_file=key_file)

    assert first == second and len(first) == 64
    if sys.platform != "win32":  # Windows has no owner-only mode bits
        assert os.stat(key_file).st_mode & 0o077 == 0
