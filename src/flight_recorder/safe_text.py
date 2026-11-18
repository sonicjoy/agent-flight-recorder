"""Recorded text made safe to show to a person or hand to a model.

Everything recorded is untrusted text: a tool's output or a fetched page can hold terminal
escape codes (clear the screen, set the window title), right-to-left overrides that disguise
text, or invisible characters that hide words from a reader but not from a model. `clean`
shows those as visible escapes. It doesn't make text safe to follow: recorded text can still
say anything (docs/cli.md).

`safe` is the boundary for data that leaves afr as a whole value (the MCP server's results):
fingerprints removed, hidden characters escaped, every string capped. Pure: no CLI, MCP or
database imports.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable
from datetime import date, datetime
from typing import Any, cast

from flight_recorder.payloads import cut
from flight_recorder.redact.redactor import FINGERPRINT_CHARS

KEPT = frozenset("\n\t")
HIDDEN = frozenset(("Cc", "Cf", "Zl", "Zp", "Co", "Cn"))
"""Control and format characters (bidi overrides, zero-width, tags), line and paragraph
separators, private-use and unassigned code points: shown as escapes."""
FILLERS = frozenset(chr(c) for c in (0x115F, 0x1160, 0x3164, 0xFFA0))
"""Hangul fillers: letters that render blank."""
PLACEHOLDER = re.compile(
    rf"\[REDACTED:(?P<kind>[a-z0-9_]+)(?::(?P<fp>[0-9a-f]{{{FINGERPRINT_CHARS}}}))?\]"
)
SAFE_CHARS = 4_000  # the backstop cap for a string with no cap of its own
KEY_CHARS = 200


def _selector(char: str) -> bool:
    code = ord(char)
    return 0xFE00 <= code <= 0xFE0F or 0xE0100 <= code <= 0xE01EF


def _hidden(value: str, i: int) -> bool:
    """Whether the character at `i` would hide something. A variation selector is fine
    once after an ordinary character (that's how emoji pick their style); a run of them,
    or a supplementary one, can carry hidden bytes."""
    char = value[i]
    if char in KEPT:
        return False
    if _selector(char):
        return ord(char) > 0xFFFF or i == 0 or _selector(value[i - 1])
    return char in FILLERS or unicodedata.category(char) in HIDDEN


def _escape(char: str) -> str:
    code = ord(char)
    if code < 0x100:
        return f"\\x{code:02x}"
    return f"\\u{code:04x}" if code < 0x10000 else f"\\U{code:08x}"


def clean(value: str) -> str:
    """Recorded text made safe to print: control and invisible characters (but newlines
    and tabs) become visible escapes such as `\\x1b`, `\\u202e` and `\\U000e0041`."""
    value = value.replace("\r\n", "\n")
    return "".join(_escape(c) if _hidden(value, i) else c for i, c in enumerate(value))


def map_strings(
    value: Any,
    change: Callable[[str], str],
    *,
    key: Callable[[str], str] | None = None,
    leaf: Callable[[Any], Any] | None = None,
) -> Any:
    """`value` with `change` applied to every string at any depth, `key` (default `change`)
    to every string dict key, and `leaf` (default: unchanged) to every other value that
    isn't a dict, list or tuple. Tuples come back as lists. Recursive, like the redactor's
    walk that every stored value has already been through."""
    on_key = key or change
    if isinstance(value, str):
        return change(value)
    if isinstance(value, dict):
        items = cast("dict[Any, Any]", value)
        return {
            (on_key(k) if isinstance(k, str) else map_strings(k, change, key=key, leaf=leaf)):
            map_strings(v, change, key=key, leaf=leaf)
            for k, v in items.items()
        }  # fmt: skip
    if isinstance(value, list | tuple):
        return [map_strings(v, change, key=key, leaf=leaf) for v in cast("list[Any]", value)]
    return leaf(value) if leaf else value


CUT_PLACEHOLDER = re.compile(r"\[REDACTED:(?P<kind>[a-z0-9_]+):[0-9a-f]*\Z")
"""A placeholder cut inside its fingerprint at the very end of a text: a run name is cut to
80 characters after the hook redacted it. Not a field the hook cut at 32 KB: that cut ends in
`...[truncated N chars]`, so its fingerprint stays (MCP results are capped far before it)."""


def strip_text(text: str) -> str:
    """`text` with each placeholder's fingerprint removed (`[REDACTED:kind]`), including a
    placeholder cut short at the end."""
    text = PLACEHOLDER.sub(lambda m: f"[REDACTED:{m['kind']}]", text)
    return CUT_PLACEHOLDER.sub(lambda m: f"[REDACTED:{m['kind']}", text)


def strip_fingerprints(value: Any) -> Any:
    """`value` with each placeholder's fingerprint removed (`[REDACTED:kind]`): the
    fingerprint ties a value to one person's redaction key. Nothing else changes."""
    return map_strings(value, strip_text)


def _json_leaf(value: Any) -> Any:
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, datetime | date):
        return value.isoformat()
    return str(value)  # UUID, Decimal, anything else


def safe(value: Any, limit: int = SAFE_CHARS) -> Any:
    """`value` as JSON-ready data a model can be shown: every string has its fingerprints
    removed, hidden characters escaped, then is cut to `limit` (keys to KEY_CHARS). Cleaning
    comes before cutting, so the cap holds after escapes. Running it twice changes nothing."""
    return map_strings(
        value,
        lambda s: _capped(s, limit),
        key=lambda k: _capped(k, KEY_CHARS),
        leaf=_json_leaf,
    )


def _capped(text: str, limit: int) -> str:
    """`cut(clean(strip_text(text)), limit)`, cleaning only what can show. Cleaning maps
    each character to itself or a longer escape, except `\\r\\n`, which becomes one `\\n`; so
    2 * limit + 2 raw characters clean to more than `limit`, and the 2 after them cover a
    pair split at the edge."""
    return cut(clean(strip_text(text)[: 2 * limit + 4]), limit)
