"""The triager's structured output, and the code checks it must pass."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

from pydantic import BaseModel, Field

from issue_triager.fixtures import GroundTruth, TriageCase

Area = Literal["demo-agent", "recorder", "evals", "analyst", "mcp", "infra", "ui", "documentation"]


class TriageDecision(BaseModel):
    """Your triage decision for the issue."""

    areas: list[Area] = Field(description="One or more area labels for the work.")
    stretch: bool = Field(description="True if nice-to-have: cut first if its week runs long.")
    week: int | None = Field(
        description="Milestone week 1-5 the work belongs in, or null if it is outside the plan."
    )
    duplicate_of: str | None = Field(
        default=None,
        description="Id of an existing issue asking for the same work, if you are fairly sure.",
    )
    regression_of: str | None = Field(
        default=None,
        description="Id of a done (closed) issue whose work this reports as broken again.",
    )
    confidence: float = Field(ge=0, le=1, description="How sure you are overall, 0 to 1.")
    rationale: str = Field(description="One or two sentences explaining the decision.")

    def to_truth(self) -> GroundTruth:
        """The comparable part, in the same shape as the fixtures' ground truth."""
        return GroundTruth(
            areas=frozenset(self.areas),
            stretch=self.stretch,
            week=self.week,
            duplicate_of=self.duplicate_of,
            regression_of=self.regression_of,
        )


def problems(
    decision: TriageDecision, *, target_id: str, corpus: Mapping[str, TriageCase]
) -> list[str]:
    """Rule violations, as feedback the agent can act on. Empty means valid."""
    found: list[str] = []
    if not decision.areas:
        found.append("Pick at least one area.")
    if decision.week is not None and not 1 <= decision.week <= 5:
        found.append(f"week must be 1-5 or null, not {decision.week}.")
    if decision.stretch and decision.week is None:
        found.append("stretch means 'cut first from its week', so it needs a week.")
    if decision.duplicate_of and decision.regression_of:
        found.append("An issue is a duplicate or a regression, not both.")
    for field, ref in (
        ("duplicate_of", decision.duplicate_of),
        ("regression_of", decision.regression_of),
    ):
        if ref is None:
            continue
        ref = ref.lstrip("#")
        if ref == target_id:
            found.append(f"{field} can't point at the issue being triaged.")
        elif ref not in corpus:
            found.append(f"{field}: issue #{ref} doesn't exist.")
        elif field == "regression_of" and (
            corpus[ref].state != "closed" or corpus[ref].truth.duplicate_of
        ):
            found.append(
                f"regression_of: #{ref} isn't done, so it can't have regressed. "
                "A bug in unfinished work is neither a duplicate nor a regression."
            )
    return found
