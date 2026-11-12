"""Turn recorded runs into terminal output, safely.

Everything recorded is untrusted text: a tool's output or a fetched page can hold terminal
escape codes (clear the screen, set the window title), right-to-left overrides that
disguise text, or Rich markup. `clean` shows those as visible escapes, and recorded text
only ever reaches the terminal as `rich.text.Text`, never as markup.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from rich.console import Console, Group, RenderableType
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.tree import Tree

from flight_recorder.store.read import RunSummary, SpanNode

BIDI = frozenset("؜‎‏‪‫‬‭‮⁦⁧⁨⁩")
KEPT = frozenset("\n\t")
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
CONTENT_LINES = 40
CONTENT_CHARS = 4_000


def console() -> Console:
    """A console that never reads markup, emoji codes or highlights in plain strings."""
    return Console(markup=False, emoji=False, highlight=False)


def _escape(char: str) -> str:
    code = ord(char)
    return f"\\x{code:02x}" if code < 0x100 else f"\\u{code:04x}"


def clean(value: str) -> str:
    """Recorded text made safe to print: control characters (but newlines and tabs) and
    bidirectional overrides become visible escapes such as `\\x1b` and `\\u202e`."""
    value = value.replace("\r\n", "\n")
    return "".join(
        _escape(c)
        if c in BIDI or (c not in KEPT and (ord(c) < 0x20 or 0x7F <= ord(c) <= 0x9F))
        else c
        for c in value
    )


def text(value: object, style: str = "") -> Text:
    return Text(clean(str(value)), style=style)


def one_line(value: str | None, width: int) -> str:
    """The first line, cleaned and shortened to `width` characters."""
    if not value:
        return ""
    first = clean(value.strip().split("\n", 1)[0])
    return first if len(first) <= width else first[: width - 1] + "…"


def tokens(count: int | None) -> str:
    if count is None:
        return ""
    for size, unit in ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "k")):
        if count >= size:
            shown = count / size
            return f"{shown:.0f}{unit}" if shown >= 100 else f"{shown:.1f}{unit}"
    return str(count)


def money(cost: Decimal | None) -> str:
    if cost is None:
        return ""
    return f"${cost:.2f}" if cost >= 1 else f"${cost:.4f}"


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


def runs_table(summaries: Sequence[RunSummary]) -> Table:
    """One line per run. The agent says the source (claude-code, or the OTel service);
    the name gets whatever width is left."""
    now = datetime.now().astimezone()
    table = Table(box=None, pad_edge=False, header_style="bold", collapse_padding=True)
    table.add_column("id", no_wrap=True)
    table.add_column("started", no_wrap=True)
    table.add_column("agent", max_width=16, no_wrap=True, overflow="ellipsis")
    table.add_column("name", min_width=8)  # the only column that shrinks
    for column in ("status", "took", "tokens", "cost"):
        table.add_column(column, no_wrap=True, justify="left" if column == "status" else "right")
    for s in summaries:
        run = s.run
        table.add_row(
            str(run.id)[:8],
            short_time(run.started_at, now),
            text(run.agent or run.source),
            Text(one_line(run.name, 80), no_wrap=True, overflow="ellipsis"),
            Text(s.state, style=STATE_STYLE.get(s.state, "")),
            duration(run.started_at, run.ended_at),
            f"{tokens(run.input_tokens)}/{tokens(run.output_tokens)}" if run.input_tokens else "",
            cost(s.known_cost, s.unpriced),
        )
    return table


def _usage_total(nodes: Sequence[SpanNode], key: str) -> int:
    return sum(n.usage.get(key) or 0 for n in nodes if isinstance(n.usage.get(key), int))


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
                shown.update(node.path())
    return shown


def _folded(hidden: Sequence[SpanNode]) -> Text:
    """A summary of the spans folded away: how many of each kind, their tokens and cost."""
    every = [n for h in hidden for n in h.walk()]
    kinds: dict[str, int] = {}
    for h in hidden:
        kinds[h.kind] = kinds.get(h.kind, 0) + 1
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
    tree = Tree(Text("spans", style="dim"), guide_style="dim")

    def add(branch: Tree, node: SpanNode) -> None:
        child_branch = branch.add(span_line(node))
        children = [c for c in node.children if shown is None or c in shown]
        if depth is not None and node.depth + 1 >= depth:
            if children:
                child_branch.add(_folded(children))
            return
        for item in _visible(children, fold):
            if isinstance(item, list):
                child_branch.add(_folded(item))
            else:
                add(child_branch, item)

    for item in _visible([r for r in roots if shown is None or r in shown], fold):
        if isinstance(item, list):
            tree.add(_folded(item))
        else:
            add(tree, item)
    return tree


def _content(value: Any, full: bool, max_lines: int, max_chars: int) -> Text:
    """A span's input or output, pretty-printed, cleaned and cut unless `full`."""
    shown = value if isinstance(value, str) else json.dumps(value, indent=2, ensure_ascii=False)
    shown = clean(shown)
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
    line = Text()
    for i, part in enumerate(node.path()):
        if i:
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
    """One replay step: where it is in the run and the tree, the span, and its content."""
    offset = node.started_at - started
    head = Text(f"step {number}/{total}  +{_clock(offset)}  ", style="dim")
    head.append_text(breadcrumb(node))
    parts: list[RenderableType] = [head, span_line(node)]
    if node.error and not node.problem:
        parts.append(text(node.error, style="red"))
    if content is not None:
        for title, value in zip(("input", "output"), content, strict=True):
            if value not in (None, "", [], {}):
                parts.append(
                    Panel(
                        _content(value, full, max_lines, max_chars), title=title, title_align="left"
                    )
                )
    return Group(*parts)


def _clock(offset: timedelta) -> str:
    seconds = max(0, round(offset.total_seconds()))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"
