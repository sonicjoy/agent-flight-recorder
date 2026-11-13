"""Writes docs/evals/retrieval.yaml, the #14 retrieval check's queries, from the run store.

Each query names its right runs by trace id, so the file works on any copy of the store and
its scores can be reproduced. The ids come from rules over the triager runs' attributes:

- behaviour: what the agent decided (`triager.decided.*`), right or wrong; a summary has
  the decision but never the truth, so search can't tell the two apart.
- topic: the issue a run triaged (`triager.case`), with its synthetic paraphrases.

    uv run python scripts/retrieval_queries.py docs/evals/retrieval.yaml
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from sqlalchemy import select

from flight_recorder.store.db import make_engine, session_scope
from flight_recorder.store.models import Run

Rule = Callable[[dict[str, Any]], bool]


def _labels(a: dict[str, Any]) -> set[str]:
    return set(a.get("triager.decided.labels") or [])


def _milestone(a: dict[str, Any]) -> str:
    return str(a.get("triager.decided.milestone") or "")


def _linked(a: dict[str, Any]) -> bool:
    return a.get("triager.decided.duplicate_of") not in (None, "", "(none)")


def _cases(*cases: str) -> Rule:
    return lambda a: a.get("triager.case") in cases


# (query, rule in words, rule)
BEHAVIOUR: list[tuple[str, str, Rule]] = [
    ("marked the issue as a duplicate of another issue", "decided duplicate_of is set", _linked),
    ("added the stretch label to an optional feature", "decided labels include stretch",
     lambda a: "stretch" in _labels(a)),
    ("labelled an eval issue as recorder work too", "decided labels include evals and recorder",
     lambda a: {"evals", "recorder"} <= _labels(a)),
    ("labelled analyst work as evals too", "decided labels include analyst and evals",
     lambda a: {"analyst", "evals"} <= _labels(a)),
    ("labelled infrastructure work as documentation too",
     "decided labels include infra and documentation",
     lambda a: {"infra", "documentation"} <= _labels(a)),
    ("labelled it as UI work", "decided labels include ui", lambda a: "ui" in _labels(a)),
    ("labelled it as MCP work", "decided labels include mcp", lambda a: "mcp" in _labels(a)),
    ("filed it under the week 1 demo agent milestone", "decided milestone is Week 1",
     lambda a: _milestone(a).startswith("Week 1")),
    ("filed it under the week 5 replay UI and launch milestone", "decided milestone is Week 5",
     lambda a: _milestone(a).startswith("Week 5")),
]  # fmt: skip

# Paraphrases (synthetic.yaml `based_on`) count as the same issue.
TOPIC: list[tuple[str, tuple[str, ...]]] = [
    ("containerise the API and MCP server", ("26", "s24")),
    ("launch checklist with gitleaks before going public", ("33",)),
    ("retry when Gemini returns 503 high demand", ("s65",)),
    ("seed data and five-minute quickstart", ("30", "s28")),
    ("trace schema, migrations and local Postgres", ("7", "s06")),
    ("pre-commit end-of-file fixer breaks tests on Windows", ("s47",)),
    ("llm-ping shows $0.00 for a model with no price", ("s62",)),
    ("golden eval cases from labelled failures", ("16", "s15")),
    ("compare our harness with promptfoo or DeepEval", ("20", "s19")),
    ("how do I point my own LangChain agent at the recorder", ("s52",)),
    ("red-team set: prompt injection and secret leaks", ("18", "s17")),
]

HEADER = """\
# The #14 retrieval check's queries: recall@5 of `afr search` over the triager runs.
# Results and decision: error-analysis.md. Run it with
#   uv run afr search-check docs/evals/retrieval.yaml --agent issue-triager [--compare MODEL]
# Generated from the run store by scripts/retrieval_queries.py; don't edit by hand.
# Right runs are trace ids (runs.external_id) of the triager runs that made a decision.
"""


def queries(runs: list[tuple[str, dict[str, Any]]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    rules = [(q, "behaviour", words, rule) for q, words, rule in BEHAVIOUR]
    rules += [(q, "topic", f"case is {' or '.join(c)}", _cases(*c)) for q, c in TOPIC]
    for text, kind, words, rule in rules:
        ids = sorted(trace for trace, attributes in runs if rule(attributes))
        if not ids:
            raise SystemExit(f"no runs for {text!r}")
        out.append({"query": text, "kind": kind, "rule": words, "runs": ids})
    return out


def render(found: list[dict[str, Any]]) -> str:
    lines = [HEADER]
    for q in found:
        lines += [
            f'- query: "{q["query"]}"',
            f"  kind: {q['kind']}",
            f"  rule: {q['rule']} ({len(q['runs'])} {'run' if len(q['runs']) == 1 else 'runs'})",
            f"  runs: [{', '.join(q['runs'])}]",
            "",
        ]
    return "\n".join(lines)


def main(path: str) -> None:
    with session_scope(make_engine()) as session:
        runs = [
            (run.external_id, dict(run.attributes))
            for run in session.scalars(select(Run).where(Run.agent == "issue-triager"))
            if (run.attributes or {}).get("triager.decided.labels") is not None
        ]
    Path(path).write_text(render(queries(runs)), encoding="utf-8", newline="\n")
    print(f"{path}: {len(BEHAVIOUR) + len(TOPIC)} queries over {len(runs)} runs")


if __name__ == "__main__":
    main(sys.argv[1])
