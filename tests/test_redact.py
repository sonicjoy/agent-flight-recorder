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
from fake_secrets import (
    in_context,
    in_dicts,
    prefixed_assignments,
    private_key,
    random_text,
    secrets_by_kind,
)
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
    "port and scoped package": "npm install from http://localhost:4873/@types/node",
    "URL with no password": "https://github.com/sonicjoy/agent-flight-recorder/pull/43",
    "names that only start with a keyword": "PASSWORD_FILE=/run/secrets/db_password_file "
    "secret_name=prod-database-credentials tokenizer=cl100k_base_tokenizer",
    "counts named like a keyword": "num_tokens=12345678 maxTokens=40960000",
}


@pytest.mark.parametrize(
    ("url", "password"),
    [
        ("postgresql://afr:ab/cdEF12xyz@db:5432/afr", "ab/cdEF12xyz"),
        ("postgresql://afr:hunter2#x@db/afr", "hunter2#x"),
        ("postgresql://afr:p@ssw0rd99@db:5432/afr", "p@ssw0rd99"),
        ("redis://default:a?b/c@d#e@cache.internal:6379/0", "a?b/c@d#e"),
        ("mysql://root:pw@127.0.0.1", "pw"),
    ],
)
def test_unencoded_characters_in_a_connection_password_are_redacted(
    redactor: Redactor, url: str, password: str
) -> None:
    """Hand-written URLs often skip percent-encoding; the whole password still goes."""
    result, counts = redact(redactor, f"DATABASE_URL={url} end")

    redacted_url = url.replace(
        f":{password}@", f":{redactor.placeholder('connection_password', password)}@"
    )
    assert result == f"DATABASE_URL={redacted_url} end"
    assert counts == Counter({"connection_password": 1})


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


@pytest.mark.parametrize("label", sorted(prefixed_assignments()))
def test_a_key_name_with_a_prefix_is_still_a_key_name(redactor: Redactor, label: str) -> None:
    """DB_PASSWORD=, JWT_SECRET=, dbPassword=: how .env files and code name secrets."""
    text, secret = prefixed_assignments()[label]

    result, counts = redact(redactor, f"before {text}\nafter")  # `key: value` runs to the end

    assert secret not in result
    assert result.startswith("before ") and result.endswith("\nafter")
    assert counts == Counter({"secret_assignment": 1})


@pytest.mark.parametrize("key", sorted(in_dicts()))
def test_a_value_under_a_secret_key_is_redacted_whole(redactor: Redactor, key: str) -> None:
    """Tool inputs and outputs are dicts: the key that names the secret isn't in the value's
    string, so no pattern sees `password=` and the value has no shape of its own."""
    value, secret = in_dicts()[key]

    result = redactor.redact({"tool_input": value, "list": [value]})

    dumped = json.dumps(result.value)
    assert secret not in dumped
    assert result.value["tool_input"]["user"] == "app"  # other values kept
    assert result.counts == Counter({"secret_assignment": 2})
    assert redactor.redact(result.value).counts == Counter()  # idempotent


def test_values_under_keys_that_only_look_like_secrets_are_kept(redactor: Redactor) -> None:
    value = {
        "max_tokens": 4096,
        "input_tokens": "12345678",
        "token_count": "12345678",
        "secret_name": "prod-database-credentials",
        "password": "",
        "auth_token": "null",  # says there is none
        "token": None,
        "api_key": 12345678901,  # not a string
    }

    assert redactor.redact(value).value == value


# (text, the value that must go): short values, and values with spaces, after a secret's name
SHORT_OR_SPACED = {
    "short after =": ("password=hunter2", "hunter2"),
    "short env var before a command": ("PGPASSWORD=abc123 psql -h db", "abc123"),
    "phrase after a colon": ("password: correct horse battery", "correct horse battery"),
    "phrase in double quotes": ('"password": "correct horse battery"', "correct horse battery"),
    "phrase in single quotes": ("api_key = 'my key here'", "my key here"),
    "phrase after = in quotes": ('DB_PASSWORD="two words"', "two words"),
    "short in a JSON string": ('\\"token\\": \\"ab12\\"', "ab12"),
}  # fmt: skip


@pytest.mark.parametrize("label", SHORT_OR_SPACED)
def test_short_and_spaced_values_after_a_secrets_name_are_redacted(
    redactor: Redactor, label: str
) -> None:
    text, secret = SHORT_OR_SPACED[label]

    result, counts = redact(redactor, f"{text}\nnext line")

    expected = text.replace(secret, redactor.placeholder("secret_assignment", secret), 1)
    assert result == f"{expected}\nnext line"
    assert counts == Counter({"secret_assignment": 1})


@pytest.mark.parametrize(
    "text", ["password: null", "token = none", '"password": ""', "password=", "TOKEN=false"]
)
def test_a_secrets_name_with_no_value_is_left_alone(redactor: Redactor, text: str) -> None:
    assert redact(redactor, text) == (text, Counter())


def test_a_short_value_under_a_secret_key_is_redacted(redactor: Redactor) -> None:
    result = redactor.redact({"password": "hunter2", "token": "two words"})

    assert result.value == {
        "password": redactor.placeholder("secret_assignment", "hunter2"),
        "token": redactor.placeholder("secret_assignment", "two words"),
    }


GITHUB_TOKEN = "ghp_" + "a1B2c3D4" * 5
# A secret's name whose value is skipped (a placeholder, a type, "none"), then a real one
# later on the same line: the skipped text must not hide the secret after it.
AFTER_A_SKIPPED_VALUE = {
    "after a token another pattern replaced": f"github token: {GITHUB_TOKEN} password: Hunter22xyz",
    "after a placeholder": "token: [REDACTED:x:12345678] password: Hunter22xyz",
    "after a type name": "password: str  # set later: password: Hunter22xyz",
    "after none": "password: none here, then password: Hunter22xyz",
}  # fmt: skip


@pytest.mark.parametrize("text", AFTER_A_SKIPPED_VALUE.values(), ids=AFTER_A_SKIPPED_VALUE.keys())
def test_a_secret_after_a_skipped_value_on_the_same_line_is_redacted(
    redactor: Redactor, text: str
) -> None:
    result, _ = redact(redactor, text)

    assert "Hunter22xyz" not in result
    assert result.endswith("password: " + redactor.placeholder("secret_assignment", "Hunter22xyz"))


# (text, the value that must go): quoted values with escapes, and long ones
QUOTED = {
    "an escaped backslash in JSON": ('{"password": "Abcdefgh1\\\\xyz"}', "Abcdefgh1\\\\xyz"),
    "an escaped quote in a JSON string": ('{"password": "Abc\\"defgh1"}', 'Abc\\"defgh1'),
    "longer than 256 characters": ('token = "' + "x" * 300 + '"', "x" * 300),
    "a long secret with dots": (json.dumps({"client_secret": "Ab1." * 70}), "Ab1." * 70),
}  # fmt: skip


@pytest.mark.parametrize("label", QUOTED)
def test_a_quoted_value_with_escapes_or_over_256_characters_is_redacted_whole(
    redactor: Redactor, label: str
) -> None:
    text, secret = QUOTED[label]

    result, counts = redact(redactor, text)

    assert result == text.replace(secret, redactor.placeholder("secret_assignment", secret), 1)
    assert counts == Counter({"secret_assignment": 1})


ADVERSARIAL = {
    "long word": "a" * 200_000,
    "many at signs": "a@" * 100_000,
    "many plus signs": "+1 " * 70_000,
    "token chars": "Ab1+" * 50_000,
    "begin markers": ("-----BEGIN " + "PRIVATE KEY----- ") * 5_000,
    "url-ish": "x://a:" * 40_000,
    "url-ish with at signs": "x://a:b@" * 30_000,
    "url then a long password": "x://a:" + "b" * 200_000,
    "assignments": "token=" * 40_000,
    "prefixed assignments": "DB_PASSWORD=" * 30_000,
    "long prefix": "a_" * 100_000 + "password=x",
    "camelCase keywords": "aPassword" * 40_000,
    "keyword then spaces": "password" + " " * 200_000,
    "a long phrase after a colon": "password: " + "word " * 100_000,
    "an unclosed quote": 'password: "' + "word " * 100_000,
    "many phrases": "token: a b " * 40_000,
    "many skipped values": "password: none " * 40_000,
    "many placeholders after a name": "token: [REDACTED:github_token:12345678] " * 15_000,
    "escapes in an unclosed quote": 'password="' + "\\a" * 100_000,
    "many unclosed quotes": 'token="' * 40_000,
}


@pytest.mark.parametrize("text", ADVERSARIAL.values(), ids=ADVERSARIAL.keys())
def test_matching_stays_fast_on_hostile_input(redactor: Redactor, text: str) -> None:
    """Redaction runs on untrusted agent output; a slow regex would stall ingest."""
    started = time.perf_counter()
    redact(redactor, text)

    assert time.perf_counter() - started < 2.0


def test_dict_keys_stay_fast_on_hostile_input(redactor: Redactor) -> None:
    value = {"a_" * 100_000 + "token": "x" * 10, "aPassword" * 40_000: "y" * 10}

    started = time.perf_counter()
    redactor.redact(value)

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
