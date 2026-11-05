"""Export this repo's GitHub issues to the triager's fixture file.

Read-only: it runs `gh issue list` and never writes to GitHub.
Run with: uv run python scripts/export_issues.py
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

from issue_triager.fixtures import (
    AREAS,
    ISSUES_FILE,
    STRETCH,
    GroundTruth,
    IssueExport,
    TriageCase,
    dump_export,
)

REPO = "sonicjoy/agent-flight-recorder"
FIELDS = "number,title,body,labels,milestone,state,stateReason,comments"
DUPLICATE_OF = re.compile(r"^\s*duplicate of #(\d+)", re.IGNORECASE | re.MULTILINE)
REGRESSION_OF = re.compile(r"^\s*regression of #(\d+)", re.IGNORECASE | re.MULTILINE)
WEEK_TITLE = re.compile(r"^Week (\d+)\b")


def fetch_issues(repo: str = REPO) -> list[dict[str, Any]]:
    out = subprocess.run(
        ["gh", "issue", "list", "--repo", repo, "--state", "all", "--limit", "1000",
         "--json", FIELDS],
        check=True, capture_output=True, text=True, encoding="utf-8",
    ).stdout  # fmt: skip
    return json.loads(out)


def duplicate_of(issue: dict[str, Any]) -> str | None:
    """GitHub's convention: a closing comment that starts "Duplicate of #N"."""
    for comment in issue.get("comments", []):
        if match := DUPLICATE_OF.search(comment["body"]):
            return match.group(1)
    return None


def regression_of(issue: dict[str, Any]) -> str | None:
    """Our convention: a line in the body or a comment that starts "Regression of #N"."""
    for text in (issue["body"], *(c["body"] for c in issue.get("comments", []))):
        if match := REGRESSION_OF.search(text):
            return match.group(1)
    return None


def week_of(milestone: dict[str, Any] | None) -> int | None:
    if not milestone:
        return None
    match = WEEK_TITLE.match(milestone["title"])
    return int(match.group(1)) if match else None


def to_case(issue: dict[str, Any]) -> TriageCase:
    labels = {label["name"] for label in issue["labels"]}
    unknown = labels - AREAS - {STRETCH}
    if unknown:
        raise ValueError(f"#{issue['number']} has labels the triager doesn't know: {unknown}")
    return TriageCase(
        id=str(issue["number"]),
        title=issue["title"],
        body=issue["body"].replace("\r\n", "\n").strip(),
        truth=GroundTruth(
            areas=frozenset(labels & AREAS),
            stretch=STRETCH in labels,
            week=week_of(issue["milestone"]),
            duplicate_of=duplicate_of(issue),
            regression_of=regression_of(issue),
        ),
        source="real",
        state="closed" if issue["state"] == "CLOSED" else "open",
    )


def build_export(issues: list[dict[str, Any]], repo: str = REPO) -> IssueExport:
    cases = sorted((to_case(i) for i in issues), key=lambda c: int(c.id))
    return IssueExport(repo=repo, issues=cases)


def main() -> None:
    export = build_export(fetch_issues())
    ISSUES_FILE.parent.mkdir(parents=True, exist_ok=True)
    ISSUES_FILE.write_text(dump_export(export), encoding="utf-8", newline="\n")
    dupes = [c for c in export.issues if c.truth.duplicate_of]
    print(f"wrote {len(export.issues)} issues to {Path(os.path.relpath(ISSUES_FILE)).as_posix()}")
    pairs = ", ".join(f"#{c.id} -> #{c.truth.duplicate_of}" for c in dupes)
    print(f"duplicates: {pairs or 'none'}")


if __name__ == "__main__":
    main()
