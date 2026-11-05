"""Triage cases: this repo's real issues plus hand-written synthetic ones.

Each case pairs what the agent may see (`TriageInput`) with the answer it is
graded against (`GroundTruth`). Keep the two apart: the agent must never be
given the target issue's own labels or milestone.
"""

from __future__ import annotations

import json
from functools import cache
from pathlib import Path
from typing import Literal, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

FIXTURES_DIR = Path(__file__).parent / "fixtures"
ISSUES_FILE = FIXTURES_DIR / "issues.json"
SYNTHETIC_FILE = FIXTURES_DIR / "synthetic.yaml"

STRETCH = "stretch"
AREAS = frozenset(
    {"demo-agent", "recorder", "evals", "analyst", "mcp", "infra", "ui", "documentation"}
)
WEEKS = frozenset(range(1, 6))


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class TriageInput(_Frozen):
    """What the agent is given about the issue it triages."""

    id: str
    title: str
    body: str


class GroundTruth(_Frozen):
    """The triage decision the agent is graded against.

    `duplicate_of` and `regression_of` both point at an earlier issue, but mean
    different things and call for different actions:

    - duplicate: asks for the same work again. Close it, link the original.
    - regression: reports that work an issue already delivered (closed as
      completed) has broken again. Keep it open as a bug; link the original.
    """

    areas: frozenset[str]
    stretch: bool
    week: int | None
    duplicate_of: str | None = None
    regression_of: str | None = None

    @model_validator(mode="after")
    def _one_relation(self) -> Self:
        if self.duplicate_of and self.regression_of:
            raise ValueError("an issue is a duplicate or a regression, not both")
        if self.stretch and self.week is None:
            raise ValueError("stretch means 'cut first from its week', so it needs a week")
        return self


class TriageCase(_Frozen):
    id: str
    """Real issues use their number ("7"); synthetic cases use "s01", "s02", ..."""
    title: str
    body: str
    truth: GroundTruth
    source: Literal["real", "synthetic"]
    state: Literal["open", "closed"] = "open"
    kind: Literal["issue", "paraphrase", "tricky"] = "issue"
    based_on: str | None = None
    """For paraphrases: the real issue this rewords."""
    note: str = ""
    """For tricky cases: what makes the case hard."""

    def agent_input(self) -> TriageInput:
        return TriageInput(id=self.id, title=self.title, body=self.body)


class IssueExport(_Frozen):
    repo: str
    issues: list[TriageCase] = Field(default_factory=list[TriageCase])


def load_real() -> list[TriageCase]:
    return IssueExport.model_validate_json(ISSUES_FILE.read_text(encoding="utf-8")).issues


def load_synthetic() -> list[TriageCase]:
    raw = yaml.safe_load(SYNTHETIC_FILE.read_text(encoding="utf-8"))
    return [TriageCase.model_validate({**case, "source": "synthetic"}) for case in raw["cases"]]


@cache
def load_cases() -> tuple[TriageCase, ...]:
    """All cases, real first. Results should be reported per `source`."""
    return (*load_real(), *load_synthetic())


def dump_export(export: IssueExport) -> str:
    """Stable JSON, so re-exporting an unchanged repo leaves no diff."""
    data = export.model_dump(mode="json", exclude_defaults=True)
    for issue in data["issues"]:
        issue["truth"]["areas"] = sorted(issue["truth"]["areas"])
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"
