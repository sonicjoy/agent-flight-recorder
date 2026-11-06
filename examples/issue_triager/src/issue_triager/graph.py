"""The triage graph.

    load_issue -> triage (agent loop) -> validate --invalid--> triage (once) / fail
                                            | ok
                                            v
                                    draft_comment -> human_review (interrupt) -> apply | rejected

Only `triage` loops: the model decides when and what to search. Everything else is fixed.
State holds plain JSON-able values so any checkpointer can store it.
"""

from __future__ import annotations

import json
import operator
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Literal, TypedDict

from langchain.agents import create_agent
from langchain.agents.middleware import ToolCallLimitMiddleware
from langchain.agents.structured_output import ToolStrategy
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import interrupt

from issue_triager.decision import TriageDecision, problems
from issue_triager.fixtures import TriageCase, TriageInput
from issue_triager.prompts import COMMENT_SYSTEM, TRIAGE_SYSTEM, triage_request
from issue_triager.tools import FailureInjector, make_tools

MAX_ATTEMPTS = 2  # first try plus one retry after validation feedback
MAX_TOOL_CALLS = 8
RESULT_PREVIEW = 300


class Usage(TypedDict):
    step: str
    model: str
    input_tokens: int
    output_tokens: int


class TriageState(TypedDict):
    """Full state. Nodes return partial updates (Update); the graph starts from case_id."""

    case_id: str
    issue: dict[str, Any]
    attempts: int
    decision: dict[str, Any] | None
    feedback: list[str]
    steps: Annotated[list[dict[str, Any]], operator.add]
    usage: Annotated[list[Usage], operator.add]
    comment: str
    approved: bool
    status: Literal["applied", "rejected", "failed"]
    output_path: str


Update = dict[str, Any]


@dataclass(frozen=True)
class Models:
    triage: BaseChatModel
    comment: BaseChatModel
    triage_spec: str
    comment_spec: str


def _usage(step: str, model: str, messages: Sequence[AnyMessage]) -> list[Usage]:
    totals = Usage(step=step, model=model, input_tokens=0, output_tokens=0)
    for message in messages:
        if isinstance(message, AIMessage) and message.usage_metadata:
            totals["input_tokens"] += message.usage_metadata["input_tokens"]
            totals["output_tokens"] += message.usage_metadata["output_tokens"]
    return [totals]


def _steps(attempt: int, messages: Sequence[AnyMessage]) -> list[dict[str, Any]]:
    """Tool calls and their results, for the CLI and the dry-run output file."""
    calls: dict[str, dict[str, Any]] = {}
    for message in messages:
        if isinstance(message, AIMessage):
            for call in message.tool_calls:
                calls[call["id"] or ""] = {
                    "attempt": attempt,
                    "tool": call["name"],
                    "args": call["args"],
                }
        elif isinstance(message, ToolMessage) and message.tool_call_id in calls:
            calls[message.tool_call_id]["status"] = message.status
            calls[message.tool_call_id]["result"] = str(message.content)[:RESULT_PREVIEW]
    return list(calls.values())


def build_graph(
    case: TriageCase,
    corpus: Sequence[TriageCase],
    models: Models,
    *,
    checkpointer: BaseCheckpointSaver[Any],
    injector: FailureInjector | None = None,
    runs_dir: Path = Path("runs"),
) -> CompiledStateGraph[TriageState, None, TriageState, TriageState]:
    """A graph that triages `case`, searching only `corpus` (real issues, minus the case)."""
    injector = injector or FailureInjector()
    by_id = {c.id: c for c in corpus if c.id != case.id}
    agent = create_agent(
        models.triage,
        make_tools(corpus, target_id=case.id, injector=injector),
        system_prompt=TRIAGE_SYSTEM,
        response_format=ToolStrategy(TriageDecision),
        middleware=[ToolCallLimitMiddleware(run_limit=MAX_TOOL_CALLS)],
        name="triage_agent",
    )

    def load_issue(state: TriageState) -> Update:
        return {"issue": case.agent_input().model_dump(), "attempts": 0, "feedback": []}

    def triage(state: TriageState, config: RunnableConfig) -> Update:
        issue = TriageInput.model_validate(state["issue"])
        messages: list[AnyMessage] = [HumanMessage(triage_request(issue))]
        if state.get("feedback"):
            previous = json.dumps(state.get("decision"))
            rules = "\n".join(f"- {p}" for p in state["feedback"])
            messages.append(
                HumanMessage(f"Your previous decision {previous} broke these rules:\n{rules}\n"
                             "Decide again.")
            )  # fmt: skip
        attempt = state.get("attempts", 0) + 1
        result = agent.invoke({"messages": messages}, config)  # pyright: ignore[reportArgumentType]
        decision = result.get("structured_response")
        return {
            "attempts": attempt,
            "decision": decision.model_dump() if isinstance(decision, TriageDecision) else None,
            "steps": _steps(attempt, result["messages"]),
            "usage": _usage("triage", models.triage_spec, result["messages"]),
        }

    def validate(state: TriageState) -> Update:
        raw = state.get("decision")
        if raw is None:
            return {"feedback": ["No decision was returned."]}
        decision = TriageDecision.model_validate(raw)
        return {"feedback": problems(decision, target_id=case.id, corpus=by_id)}

    def after_validate(state: TriageState) -> str:
        if not state.get("feedback"):
            return "draft_comment"
        return "triage" if state.get("attempts", 0) < MAX_ATTEMPTS else "fail"

    def fail(state: TriageState) -> Update:
        return {"status": "failed"}

    def draft_comment(state: TriageState, config: RunnableConfig) -> Update:
        issue = TriageInput.model_validate(state["issue"])
        reply = models.comment.invoke(
            [
                SystemMessage(COMMENT_SYSTEM),
                HumanMessage(f"Issue: {issue.title}\n\nDecision: {json.dumps(state['decision'])}"),
            ],
            config,
        )
        return {
            "comment": reply.text.strip(),
            "usage": _usage("comment", models.comment_spec, [reply]),
        }

    def human_review(state: TriageState) -> Update:
        answer = interrupt({"decision": state["decision"], "comment": state["comment"]})
        approved = bool(answer)
        return {"approved": approved} if approved else {"approved": False, "status": "rejected"}

    def after_review(state: TriageState) -> str:
        return "apply" if state.get("approved") else END

    def apply(state: TriageState) -> Update:
        """Dry run: write what would be applied to a local file. Nothing goes to GitHub."""
        runs_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        path = runs_dir / f"triage-{case.id}-{stamp}.json"
        record = {
            "case_id": case.id,
            "models": {"triage": models.triage_spec, "comment": models.comment_spec},
            "decision": state["decision"],
            "comment": state["comment"],
            "attempts": state["attempts"],
            "steps": state.get("steps", []),
            "injected_failures": injector.injected,
            "usage": state.get("usage", []),
        }
        path.write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        return {"status": "applied", "output_path": path.as_posix()}

    graph = StateGraph(TriageState)
    graph.add_node("load_issue", load_issue)
    graph.add_node("triage", triage)
    graph.add_node("validate", validate)
    graph.add_node("fail", fail)
    graph.add_node("draft_comment", draft_comment)
    graph.add_node("human_review", human_review)
    graph.add_node("apply", apply)
    graph.add_edge(START, "load_issue")
    graph.add_edge("load_issue", "triage")
    graph.add_edge("triage", "validate")
    graph.add_conditional_edges("validate", after_validate, ["triage", "draft_comment", "fail"])
    graph.add_edge("fail", END)
    graph.add_edge("draft_comment", "human_review")
    graph.add_conditional_edges("human_review", after_review, ["apply", END])
    graph.add_edge("apply", END)
    return graph.compile(checkpointer=checkpointer, name="issue_triager")
