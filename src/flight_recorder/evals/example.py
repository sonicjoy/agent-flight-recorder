"""A rule-based agent that exists only to exercise the eval harness (the `smoke` suite).

It reads an issue's title and body and answers like a tiny triager, with no model and no
key, so CI and anyone with a checkout can run `afr eval run --suite smoke`. Its score says
nothing about the issue triager or any real agent.
"""

from __future__ import annotations

from typing import Any

RULES = (  # (words, label, type), checked in order
    (("crash", "error", "exception"), "bug", "bug"),
    (("docs", "readme", "typo"), "documentation", "docs"),
    (("slow", "timeout"), "performance", None),
)


def triage(input: dict[str, Any]) -> dict[str, Any]:
    text = f"{input.get('title', '')} {input.get('body', '')}".lower()
    matched = [(label, kind) for words, label, kind in RULES if any(w in text for w in words)]
    kinds = [kind for _, kind in matched if kind]
    return {
        "type": kinds[0] if kinds else None,  # a bug word wins over docs
        "labels": [label for label, _ in matched],
        "meta": {"rules": len(matched)},
    }
