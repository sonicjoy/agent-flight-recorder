"""One model call per group (docs/designs/analyst.md §4): a name, a cause and up to three
representatives, checked in code. The model never touches the store or a file.

`chat_model` is imported at module level so tests replace it, as the judge's tests do.
"""

from __future__ import annotations

from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from flight_recorder.evals.judge import CLOSING, escape
from flight_recorder.llm import chat_model
from flight_recorder.store.labels import check_mode

CAUSE_CHARS = 200
MAX_REPRESENTATIVES = 3

__all__ = ["CAUSE_CHARS", "Reply", "chat_model", "judge_group"]


class Reply(BaseModel):
    name: str = Field(description="A short failure-mode name, for example extra-label.")
    cause: str = Field(description="One sentence: what the agent got wrong and why.")
    representatives: list[str] = Field(
        description="1 to 3 item ids (e1, e2, ...) that show this failure best."
    )


def _messages(signature: str, shown: list[dict[str, Any]]) -> list[Any]:
    system = (
        "You name one group of failures from an AI agent. Every item below failed the same "
        f"way: {signature}\n"
        "Reply with a short failure-mode name, one sentence on the cause, and the ids of 1 to "
        f"{MAX_REPRESENTATIVES} items that show it best.\n" + CLOSING
    )
    items = "\n".join(
        f'<item id="e{n}">{escape(str(entry["text"]))}</item>' for n, entry in enumerate(shown, 1)
    )
    return [SystemMessage(system), HumanMessage(items)]


def judge_group(model: str, signature: str, shown: list[dict[str, Any]]) -> dict[str, Any]:
    """`{name, cause, representatives, error}` for one group; any failure is the group's error,
    with the signature as its name and no representatives."""
    try:
        reply = chat_model(model).with_structured_output(Reply).invoke(_messages(signature, shown))
        if not isinstance(reply, Reply):
            raise ValueError("the reply did not fit the schema")
        local = {f"e{n}": entry["id"] for n, entry in enumerate(shown, 1)}
        picked = list(dict.fromkeys(reply.representatives))
        if not 1 <= len(picked) <= MAX_REPRESENTATIVES or any(p not in local for p in picked):
            raise ValueError(f"representatives must be 1 to {MAX_REPRESENTATIVES} of the shown "
                             f"ids, got {picked!r}"[:300])  # fmt: skip
        return {"name": check_mode(reply.name), "cause": reply.cause.strip()[:CAUSE_CHARS],
                "representatives": [local[p] for p in picked], "error": None}  # fmt: skip
    except Exception as error:  # one group's failure never stops the analysis
        return {"name": signature, "cause": None, "representatives": [],
                "error": f"{type(error).__name__}: {error}"[:300]}  # fmt: skip
