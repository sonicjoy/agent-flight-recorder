"""Shared fixtures for the LLM judge's tests (#17): a rubric, items, and a fake chat model."""

from __future__ import annotations

import copy
from typing import Any

RUBRIC: dict[str, Any] = {
    "model": "anthropic:claude-haiku-5-5",
    "instructions": "You grade a triage comment written for a GitHub issue.",
    "criteria": [
        {
            "id": "suggests-not-settles",
            "pass": "any duplicate or regression is worded as a suggestion",
            "fail": "it states a duplicate or regression as decided",
        },
        {
            "id": "only-decision-references",
            "pass": "every issue, label and milestone it names is in the decision",
            "fail": "it names one the decision doesn't",
        },
    ],
    "examples": [
        {
            "item": {"comment": "This is a duplicate of #2.", "title": "t", "decision": {}},
            "criteria": {"suggests-not-settles": "fail", "only-decision-references": "fail"},
            "reason": "Settles a duplicate the decision doesn't hold.",
        }
    ],
}

ITEM: dict[str, Any] = {
    "comment": "This may duplicate #2; a maintainer should confirm.",
    "title": "Tests fail on Windows",
    "decision": {"labels": ["bug"], "duplicate_of": 2, "rationale": "Same failure as #2."},
}


def rubric() -> dict[str, Any]:
    return copy.deepcopy(RUBRIC)


def items(*extra: dict[str, Any]) -> list[dict[str, Any]]:
    base = [
        {"id": "r-0001", "tag": "corpus", "item": copy.deepcopy(ITEM), "grade": "pass"},
        {"id": "b-1", "tag": "broken", "grade": "fail", "targets": "suggests-not-settles",
         "author": "owner", "item": copy.deepcopy(ITEM) | {"comment": "Duplicate of #2."}},
    ]  # fmt: skip
    return [*base, *extra]


def judge_input(**changes: Any) -> dict[str, Any]:
    from flight_recorder.evals.judge_suite import VERSION

    rub = rubric()
    value: dict[str, Any] = {
        "judge_version": VERSION,
        "model": rub.pop("model"),
        "rubric": rub,
        "item": copy.deepcopy(ITEM),
    }
    return value | changes


class FakeModel:
    """`chat_model(spec)` stand-in: `with_structured_output(schema).invoke(messages)` returns
    what `reply(schema)` gives, and records the schema and messages."""

    def __init__(self, reply: Any) -> None:
        self.reply = reply
        self.specs: list[str] = []
        self.messages: list[list[Any]] = []
        self.schema: Any = None

    def __call__(self, spec: str) -> FakeModel:
        self.specs.append(spec)
        return self

    def with_structured_output(self, schema: Any) -> FakeModel:
        self.schema = schema
        return self

    def invoke(self, messages: list[Any]) -> Any:
        self.messages.append(messages)
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply(self.schema) if callable(self.reply) else self.reply


def verdicts(**by_id: str) -> Any:
    """A reply factory: an instance of the schema with these verdicts and a reason."""

    def make(schema: Any) -> Any:
        return schema.model_validate({**by_id, "reason": "because"})

    return make
