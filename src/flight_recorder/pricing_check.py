"""Check pricing.yaml's Claude rates against Anthropic's pricing page, and update them.

The page is read as Markdown (the docs serve it at the page's URL plus ".md"). Two of its
tables are used: "Model pricing" (input, cache writes, cache hits, output; Haiku 5.5 has a
row per prompt-length tier) and "Fast mode pricing" (input and output only). Fast-mode cache
rates are not listed: the page says cache multipliers apply on top of fast mode, so they are
derived from the model's own cache multipliers.

Only models already in pricing.yaml are compared. Nothing is added or removed: a model or
tier the page lacks, or one pricing.yaml lacks, is reported as a note for a person to act on.
"""

from __future__ import annotations

import re
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import yaml

PRICING_URL = "https://platform.claude.com/docs/en/about-claude/pricing"
MARKDOWN_URL = PRICING_URL + ".md"
PROVIDER = "anthropic:"
RATE_FIELDS = ("input", "output", "cache_read", "cache_write_5m", "cache_write_1h")

# "Model pricing" column headers → pricing.yaml fields
MODEL_COLUMNS = {
    "base input tokens": "input",
    "5m cache writes": "cache_write_5m",
    "1h cache writes": "cache_write_1h",
    "cache hits and refreshes": "cache_read",
    "output tokens": "output",
}
FAST_COLUMNS = {"input": "input", "output": "output"}

FieldPath = tuple[str, ...]  # under models:, e.g. ("anthropic:claude-opus-5-5", "fast", "input")


class PageFormatError(Exception):
    """The page no longer looks the way this parser expects; a person needs to look."""


@dataclass(frozen=True)
class Change:
    path: FieldPath
    old: float | int | None
    new: float | int

    def __str__(self) -> str:
        old = "(missing)" if self.old is None else _fmt(self.old)
        return f"{'.'.join(self.path)}: {old} -> {_fmt(self.new)}"


@dataclass(frozen=True)
class Report:
    changes: list[Change]
    notes: list[str]


# --- reading the page -------------------------------------------------------------------

TIER = re.compile(r"\(for prompts (up to|over) ([\d,]+) tokens\)")


def fetch_page() -> str:
    request = urllib.request.Request(MARKDOWN_URL, headers={"User-Agent": "agent-flight-recorder"})
    with urllib.request.urlopen(request, timeout=30) as response:
        body: bytes = response.read()
    return body.decode("utf-8")


def _sections(markdown: str) -> dict[str, str]:
    """Body of each heading, keyed by its lower-cased title."""
    parts = re.split(r"^#{2,4} +(.+?) *$", markdown, flags=re.MULTILINE)
    return {
        title.strip().lower(): body for title, body in zip(parts[1::2], parts[2::2], strict=True)
    }


def _tables(text: str) -> list[list[list[str]]]:
    """Every Markdown table in the text, as rows of cells (header row first, no separator)."""
    tables: list[list[list[str]]] = []
    current: list[list[str]] = []
    for line in [*text.splitlines(), ""]:
        if line.startswith("|"):
            row = line.strip().removeprefix("|").removesuffix("|")
            cells = [cell.strip() for cell in re.split(r"(?<!\\)\|", row)]  # "\|" is not a split
            if not all(re.fullmatch(r":?-+:?", cell) for cell in cells):
                current.append(cells)
        elif current:
            tables.append(current)
            current = []
    return tables


def _rows(
    sections: dict[str, str], section: str, columns: dict[str, str]
) -> list[tuple[str, dict[str, float]]]:
    """(model name, rates) from the first table in a section whose header has every column."""
    if section not in sections:
        raise PageFormatError(f"no {section!r} section on the page")
    for table in _tables(sections[section]):
        header = [cell.lower() for cell in table[0]]
        if header[0] == "model" and set(columns) <= set(header):
            break
    else:
        raise PageFormatError(f"no table with columns {sorted(columns)} in {section!r}")

    index = {field: header.index(column) for column, field in columns.items()}
    rows: list[tuple[str, dict[str, float]]] = []
    for cells in table[1:]:
        if len(cells) != len(header):
            raise PageFormatError(f"row with {len(cells)} cells, not {len(header)}: {cells[0]!r}")
        rows.append((cells[0], {field: _rate(cells[i]) for field, i in index.items()}))
    return rows


def _rate(cell: str) -> float:
    match = re.match(r"\$([\d,]+(?:\.\d+)?)(?: USD)? / MTok", cell)
    if match is None:
        raise PageFormatError(f"not a price per MTok: {cell!r}")
    return float(match.group(1).replace(",", ""))


def _spec(name: str) -> str:
    """'Claude Opus 5.5' → 'anthropic:claude-opus-5-5'."""
    return PROVIDER + re.sub(r"[\s.]+", "-", name.strip().lower())


def parse_page(markdown: str) -> dict[str, dict[str, Any]]:
    """Rates per model spec, in pricing.yaml's shape (long_context and fast as nested maps)."""
    sections = _sections(markdown)
    models: dict[str, dict[str, Any]] = {}
    for name, rates in _rows(sections, "model pricing", MODEL_COLUMNS):
        name = re.sub(r"\(\[.*?\]\(.*?\)\)", "", name)  # "([limited availability](...))"
        tier = TIER.search(name)
        spec = _spec(TIER.sub("", name))
        entry = models.setdefault(spec, {})
        if tier is None or tier.group(1) == "up to":
            entry.update(rates)
        else:
            threshold = int(tier.group(2).replace(",", ""))
            entry["long_context"] = {"above_input_tokens": threshold, **rates}

    for name, rates in _rows(sections, "fast mode pricing", FAST_COLUMNS):
        for one in name.split(" / "):  # "Claude Opus 5 / Claude Opus 4.8"
            entry = models.setdefault(_spec(one), {})
            entry["fast"] = {**rates, **_fast_cache_rates(entry, rates["input"])}

    if not models:
        raise PageFormatError("no models found on the page")
    return models


def _fast_cache_rates(base: dict[str, Any], fast_input: float) -> dict[str, float]:
    """Cache multipliers apply on top of fast mode, so scale the model's own multipliers."""
    if "input" not in base or not base["input"]:
        return {}
    return {
        field: round(fast_input * base[field] / base["input"], 6)
        for field in ("cache_read", "cache_write_5m", "cache_write_1h")
        if field in base
    }


# --- comparing --------------------------------------------------------------------------

TIER_FIELDS = {"long_context": (*RATE_FIELDS, "above_input_tokens"), "fast": RATE_FIELDS}


def check(text: str, markdown: str) -> Report:
    return compare(yaml.safe_load(text), parse_page(markdown))


def compare(table: dict[str, Any], page: dict[str, dict[str, Any]]) -> Report:
    """Differences between pricing.yaml (as loaded) and the page, for Claude models only."""
    changes: list[Change] = []
    notes: list[str] = []
    for spec, ours in table["models"].items():
        if not spec.startswith(PROVIDER):
            continue
        theirs = page.get(spec)
        if theirs is None:
            notes.append(f"{spec}: not on the pricing page (renamed or retired?)")
            continue
        _compare((spec,), RATE_FIELDS, ours, theirs, changes)
        for tier, fields in TIER_FIELDS.items():
            if tier in ours and tier in theirs:
                _compare((spec, tier), fields, ours[tier], theirs[tier], changes)
            elif tier in ours:
                notes.append(f"{spec}: has {tier} here, but the page shows none")
            elif tier in theirs:
                notes.append(f"{spec}: the page has {tier} rates, not in pricing.yaml")
    return Report(changes, notes)


def _compare(
    path: FieldPath,
    fields: tuple[str, ...],
    ours: dict[str, Any],
    theirs: dict[str, Any],
    changes: list[Change],
) -> None:
    for field in fields:
        old = ours.get(field)  # a missing field and `field: null` are both missing
        if field in theirs and (old is None or abs(old - theirs[field]) > 1e-9):
            changes.append(Change((*path, field), old, theirs[field]))


# --- writing ----------------------------------------------------------------------------

LEAF = re.compile(
    r"^(?P<indent> *)(?P<key>[\w.:-]+):(?P<space> *)(?P<value>[^#\s][^#]*?)?(?P<rest> *(#.*)?)$"
)


def apply(text: str, changes: list[Change], version: str) -> str:
    """pricing.yaml's text with the changes made and the version set; comments are kept.

    A changed value is replaced in place. A field the file lacks is added as the first child
    of its mapping, whose header must already exist (tiers are never added).
    """
    if not re.search(r"^version:", text, re.MULTILINE):
        raise ValueError("pricing.yaml has no top-level version")
    lines = text.splitlines(keepends=True)
    wanted = {change.path: change.new for change in changes}
    stack: list[tuple[int, str]] = []  # (indent, key) of the open mappings
    headers: dict[FieldPath, int] = {}  # line index of each mapping's header
    for i, line in enumerate(lines):
        match = LEAF.match(line.rstrip("\r\n"))
        if match is None:
            continue
        indent, key = len(match["indent"]), match["key"]
        while stack and stack[-1][0] >= indent:
            stack.pop()
        path = (*[k for _, k in stack], key)
        if match["value"] is None:  # a mapping header
            stack.append((indent, key))
            headers[path] = i
        elif path == ("version",):
            lines[i] = _replace(line, match, f'"{version}"')
        elif path[0] == "models" and path[1:] in wanted:
            lines[i] = _replace(line, match, _fmt(wanted.pop(path[1:])))

    # Fields the file didn't have: insert under their mapping header, bottom-up.
    inserts: list[tuple[int, str]] = []
    for field, value in wanted.items():
        header = headers.get(("models", *field[:-1]))
        if header is None:
            raise ValueError(f"no {'.'.join(field[:-1])} mapping in pricing.yaml to add to")
        line = lines[header]
        indent = " " * (len(line) - len(line.lstrip(" ")) + 2)
        ending = line[len(line.rstrip("\r\n")) :] or "\n"
        inserts.append((header + 1, f"{indent}{field[-1]}: {_fmt(value)}{ending}"))
    for index, line in sorted(inserts, reverse=True):
        lines.insert(index, line)
    return "".join(lines)


def _replace(line: str, match: re.Match[str], value: str) -> str:
    ending = line[len(line.rstrip("\r\n")) :]
    return f"{match['indent']}{match['key']}:{match['space'] or ' '}{value}{match['rest']}{ending}"


def _fmt(value: float | int) -> str:
    """Prices as pricing.yaml writes them: at least two decimals (4.00, 0.125)."""
    if isinstance(value, int):
        return str(value)
    whole, fraction = f"{value:.6f}".rstrip("0").split(".")
    return f"{whole}.{fraction.ljust(2, '0')}"


def today() -> str:
    return datetime.now(UTC).date().isoformat()
