"""What counts as sensitive: one regex per kind of secret or personal detail.

Each pattern replaces either the whole match or, when the regex has a group named `secret`,
just that group (a connection string keeps its host; `password=` keeps its key).

Every regex starts only at a token boundary (a lookbehind or `\\b`) and uses possessive
quantifiers where a run could otherwise backtrack, so matching stays linear even on
adversarial input: redaction runs on untrusted agent output.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass

PLACEHOLDER_PREFIX = "[REDACTED:"


@dataclass(frozen=True)
class Pattern:
    name: str
    regex: re.Pattern[str]
    check: Callable[[re.Match[str]], bool] | None = None
    """Extra test on a match; return False to leave it alone."""


def _digits_between(low: int, high: int) -> Callable[[re.Match[str]], bool]:
    def check(match: re.Match[str]) -> bool:
        return low <= sum(c.isdigit() for c in match.group(0)) <= high

    return check


def _entropy(text: str) -> float:
    counts = Counter(text)
    return -sum(n / len(text) * math.log2(n / len(text)) for n in counts.values())


_HEX_OR_UUID = re.compile(r"[0-9a-fA-F-]+")
_LOWERCASE_PATH = re.compile(r"[a-z0-9_.-]+(?:/[a-z0-9_.-]+)+/?")


def _looks_random(match: re.Match[str]) -> bool:
    """A long token that looks like a key, not a hash, id, path or data URI."""
    token = match.group(0)
    if _HEX_OR_UUID.fullmatch(token):  # git SHAs, trace ids, UUIDs: ids, not secrets
        return False
    if _LOWERCASE_PATH.fullmatch(token):  # src/flight_recorder/store/...
        return False
    if match.string[max(0, match.start() - 7) : match.start()] == "base64,":  # data: URIs
        return False
    classes = sum(
        [
            any(c.isdigit() for c in token),
            any(c.islower() for c in token),
            any(c.isupper() for c in token),
        ]
    )
    return classes == 3 and _entropy(token) >= 4.0


_IN_PLACEHOLDER = re.compile(r"\[REDACTED:[a-z_]{0,40}+$")


def _not_a_placeholder(match: re.Match[str]) -> bool:
    """Not the kind name inside a placeholder: [REDACTED:github_token:1a2b3c4d] ends in a
    secret's name followed by `:` and eight characters."""
    before = match.string[max(0, match.start() - 60) : match.start()]
    return not _IN_PLACEHOLDER.search(before)


_TOKEN = r"A-Za-z0-9+/_=-"  # characters of base64 and URL-safe tokens

# Names of a secret, anywhere a key name may end in one: password, DB_PASSWORD, dbPassword,
# spring.datasource.password, --db-password. The name starts where a word starts (not after
# a letter or digit) or at a camelCase hump, and ends at a word boundary, so PASSWORD_FILE,
# secret_name and max_tokens are not names of a secret. The lookbehinds are case-sensitive;
# only the name itself ignores case.
SECRET_NAME = (
    r"(?:(?<![A-Za-z0-9])|(?<=[a-z0-9])(?=[A-Z]))"
    r"(?i:api[_-]?key|secret[_-]?key|client[_-]?secret|secret|password|passwd|pwd"
    r"|access[_-]?token|auth[_-]?token|token)\b"
)
MIN_SECRET_CHARS = 8  # shorter values after a secret's name are left alone (see docs)

PATTERNS: tuple[Pattern, ...] = (
    # Unterminated keys (a truncated prompt) are redacted to the end of the text.
    Pattern(
        "private_key",
        re.compile(
            r"-----BEGIN[A-Z ]{0,30}PRIVATE KEY(?: BLOCK)?-----[\s\S]*?"
            r"(?:-----END[A-Z ]{0,30}PRIVATE KEY(?: BLOCK)?-----|\Z)"
        ),
    ),
    Pattern("anthropic_key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}+")),
    Pattern("openai_key", re.compile(r"\bsk-(?:proj-|svcacct-|admin-)?[A-Za-z0-9_-]{20,}+")),
    Pattern(
        "github_token",
        re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,255}+|github_pat_[A-Za-z0-9_]{22,255}+)"),
    ),
    Pattern("aws_access_key", re.compile(r"\b(?:AKIA|ASIA|ABIA|ACCA)[A-Z0-9]{16}\b")),
    Pattern(
        "aws_secret_key",
        re.compile(
            r"(?i)\baws.{0,20}?secret.{0,20}?[=:]\s*(?:\\?[\"'])?"
            r"(?P<secret>[A-Za-z0-9/+]{40})(?![A-Za-z0-9/+])"
        ),
    ),
    Pattern("gcp_api_key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}(?![0-9A-Za-z_-])")),
    Pattern("gcp_oauth_token", re.compile(r"\bya29\.[0-9A-Za-z_-]{20,}+")),
    Pattern(
        "jwt",
        re.compile(r"\beyJ[A-Za-z0-9_-]{8,}+\.eyJ[A-Za-z0-9_-]{8,}+\.[A-Za-z0-9_-]{8,}+"),
    ),
    Pattern(
        "bearer_token",
        re.compile(r"(?i)\b(?:bearer|basic|token)\s++(?P<secret>[A-Za-z0-9._~+/-]{16,}+=*)"),
    ),
    Pattern(
        "connection_password",  # scheme://user:PASSWORD@host keeps the scheme, user and host
        # Starts only where a token starts, and the scheme is short: `\b` alone matched at
        # every "+" in "Ab1+Ab1+...", each scan running to the end (quadratic).
        # Hand-written passwords often hold an unencoded / # ? or @, so the password runs to
        # the last @ that is followed by a host. It is capped at 128 characters so a failed
        # match can't scan to the end of the text. A port and path (localhost:4873/@types)
        # is not a password.
        re.compile(
            r"(?<![a-zA-Z0-9+.-])[a-zA-Z][a-zA-Z0-9+.-]{0,30}+://"
            r"[^\s:/?#@]++:(?!\d{1,5}+(?:[/?#]|$))"
            r"(?P<secret>[^\s\"'\\]{1,128})@(?=[^\s@/?#\"'\\])"
        ),
    ),
    Pattern(
        "secret_assignment",  # password=..., "api_key": "...", and the same inside a JSON string
        # (\"api_key\": \"...\"), which is how OTel sends gen_ai.input.messages. The backslash
        # stays outside the secret so the JSON string remains valid after replacement.
        re.compile(
            SECRET_NAME + r"(?:\\?[\"'])?\s*[:=]\s*(?:\\?[\"'])?"
            rf"(?P<secret>[^\s\"'\\,;{{}}]{{{MIN_SECRET_CHARS},}}+)"
        ),
        check=_not_a_placeholder,  # a kind name such as github_token ends in "token"
    ),
    Pattern(
        "email",
        re.compile(
            r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]++@[A-Za-z0-9-]++(?:\.[A-Za-z0-9-]++)*\.[A-Za-z]{2,}+\b"
        ),
    ),
    # International format (+44 20 7946 0958) or (555) 123-4567 only; bare digit runs are ids.
    Pattern(
        "phone",
        re.compile(r"(?<![\w+])(?:\+[1-9][\d\s().-]{6,20}\d|\(\d{3}\)\s?\d{3}[\s.-]\d{4})(?![\w])"),
        check=_digits_between(8, 15),
    ),
    Pattern(
        "high_entropy",
        re.compile(rf"(?<![{_TOKEN}])[{_TOKEN}]{{32,}}+(?![{_TOKEN}])"),
        check=_looks_random,
    ),
)
