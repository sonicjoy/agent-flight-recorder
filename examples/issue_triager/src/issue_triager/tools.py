"""The triager's tools. They read the committed fixtures only; nothing touches GitHub.

`FailureInjector` breaks tool calls on purpose, so the recorder sees errors the
agent has to cope with.
"""

from __future__ import annotations

import json
import random
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, get_args

from langchain_core.tools import BaseTool, ToolException, tool

from issue_triager.fixtures import TriageCase
from issue_triager.search import KeywordIndex

FailureMode = Literal["timeout", "bad-data", "empty", "noise"]
FAILURE_MODES: tuple[FailureMode, ...] = get_args(FailureMode)
BODY_PREVIEW = 300


@dataclass
class FailureInjector:
    """Picks a failure for a tool call with probability `rate`, or None."""

    modes: Sequence[FailureMode] = ()
    rate: float = 0.0
    seed: int | None = None
    injected: list[str] = field(default_factory=list[str])

    def __post_init__(self) -> None:
        self._rng = random.Random(self.seed)

    def pick(self, tool_name: str) -> FailureMode | None:
        if not self.modes or self._rng.random() >= self.rate:
            return None
        mode = self._rng.choice(self.modes)
        self.injected.append(f"{tool_name}:{mode}")
        return mode

    def shuffle(self, items: list[TriageCase]) -> list[TriageCase]:
        return self._rng.sample(items, k=len(items))


def status_of(case: TriageCase) -> str:
    if case.truth.duplicate_of:
        return f"closed as duplicate of #{case.truth.duplicate_of}"
    return "done (closed)" if case.state == "closed" else "open"


def summarise(case: TriageCase, *, full: bool = False) -> dict[str, Any]:
    """An issue as a past triage decision: its labels and week are fair to show."""
    body = case.body if full else case.body[:BODY_PREVIEW]
    return {
        "id": case.id,
        "title": case.title,
        "status": status_of(case),
        "areas": sorted(case.truth.areas),
        "stretch": case.truth.stretch,
        "week": case.truth.week,
        "body": body,
    }


def _corrupt(item: dict[str, Any]) -> dict[str, Any]:
    """Bad data the way real APIs serve it: truncated text, missing and wrong fields."""
    return {
        "id": item["id"],
        "title": item["title"][: len(item["title"]) // 2],
        "status": None,
        "areas": [],
        "week": -1,
        "body": item["body"][:40] + "��",
    }


def make_tools(
    corpus: Sequence[TriageCase],
    *,
    target_id: str,
    injector: FailureInjector | None = None,
) -> list[BaseTool]:
    """Tools over `corpus` (real issues). The issue being triaged is never visible."""
    index = KeywordIndex(corpus)
    by_id = {c.id: c for c in corpus if c.id != target_id}
    inject = injector or FailureInjector()

    @tool
    def search_issues(query: str) -> str:
        """Keyword search over this repo's existing issues. Returns up to 5 matches as JSON,
        each with its id, title, status, area labels, stretch flag, week and the start of
        its body. Use specific words; there is no synonym matching."""
        mode = inject.pick("search_issues")
        if mode == "timeout":
            raise ToolException("search_issues timed out after 30s")
        if mode == "empty":
            return "[]"
        if mode == "noise":
            hits = inject.shuffle(list(by_id.values()))[:5]
        else:
            hits = [case for case, _ in index.search(query, exclude=target_id)]
        items = [summarise(c) for c in hits]
        if mode == "bad-data":
            items = [_corrupt(i) for i in items]
        return json.dumps(items, ensure_ascii=False)

    @tool
    def get_issue(issue_id: str) -> str:
        """Fetch one existing issue in full by id (for example "7"), as JSON."""
        mode = inject.pick("get_issue")
        if mode == "timeout":
            raise ToolException("get_issue timed out after 30s")
        case = by_id.get(issue_id.lstrip("#"))
        if case is None or mode == "empty":
            raise ToolException(f"issue {issue_id} not found")
        if mode == "noise":
            case = inject.shuffle(list(by_id.values()))[0]
        item = summarise(case, full=True)
        return json.dumps(_corrupt(item) if mode == "bad-data" else item, ensure_ascii=False)

    for t in (search_issues, get_issue):
        t.handle_tool_error = True  # the agent sees the error message, as with a real API
    return [search_issues, get_issue]
