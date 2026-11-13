"""Turn recorded runs into terminal output, safely.

Everything recorded is untrusted text: a tool's output or a fetched page can hold terminal
escape codes (clear the screen, set the window title), right-to-left overrides that
disguise text, invisible characters that hide words from a reader (but not from a model),
or Rich markup. `clean` shows those as visible escapes, and recorded text only ever
reaches the terminal as `rich.text.Text`, never as markup.
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections import Counter
from collections.abc import Sequence
from datetime import datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any, cast

from rich.console import Console, Group, RenderableType
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.tree import Tree

from flight_recorder.payloads import cut, flat

if TYPE_CHECKING:
    from flight_recorder.store.read import RunSummary, SpanNode

KEPT = frozenset("\n\t")
HIDDEN = frozenset(("Cc", "Cf", "Zl", "Zp", "Co", "Cn"))
"""Control and format characters (bidi overrides, zero-width, tags), line and paragraph
separators, private-use and unassigned code points: shown as escapes."""
FILLERS = frozenset(chr(c) for c in (0x115F, 0x1160, 0x3164, 0xFFA0))
"""Hangul fillers: letters that render blank."""


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


STATE_STYLE = {
    "ok": "green",
    "error": "bold red",
    "rejected": "magenta",
    "running": "cyan",
    "unfinished": "yellow",
    "unset": "dim",
}
FOLD_OVER = 20  # children; a node with more shows the first and last few, and every problem
FOLD_KEEP = 5
TREE_DEPTH = 40  # levels `show` draws at most; deeper spans are summarised
BREADCRUMB_PARTS = 6
CONTENT_LINES = 40
CONTENT_CHARS = 4_000


def console() -> Console:
    """A console that never reads markup, emoji codes or highlights in plain strings."""
    return Console(markup=False, emoji=False, highlight=False)


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


def text(value: object, style: str = "") -> Text:
    return Text(clean(str(value)), style=style)


def one_line(value: str | None, width: int) -> str:
    """The first line, cleaned and shortened to `width` characters."""
    if not value:
        return ""
    return cut(clean(value.strip().split("\n", 1)[0]), width)


def tokens(count: int | None) -> str:
    if count is None:
        return ""
    for size, unit in ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "k")):
        if count >= size - size // 2_000:  # 999,950 rounds to 1.0M, not 1000k
            shown = count / size
            return f"{shown:.0f}{unit}" if shown >= 99.95 else f"{shown:.1f}{unit}"
    return str(count)


def money(cost: Decimal | None) -> str:
    if cost is None:
        return ""
    if 0 < cost < Decimal("0.0001"):
        return "<$0.0001"
    shown = cost.quantize(Decimal("0.0001"))
    return f"${shown:.2f}" if shown >= 1 else f"${shown:.4f}"


def cost(known: Decimal | None, unpriced: bool) -> str:
    """A cost, saying so when part of it couldn't be priced. Blank when nothing has a cost
    (tool and step spans aren't model calls), never $0."""
    if not unpriced:
        return money(known)
    return f"{money(known)} + unpriced" if known else "unpriced"


def duration(started: datetime, ended: datetime | None) -> str:
    if ended is None:
        return "open"
    seconds = (ended - started).total_seconds()
    if seconds < 1:
        return f"{seconds * 1000:.0f}ms"
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, secs = divmod(round(seconds), 60)
    if minutes < 60:
        return f"{minutes}m{secs:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"


def local(at: datetime) -> str:
    return at.astimezone().strftime("%Y-%m-%d %H:%M")


def short_time(at: datetime, now: datetime) -> str:
    """`11-12 14:03` this year, `2025-11-12` before."""
    shown = at.astimezone()
    return shown.strftime("%m-%d %H:%M" if shown.year == now.year else "%Y-%m-%d")


COLUMNS = ("id", "started", "agent", "name", "status", "took", "tokens", "cost")
OPTIONAL_COLUMNS = ("agent", "tokens", "took", "started")
"""Dropped in this order when the terminal is too narrow; id, name, status and cost stay."""


def runs_table(summaries: Sequence[RunSummary], width: int) -> Table:
    """One line per run, fitted to `width`. The agent says the source (claude-code, or the
    OTel service); the name gets whatever width is left."""
    now = datetime.now().astimezone()
    rows = [
        {
            "id": str(s.run.id)[:8],
            "started": short_time(s.run.started_at, now),
            "agent": text(s.run.agent or s.run.source),
            "name": Text(one_line(s.run.name, 80), no_wrap=True, overflow="ellipsis"),
            "status": Text(s.state, style=STATE_STYLE.get(s.state, "")),
            "took": duration(s.run.started_at, s.run.ended_at),
            "tokens": (
                f"{tokens(s.run.input_tokens)}/{tokens(s.run.output_tokens)}"
                if s.run.input_tokens
                else ""
            ),
            "cost": cost(s.known_cost, s.unpriced),
        }
        for s in summaries
    ]
    widths = {c: max([len(c), *(_cells(row[c]) for row in rows)]) for c in COLUMNS}
    widths["agent"] = min(widths["agent"], 16)
    widths["name"] = 8  # it shrinks to fit
    dropped: list[str] = []
    while True:
        kept = [c for c in COLUMNS if c not in dropped]
        needed = sum(widths[c] for c in kept) + len(kept) - 1  # one space between columns
        if needed <= width or len(dropped) == len(OPTIONAL_COLUMNS):
            return _table(rows, kept)
        dropped.append(OPTIONAL_COLUMNS[len(dropped)])


def _cells(value: str | Text) -> int:
    return (Text(value) if isinstance(value, str) else value).cell_len


def _table(rows: list[dict[str, Any]], columns: list[str]) -> Table:
    table = Table(box=None, pad_edge=False, header_style="bold", collapse_padding=True)
    for column in columns:
        if column == "name":
            table.add_column(column, min_width=8)  # the only column that shrinks
        elif column == "agent":
            table.add_column(column, max_width=16, no_wrap=True, overflow="ellipsis")
        else:
            right = column in ("took", "tokens", "cost")
            table.add_column(column, no_wrap=True, justify="right" if right else "left")
    for row in rows:
        table.add_row(*(row[c] for c in columns))
    return table


def _usage_total(nodes: Sequence[SpanNode], key: str) -> int:
    return sum(v for n in nodes if isinstance(v := n.usage.get(key), int))


def run_header(summary: RunSummary, roots: Sequence[SpanNode]) -> Panel:
    """The run's identity and totals, with how much of its input came from the cache."""
    run = summary.run
    nodes = [n for root in roots for n in root.walk()]
    lines = Text()
    lines.append(one_line(run.name, 200) or "(no name)", style="bold")
    rows: list[tuple[str, Text]] = [
        ("id", Text(str(run.id))),
        ("source", text(f"{run.source} · {run.external_id}")),
        ("agent", text(run.agent or "")),
        ("status", Text(summary.state, style=STATE_STYLE.get(summary.state, ""))),
        (
            "started",
            Text(f"{local(run.started_at)} · took {duration(run.started_at, run.ended_at)}"),
        ),
        ("spans", Text(str(len(nodes)))),
    ]
    if run.input_tokens or run.output_tokens:
        read = _usage_total(nodes, "cache_read_tokens")
        written = _usage_total(nodes, "cache_write_5m_tokens") + _usage_total(
            nodes, "cache_write_1h_tokens"
        )
        used = f"{tokens(run.input_tokens or 0)} in"
        if read and run.input_tokens:
            used += f" ({read / run.input_tokens:.0%} cache reads)"
        if written:
            used += f", {tokens(written)} cache writes"
        rows.append(("tokens", Text(f"{used} · {tokens(run.output_tokens or 0)} out")))
    rows.append(("cost", Text(cost(summary.known_cost, summary.unpriced) or "-")))
    for label, value in rows:
        lines.append(f"\n{label:<8}", style="dim")
        lines.append_text(value)
    return Panel(lines, expand=False)


def span_line(node: SpanNode) -> Text:
    """One span: kind, name, model, status, time, tokens and cost."""
    line = Text()
    line.append(f"{node.kind} ", style="dim")
    line.append(one_line(node.name, 80), style="bold" if node.kind in ("agent", "step") else "")
    if node.model:
        line.append(f"  {one_line(node.model, 40)}", style="blue")
    if node.status != "unset":
        line.append(f"  {node.status}", style=STATE_STYLE.get(node.status, ""))
    line.append(f"  {duration(node.started_at, node.ended_at)}", style="dim")
    if node.input_tokens or node.output_tokens:
        line.append(f"  {tokens(node.input_tokens or 0)}/{tokens(node.output_tokens or 0)} tok")
        cached = node.usage.get("cache_read_tokens")
        if isinstance(cached, int) and cached:
            line.append(f" ({tokens(cached)} cached)", style="dim")
    priced = cost(node.cost_usd, node.unpriced)
    if priced:
        line.append(f"  {priced}", style="yellow" if node.unpriced else "")
    if node.problem and node.error:
        line.append(f"\n{one_line(node.error, 160)}", style=STATE_STYLE[node.status])
    return line


def keep(roots: Sequence[SpanNode], kind: str | None, problems_only: bool) -> set[SpanNode] | None:
    """The spans a filtered tree shows: the matches and their ancestors. None: no filter."""
    if kind is None and not problems_only:
        return None
    shown: set[SpanNode] = set()
    for root in roots:
        for node in root.walk():
            if (kind is None or node.kind == kind) and (not problems_only or node.problem):
                ancestor: SpanNode | None = node
                while ancestor is not None and ancestor not in shown:  # each span once
                    shown.add(ancestor)
                    ancestor = ancestor.parent
    return shown


def _folded(hidden: Sequence[SpanNode]) -> Text:
    """A summary of the spans folded away: how many of each kind, their tokens and cost."""
    every = [n for h in hidden for n in h.walk()]
    kinds = Counter(h.kind for h in hidden)
    parts = ", ".join(f"{count} {kind}" for kind, count in sorted(kinds.items()))
    line = Text(f"… {len(hidden)} more ({parts})", style="dim italic")
    used_in = sum(n.input_tokens or 0 for n in every)
    used_out = sum(n.output_tokens or 0 for n in every)
    if used_in or used_out:
        line.append(f"  {tokens(used_in)}/{tokens(used_out)} tok")
    priced = [n.cost_usd for n in every if n.cost_usd is not None]
    folded_cost = cost(sum(priced, Decimal(0)) if priced else None, any(n.unpriced for n in every))
    if folded_cost:
        line.append(f"  {folded_cost}")
    return line


def _has_problem(node: SpanNode) -> bool:
    return any(n.problem for n in node.walk())


def _visible(children: Sequence[SpanNode], fold: bool) -> list[SpanNode | list[SpanNode]]:
    """Children to show, with each run of folded ones as a list. Spans with a problem
    below them are never folded."""
    if not fold or len(children) <= FOLD_OVER:
        return list(children)
    items: list[SpanNode | list[SpanNode]] = []
    gap: list[SpanNode] = []
    last = len(children) - FOLD_KEEP
    for i, child in enumerate(children):
        if i < FOLD_KEEP or i >= last or _has_problem(child):
            if gap:
                items.append(gap)
                gap = []
            items.append(child)
        else:
            gap.append(child)
    if gap:
        items.append(gap)
    return items


def span_tree(
    roots: Sequence[SpanNode],
    *,
    depth: int | None = None,
    shown: set[SpanNode] | None = None,
    fold: bool = True,
) -> Tree:
    """The span tree, at most `depth` levels (and never more than TREE_DEPTH)."""
    levels = min(depth or TREE_DEPTH, TREE_DEPTH)
    tree = Tree(Text("spans", style="dim"), guide_style="dim")

    def add_all(branch: Tree, nodes: Sequence[SpanNode]) -> None:
        for item in _visible([n for n in nodes if shown is None or n in shown], fold):
            if isinstance(item, list):
                branch.add(_folded(item))
            else:
                add(branch, item)

    def add(branch: Tree, node: SpanNode) -> None:
        child_branch = branch.add(span_line(node))
        if node.depth + 1 < levels:
            add_all(child_branch, node.children)  # recursion bounded by TREE_DEPTH
            return
        children = [c for c in node.children if shown is None or c in shown]
        if children:
            child_branch.add(_folded(children))

    add_all(tree, roots)
    return tree


PLAIN_LOOKALIKES = ("null", "true", "false")


def _plain(value: str) -> bool:
    """Whether a one-line string can be shown bare without passing for something else: a
    number, null, a key, a list item, a block, or a string with spaces at its ends."""
    if not value or value != value.strip() or "\n" in value or ": " in value:
        return False
    if value in PLAIN_LOOKALIKES or value[0] in "-\"'|{[#":
        return False
    try:
        float(value)
    except ValueError:
        return True
    return False


def _scalar(value: Any) -> str:
    if isinstance(value, str) and _plain(value):
        return value
    return json.dumps(value, ensure_ascii=False)


def _pretty(value: Any, indent: int = 0) -> list[str]:
    """JSON as indented `key: value` lines, so a prompt reads as text rather than one long
    line of `\\n` escapes. A multi-line string is a block under `|`; any string that could
    pass for a number, null, a key or a list item is quoted, as is any such key."""
    pad = "  " * indent
    if isinstance(value, dict | list) and value:
        items: list[tuple[str, Any]] = (
            [(f"{_scalar(str(k))}:", v) for k, v in cast("dict[str, Any]", value).items()]
            if isinstance(value, dict)
            else [("-", v) for v in cast("list[Any]", value)]
        )
        lines: list[str] = []
        for label, item in items:
            if isinstance(item, str) and "\n" in item:
                lines.append(f"{pad}{label} |")
                lines += [f"{pad}  {line}" for line in item.split("\n")]
            elif isinstance(item, dict | list) and item:
                lines.append(f"{pad}{label}")
                lines += _pretty(item, indent + 1)
            else:
                lines.append(f"{pad}{label} {_scalar(item)}")
        return lines
    if isinstance(value, str) and "\n" in value:
        return [pad + line for line in value.split("\n")]  # the whole content is text
    return [pad + (value if isinstance(value, str) else _scalar(value))]


def _content(value: Any, full: bool, max_lines: int, max_chars: int) -> Text:
    """A span's input or output, readable, cleaned and cut unless `full`."""
    shown = clean("\n".join(_pretty(value)))
    note = ""
    if not full:
        lines = shown.split("\n")
        if len(lines) > max_lines:
            note = f"{len(lines) - max_lines} more lines"
            shown = "\n".join(lines[:max_lines])
        if len(shown) > max_chars:
            note = f"{len(shown) - max_chars:,} more characters"
            shown = shown[:max_chars]
    body = Text(shown)
    if note:
        body.append(f"\n… {note}; --full shows everything", style="dim italic")
    return body


def breadcrumb(node: SpanNode) -> Text:
    """Where a span is: its ancestors' names, the nearest few only."""
    path: list[SpanNode] = []
    part: SpanNode | None = node
    while part is not None and len(path) <= BREADCRUMB_PARTS:  # never the whole chain
        path.append(part)
        part = part.parent
    path.reverse()
    line = Text()
    if len(path) > BREADCRUMB_PARTS:
        line.append("…", style="dim")
        path = path[-BREADCRUMB_PARTS:]
    for i, part in enumerate(path):
        if i or line:
            line.append(" › ", style="dim")  # noqa: RUF001 (a deliberate separator)
        line.append(one_line(part.name, 40), style="bold" if part is node else "")
    return line


def step(
    node: SpanNode,
    number: int,
    total: int,
    started: datetime,
    content: tuple[Any, Any] | None,
    *,
    full: bool = False,
    max_lines: int = CONTENT_LINES,
    max_chars: int = CONTENT_CHARS,
) -> RenderableType:
    """One replay step: where it is in the run and the tree, the span, and its content.
    `started` is the run's start, so times stay the same whichever spans are replayed."""
    offset = node.started_at - started
    head = Text(f"step {number}/{total}  +{_clock(offset)}  ", style="dim")
    head.append_text(breadcrumb(node))
    parts: list[RenderableType] = [head, span_line(node)]
    if node.error and not node.problem:
        parts.append(text(node.error, style="red"))
    if content is not None:
        for title, value in zip(("input", "output"), content, strict=True):
            if value not in (None, "", [], {}):
                body = _content(value, full, max_lines, max_chars)
                parts.append(Panel(body, title=title, title_align="left"))
    return Group(*parts)


def _clock(offset: timedelta) -> str:
    seconds = max(0, round(offset.total_seconds()))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


# --- labelling (#14) ------------------------------------------------------------------

PAIRED = re.compile(r"(?P<prefix>.+)\.decided\.(?P<field>.+)")
NOISE_PREFIXES = ("telemetry.", "service.instance.", "openinference.")


def _attribute(value: object) -> str:
    if isinstance(value, list):
        return ", ".join(str(v) for v in value) or "(none)"  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]
    return str(value)


def _pairs(attributes: dict[str, Any]) -> list[tuple[str, str, str, str]]:
    """(prefix, field, decided key, expected key) for each `<p>.decided.<f>` that has a
    `<p>.expected.<f>`, in key order."""
    found: list[tuple[str, str, str, str]] = []
    for key in sorted(attributes):
        match = PAIRED.fullmatch(key)
        if match:
            expected_key = f"{match['prefix']}.expected.{match['field']}"
            if expected_key in attributes:
                found.append((match["prefix"], match["field"], key, expected_key))
    return found


def decision_rows(attributes: dict[str, Any]) -> Text:
    """One row per decided/expected pair, both sides named: `agent X  ≠  truth Y`."""
    lines = Text()
    for prefix, field, decided_key, expected_key in _pairs(attributes):
        decided = _attribute(attributes[decided_key])
        expected = _attribute(attributes[expected_key])
        same = decided == expected
        lines.append_text(text(f"{one_line(prefix, 60)} {one_line(field, 40)}: ", "dim"))
        lines.append("agent ", style="dim")
        lines.append_text(text(one_line(flat(decided), 160), "" if same else "bold red"))
        lines.append("  =  " if same else "  ≠  ", style="dim" if same else "red")
        lines.append("truth ", style="dim")
        lines.append_text(text(one_line(flat(expected), 160)))
        lines.append("\n")
    lines.rstrip()
    return lines


def truth_source(attributes: dict[str, Any]) -> Text | None:
    """Where the truth came from, for the `agent vs truth` heading: `truth from a synthetic
    case (s65)`, from `<p>.case_source` and `<p>.case`. A synthetic case stands out."""
    for prefix, *_ in _pairs(attributes):
        source = attributes.get(f"{prefix}.case_source")
        if source is None:
            continue
        line = Text("truth from a ")
        style = "bold yellow" if str(source) == "synthetic" else ""
        line.append_text(text(one_line(str(source), 40), style))
        line.append(" case")
        case = attributes.get(f"{prefix}.case")
        if case is not None:
            line.append_text(text(f" ({one_line(str(case), 40)})"))
        return line
    return None


def other_attributes(attributes: dict[str, Any]) -> Text:
    """Every attribute that isn't a decided/expected pair or SDK noise."""
    paired = {key for _, _, *keys in _pairs(attributes) for key in keys}
    lines = Text()
    for key in sorted(attributes):
        if key in paired or key.startswith(NOISE_PREFIXES):
            continue
        lines.append(f"{one_line(key, 60)}: ", style="dim")
        lines.append_text(text(one_line(_attribute(attributes[key]), 160), "dim"))
        lines.append("\n")
    lines.rstrip()
    return lines
