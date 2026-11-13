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


# Words after a secret's name that say there is no secret, or name a type in code
# (`password: str`), not a value.
NOT_A_VALUE = frozenset(
    {"null", "none", "nil", "undefined", "true", "false", "str", "string", "bytes", "secretstr"}
)


# Punctuation that may close such a word in code: `str):`, `None)`, `null,`, `bytes]`.
# Anything else makes it part of a value: `Str@ng123x`, `None!Shall9`, `None.x`.
_CLOSING = r"[),:|\];]*+"
_FIRST_NAME = re.compile(rf"(\w++){_CLOSING}")


def is_a_value(text: str) -> bool:
    """Whether text after a secret's name could be the secret."""
    words = text.split()
    if not words:
        return False
    first = _FIRST_NAME.fullmatch(words[0])
    return not (first and first.group(1).lower() in NOT_A_VALUE)


def _assigned_value(match: re.Match[str]) -> bool:
    """A value, and not the kind name inside a placeholder: [REDACTED:github_token:1a2b3c4d]
    ends in a secret's name followed by `:` and eight characters."""
    before = match.string[max(0, match.start() - 60) : match.start()]
    return is_a_value(match.group("secret")) and not _IN_PLACEHOLDER.search(before)


_TOKEN = r"A-Za-z0-9+/_=-"  # characters of base64 and URL-safe tokens

# Names of a secret, anywhere a key name may end in one: password, DB_PASSWORD, dbPassword,
# DBPassword, spring.datasource.password, --db-password, PASSWORD1. The name starts where a
# word starts (not after a letter or digit) or at a camelCase hump, and ends at a word
# boundary, so PASSWORD_FILE, secret_name and max_tokens are not names of a secret. `pass`
# alone is a word ("first pass: ..."), so it is a name only after a prefix: DB_PASS, dbPass.
# The lookbehinds outside the name are case-sensitive; only the name itself ignores case.
SECRET_NAME = (
    r"(?:(?<![A-Za-z0-9])|(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z]))"
    r"(?i:api[_-]?keys?+|secret[_-]?key|client[_-]?secret|private[_-]?key|secret|passwords?+"
    r"|passphrase|passwd|pwd|pgpassword|access[_-]?token|auth[_-]?token|token"
    r"|(?<=[A-Za-z0-9][_.-])pass|(?<=[a-z0-9])pass)"
    r"(?:_?+\d{1,3}+)?+\b"
)
SECRET_KEY = re.compile(SECRET_NAME + r"\W*+$")  # a key or flag that ends in a secret's name
# The value after a secret's name, of any length, in one of three forms, each a pattern of
# its own (one regex can't name the secret in each branch); quoted runs first.
_NAMED = SECRET_NAME + r"(?:\\?[\"'])?[ \t]*+"  # password, "password", \"password\"
# A type between the name and its value: `password: str = "..."`, `token: Optional[str] =`
_ANNOTATION = r"(?::[ \t]*+[A-Za-z_][\w.\[\]|, \t]*+)?"
_NEWLINE = r"(?:\r?\n|(?:\\r)?+\\n)"  # a line break, or one escaped inside a JSON string
_ASSIGN = r"(?::=|=>|=(?![=>]))"  # = (not ==), Go's :=, and Ruby's and PHP's =>
# A value's word: not starting with = (`token == x` is code). It stops at whitespace, a
# quote or a backslash; a , ; { or } is part of it only when more of the word follows, as in
# generated passwords (Tr0ub;&4dor{x}), not where it ends a value: `{password: x, user: y}`.
_PLAIN = r"[^\s\"'\\,;{}]"
_WORD = rf"[^\s\"'\\,;{{}}=](?:{_PLAIN}|[,;{{}}](?={_PLAIN}))*+"
# What the words after `name:` must not start with, or the match would be skipped whole and
# hide a secret later on the line: a placeholder, the inside of one ([REDACTED:github_token:
# 1a2b3c4d] holds `token:1a2b3c4d]`), or a word that says there is no value (as is_a_value
# reads it). Checked in the regex, so the search goes on from the next position instead.
_NOT_A_VALUE_WORD = "|".join(sorted(NOT_A_VALUE))
_SKIPPED = rf"\[REDACTED:|[0-9a-f]{{8}}\]|(?i:{_NOT_A_VALUE_WORD}){_CLOSING}(?!\S)"
SECRET_VALUES = (
    # password: "two words" / api_key='...' / \"token\": \"...\" (a JSON string): to the
    # closing quote or the end of the line; the quotes stay. Inside plain quotes an escape
    # (\\ or \") is part of the value; inside escaped quotes (JSON in a JSON string) the
    # value stops at the next backslash, so the closing \" stays and the text still parses.
    _NAMED + _ANNOTATION + rf"(?:{_ASSIGN}|:)[ \t]*+(?P<esc>\\)?(?P<q>[\"'])(?P<secret>"
    r"(?(esc)(?:(?!(?P=q))[^\\\n])++|(?:\\.|(?!(?P=q))[^\\\n])++))",
    # password: correct horse battery staple (YAML, prose): the words to the end of the line,
    # or of the next line when it is indented under the name (`password:\n  hunter2`)
    _NAMED
    + rf":[ \t]*+(?:{_NEWLINE}[ \t]++)?(?!{_SKIPPED})(?P<secret>{_WORD}(?:[ \t]++{_WORD})*+)",
    # PGPASSWORD=abc123 psql (shell, .env): one word
    _NAMED + _ANNOTATION + rf"{_ASSIGN}[ \t]*+(?P<secret>{_WORD})",
)


def _secret_flag(match: re.Match[str]) -> bool:
    """--password VALUE, --db-password VALUE: a flag named for a secret, then its value."""
    return bool(SECRET_KEY.search(match.group("flag"))) and is_a_value(match.group("secret"))


def _secret_pair(match: re.Match[str]) -> bool:
    """{name: DB_PASSWORD, value: ...}: a name field naming a secret, then its value."""
    return bool(SECRET_KEY.search(match.group("name"))) and _assigned_value(match)


_Q = r"\\?[\"']"  # a quote, or an escaped one inside a JSON string
SECRET_ELSEWHERE = (
    # docker login --password VALUE: a flag, a space and its value, unless that is another
    # flag or a variable (--password $DB_PASSWORD). A quoted value goes whole, quotes too.
    r"(?<![\w-])--(?P<flag>[A-Za-z0-9][A-Za-z0-9_.-]*+)[ \t]++"
    r"(?P<secret>\"[^\"\n]*+\"|'[^'\n]*+'|[^\s\"'\\$-]\S*+)",
    # mysql -uroot -pVALUE: the password joined to -p. -p alone asks for it. Up to 30
    # arguments are looked at, so a line of `mysql` words stays linear.
    r"(?<![\w-])mysql(?:dump|admin|sh)?+(?:[ \t]++(?!-p)\S++){0,30}+[ \t]++-p(?P<secret>\S++)",
    # Kubernetes and ECS env entries: name: DB_PASSWORD, then value: ... on the same or the
    # next line, as YAML or JSON (escaped too, inside a JSON string).
    rf"(?<![\w-])(?:{_Q})?+(?:name|key)(?:{_Q})?+:[ \t]*+(?:{_Q})?+(?P<name>[A-Za-z0-9_.-]++)"
    rf"(?:{_Q})?+[ \t]*+,?+[ \t]*+(?:{_NEWLINE}[ \t]*+)?+(?:{_Q})?+value(?:{_Q})?+:[ \t]*+"
    rf"(?:{_Q})?+(?P<secret>[^\s\"'\\,}}][^\"'\\\n,}}]*+)",
)

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
    # password=..., "api_key": "...", and the same inside a JSON string (\"api_key\": \"...\"),
    # which is how OTel sends gen_ai.input.messages. The backslash stays outside the secret so
    # the JSON string remains valid after replacement. Any length: password=hunter2 too.
    *(
        Pattern("secret_assignment", re.compile(value), check=_assigned_value)
        for value in SECRET_VALUES
    ),
    *(
        Pattern("secret_assignment", re.compile(regex), check=check)
        for regex, check in zip(
            SECRET_ELSEWHERE, (_secret_flag, _assigned_value, _secret_pair), strict=True
        )
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
