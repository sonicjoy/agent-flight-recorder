"""Replace sensitive values with typed, consistent placeholders.

    sk-proj-abc...  ->  [REDACTED:openai_key:1a2b3c4d]

The fingerprint is a truncated HMAC-SHA256 of the value under a key that never leaves the
installation (see key.py). The same value always gets the same placeholder, so a replay
shows a secret moving from one step to another, but the value can't be recovered, and
without the key nobody can check a guess against a fingerprint.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from flight_recorder.redact.patterns import (
    PATTERNS,
    PLACEHOLDER_PREFIX,
    SECRET_KEY,
    Pattern,
    is_a_value,
)

FINGERPRINT_CHARS = 8  # 32 bits: collisions are unlikely among one install's secrets
KEY_PATTERN = "secret_assignment"  # a value under a secret's key: the same kind of secret
NAME_FIELDS = ("name", "key")  # {"name": "DB_PASSWORD", "value": ...}: Kubernetes, ECS
TOKEN_COUNT = re.compile(r"(?i)tokens?+(?:_?+\d{1,3}+)?+\W*+$")  # a key ending in token


@dataclass
class Redaction:
    """A redacted value and how many matches each pattern replaced."""

    value: Any
    counts: Counter[str] = field(default_factory=Counter[str])


class Redactor:
    def __init__(self, key: bytes, patterns: tuple[Pattern, ...] = PATTERNS) -> None:
        if len(key) < 16:
            raise ValueError("redaction key must be at least 16 bytes")
        self._key = key
        self._patterns = patterns

    def fingerprint(self, secret: str) -> str:
        digest = hmac.new(self._key, secret.encode("utf-8"), hashlib.sha256).hexdigest()
        return digest[:FINGERPRINT_CHARS]

    def placeholder(self, kind: str, secret: str) -> str:
        return f"{PLACEHOLDER_PREFIX}{kind}:{self.fingerprint(secret)}]"

    def text(self, text: str, counts: Counter[str]) -> str:
        for pattern in self._patterns:
            text = pattern.regex.sub(lambda m, p=pattern: self._replace(p, m, counts), text)
        return text

    def _replace(self, pattern: Pattern, match: re.Match[str], counts: Counter[str]) -> str:
        whole = match.group(0)
        group = "secret" if "secret" in pattern.regex.groupindex else 0
        secret = match.group(group)
        if secret.startswith(PLACEHOLDER_PREFIX) or (pattern.check and not pattern.check(match)):
            return whole
        counts[pattern.name] += 1
        start, end = match.start(group) - match.start(), match.end(group) - match.start()
        return whole[:start] + self.placeholder(pattern.name, secret) + whole[end:]

    def value(self, value: Any, counts: Counter[str]) -> Any:
        """Redact every string at any depth, dict keys included. Other values pass through.

        A string or number under a key that names a secret ({"password": "..."},
        {"DB_PASSWORD": 1234}), or in a list there, is replaced whole: the key isn't in the
        value's text, so no pattern would see it. So is the value of an entry whose name
        field names a secret ({"name": "DB_PASSWORD", "value": "..."}).
        """
        if isinstance(value, str):
            return self.text(value, counts)
        if isinstance(value, dict):
            items: dict[Any, Any] = value  # pyright: ignore[reportUnknownVariableType]
            named = next((items[f] for f in NAME_FIELDS if isinstance(items.get(f), str)), None)
            return {
                self.value(k, counts): self._under(named if k == "value" else k, v, counts)
                for k, v in items.items()
            }
        if isinstance(value, list | tuple):
            sequence: list[Any] | tuple[Any, ...] = value  # pyright: ignore[reportUnknownVariableType]
            return [self.value(v, counts) for v in sequence]
        return value

    def _under(self, key: Any, value: Any, counts: Counter[str]) -> Any:
        if not (isinstance(key, str) and SECRET_KEY.search(key)):
            return self.value(value, counts)
        if isinstance(value, list | tuple):
            sequence: list[Any] | tuple[Any, ...] = value  # pyright: ignore[reportUnknownVariableType]
            return [self._under(key, v, counts) for v in sequence]
        if isinstance(value, int | float) and not isinstance(value, bool):  # a PIN, a number
            if TOKEN_COUNT.search(key):  # max_token: 4096, token_1: 5: a count or an id
                return value
            value = str(value)
        if (
            isinstance(value, str)
            and is_a_value(value)
            and not value.startswith(PLACEHOLDER_PREFIX)
        ):
            counts[KEY_PATTERN] += 1
            return self.placeholder(KEY_PATTERN, value)
        return self.value(value, counts)

    def redact(self, value: Any) -> Redaction:
        counts: Counter[str] = Counter()
        return Redaction(self.value(value, counts), counts)
