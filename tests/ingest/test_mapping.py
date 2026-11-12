"""OTLP spans -> RunIn/SpanIn, for OTel GenAI and OpenInference attributes. No database."""

from __future__ import annotations

import json
from typing import Any

import pytest
from opentelemetry.proto.common.v1.common_pb2 import AnyValue, ArrayValue, KeyValue, KeyValueList
from opentelemetry.trace import Tracer
from otlp_payloads import export, record, triage_run, two_services

from flight_recorder.ingest.mapping import any_value, map_trace, traces
from flight_recorder.store.write import RunIn, SpanIn


def mapped(build: object) -> tuple[RunIn, dict[str, SpanIn]]:
    [trace] = traces(export(record(build)))  # pyright: ignore[reportArgumentType]
    run, spans = map_trace(trace)
    return run, {s.name: s for s in spans}


def test_otel_genai_run_maps_to_a_run_and_a_span_tree() -> None:
    run, spans = mapped(triage_run())

    root, llm, tool = (
        spans["invoke_agent triage_agent"],
        spans["chat gpt-5.4-mini"],
        spans["search_issues"],
    )
    assert (root.kind, llm.kind, tool.kind) == ("agent", "llm", "tool")
    assert llm.parent_external_id == tool.parent_external_id == root.external_id
    assert root.parent_external_id is None
    assert (run.source, run.agent, run.name, run.status) == (
        "otel",
        "issue-triager",  # the application; triage_agent is one of its spans
        root.name,
        "ok",
    )
    assert run.external_id and len(run.external_id) == 32  # hex trace id
    assert run.ended_at == root.ended_at
    assert run.attributes["service.name"] == "issue-triager"


def test_llm_span_model_tokens_and_messages() -> None:
    _, spans = mapped(triage_run())
    llm = spans["chat gpt-5.4-mini"]

    assert llm.model == "openai:gpt-5.4-mini"  # provider normalised, snapshot date dropped
    assert (llm.input_tokens, llm.output_tokens) == (1000, 65)
    assert llm.input == [{"role": "user", "content": "Triage #19. Key: "}]  # JSON string parsed
    assert "gen_ai.input.messages" not in llm.attributes  # moved, not stored twice
    assert llm.attributes["gen_ai.response.model"] == "gpt-5.4-mini-2026-03-17"  # raw kept


def test_tool_span_name_arguments_and_error() -> None:
    _, spans = mapped(triage_run())
    tool = spans["search_issues"]

    assert tool.input == {"query": "evals in CI"}
    assert tool.status == "error"
    assert tool.error == "TimeoutError: search_issues timed out after 30s"
    assert tool.attributes["events"][0]["name"] == "exception"
    assert isinstance(tool.attributes["events"][0]["time"], str)  # JSON-safe for JSONB
    assert tool.model is None


def openinference_run(tracer: Tracer) -> None:
    with tracer.start_as_current_span("LangGraph", attributes={"openinference.span.kind": "CHAIN"}):
        with tracer.start_as_current_span(
            "ChatGoogleGenerativeAI",
            attributes={
                "openinference.span.kind": "LLM",
                "llm.provider": "google",
                "llm.model_name": "gemini-3.5-flash-lite",
                "llm.token_count.prompt": 900,
                "llm.token_count.completion": 40,
                "input.value": json.dumps({"messages": [{"role": "user", "content": "hi"}]}),
                "input.mime_type": "application/json",
                "llm.input_messages.0.message.role": "user",
                "llm.input_messages.0.message.content": "hi",
                "output.value": "plain text reply",
            },
        ):
            pass
        with tracer.start_as_current_span(
            "get_issue",
            attributes={
                "openinference.span.kind": "TOOL",
                "tool.name": "get_issue",
                "input.value": '{"id": "7"}',
            },
        ):
            pass


def test_openinference_run() -> None:
    run, spans = mapped(openinference_run)
    chain, llm, tool = spans["LangGraph"], spans["ChatGoogleGenerativeAI"], spans["get_issue"]

    assert (chain.kind, llm.kind, tool.kind) == ("step", "llm", "tool")
    assert llm.model == "google_genai:gemini-3.5-flash-lite"
    assert (llm.input_tokens, llm.output_tokens) == (900, 40)
    assert llm.input == {"messages": [{"role": "user", "content": "hi"}]}
    assert llm.output == "plain text reply"
    assert not any(
        k.startswith("llm.input_messages.") for k in llm.attributes
    )  # duplicates dropped
    assert tool.input == {"id": "7"}
    assert run.agent == "issue-triager"  # service.name
    assert run.name == "LangGraph"


def test_a_batch_without_the_root_leaves_run_fields_unknown() -> None:
    spans = record(triage_run())
    root_free = [s for s in spans if s.parent is not None]
    [trace] = traces(export(root_free))

    run, mapped_spans = map_trace(trace)

    assert (run.name, run.status, run.ended_at) == (None, "unset", None)
    assert run.agent == "issue-triager"  # service.name comes with every batch
    assert run.started_at == min(s.started_at for s in mapped_spans)


def test_a_trace_across_services_is_named_by_the_roots_service() -> None:
    """The tool server's spans come in the same request; the run is still the triager's."""
    triager, server = two_services()
    [trace] = traces(export(triager + server))

    run, spans = map_trace(trace)

    assert len(spans) == 2
    assert (run.agent, run.attributes["service.name"]) == ("issue-triager", "issue-triager")
    assert not run.fills_gaps


def test_a_batch_without_the_root_only_fills_gaps() -> None:
    """It may come from another service the run called, so it mustn't rename the run."""
    _, server = two_services()
    [trace] = traces(export(server))

    run, _ = map_trace(trace)

    assert (run.agent, run.fills_gaps) == ("tool-server", True)


def test_unknown_operation_is_other_and_no_attributes_is_step() -> None:
    def build(tracer: Tracer) -> None:
        with (
            tracer.start_as_current_span("root"),
            tracer.start_as_current_span("odd", attributes={"gen_ai.operation.name": "teleport"}),
        ):
            pass

    _, spans = mapped(build)

    assert (spans["root"].kind, spans["odd"].kind) == ("step", "other")


def test_any_value_conversions() -> None:
    value = AnyValue(
        kvlist_value=KeyValueList(
            values=[
                KeyValue(key="n", value=AnyValue(int_value=3)),
                KeyValue(key="ok", value=AnyValue(bool_value=True)),
                KeyValue(key="x", value=AnyValue(double_value=0.5)),
                KeyValue(key="raw", value=AnyValue(bytes_value=b"\x01\x02")),
                KeyValue(
                    key="list",
                    value=AnyValue(array_value=ArrayValue(values=[AnyValue(string_value="a")])),
                ),
                KeyValue(key="empty", value=AnyValue()),
            ]
        )
    )

    assert any_value(value) == {
        "n": 3,
        "ok": True,
        "x": 0.5,
        "raw": "AQI=",
        "list": ["a"],
        "empty": None,
    }


def test_without_a_service_name_the_agent_is_the_first_agent_not_the_most_frequent() -> None:
    """A real SDK run was attributed to its comment writer, called more often mid-run."""

    def build(tracer: Tracer) -> None:
        agent = {"gen_ai.operation.name": "invoke_agent"}
        with tracer.start_as_current_span(
            "triage", attributes=agent | {"gen_ai.agent.name": "triage_agent"}
        ):
            for _ in range(3):
                with tracer.start_as_current_span(
                    "comment", attributes=agent | {"gen_ai.agent.name": "comment_writer"}
                ):
                    pass

    [trace] = traces(export(record(build, service=None)))
    run, _ = map_trace(trace)

    assert trace.resource["service.name"].startswith("unknown_service")  # :python.exe
    assert run.agent == "triage_agent"


def nested_agents(tracer: Tracer) -> None:
    """root -> triage_agent -> comment_writer, as in the triager."""
    agent = {"gen_ai.operation.name": "invoke_agent"}
    with (
        tracer.start_as_current_span("root"),
        tracer.start_as_current_span(
            "triage", attributes=agent | {"gen_ai.agent.name": "triage_agent"}
        ),
        tracer.start_as_current_span(
            "comment", attributes=agent | {"gen_ai.agent.name": "comment_writer"}
        ),
    ):
        pass


def test_a_helper_whose_parents_are_in_another_batch_never_names_the_run() -> None:
    """Its depth can't be known from this batch, so it can't win over the real agent."""
    spans = record(nested_agents, service=None)
    without_middle = [s for s in spans if s.name != "triage"]  # root and comment_writer
    [trace] = traces(export(without_middle))

    run, _ = map_trace(trace)

    assert run.agent is None  # unknown, rather than the helper


def span_with(attributes: dict[str, Any], kind: str = "LLM") -> SpanIn:
    def build(tracer: Tracer) -> None:
        with tracer.start_as_current_span(
            "span", attributes={"openinference.span.kind": kind} | attributes
        ):
            pass

    _, spans = mapped(build)
    return spans["span"]


def test_output_messages_are_kept_when_only_input_is_moved() -> None:
    span = span_with({"input.value": "hi", "llm.output_messages.0.message.content": "the reply"})

    assert (span.input, span.output) == ("hi", None)
    assert span.attributes["llm.output_messages.0.message.content"] == "the reply"


def test_content_keys_that_were_not_used_stay_in_attributes() -> None:
    span = span_with(
        {
            "gen_ai.operation.name": "chat",
            "gen_ai.input.messages": '[{"role": "user"}]',
            "input.value": "the same prompt, OpenInference's copy",
            "gen_ai.tool.call.arguments": '{"q": 1}',
        }
    )

    assert span.input == [{"role": "user"}]
    assert "gen_ai.input.messages" not in span.attributes  # moved
    assert span.attributes["input.value"] == "the same prompt, OpenInference's copy"
    assert span.attributes["gen_ai.tool.call.arguments"] == '{"q": 1}'


@pytest.mark.parametrize(
    ("value", "tokens"),
    [
        (12, 12),
        (12.0, 12),
        ("12", 12),
        (float("inf"), None),
        (float("nan"), None),
        (1.5, None),
        (True, None),
        ("\u00b2", None),  # a digit to str.isdigit(), not to int()
        ("12k", None),
    ],
)
def test_token_counts(value: object, tokens: int | None) -> None:
    assert span_with({"llm.token_count.prompt": value}).input_tokens == tokens


@pytest.mark.parametrize(
    ("provider", "model", "expected"),
    [
        ("openai", "gpt-5.4-mini-2026-03-17", "openai:gpt-5.4-mini"),
        ("anthropic", "claude-sonnet-5-5-20260115", "anthropic:claude-sonnet-5-5"),
        ("openai", "gpt-5.5", "openai:gpt-5.5"),
    ],
)
def test_snapshot_dates_are_dropped(provider: str, model: str, expected: str) -> None:
    span = span_with({"llm.provider": provider, "llm.model_name": model})

    assert span.model == expected


def graded_root(tracer: Tracer) -> None:
    """A triager run: its root span carries the grade, and here a prompt too."""
    with tracer.start_as_current_span(
        "triage sonicjoy/agent-flight-recorder#19",
        attributes={
            "openinference.span.kind": "AGENT",
            "triager.grade": "wrong",
            "triager.grade.labels": "wrong",
            "input.value": "the whole issue text, which belongs in the span's input",
        },
    ), tracer.start_as_current_span("tool", attributes={"openinference.span.kind": "TOOL",
                                                          "triager.grade": "child"}):  # fmt: skip
        pass


def test_a_runs_attributes_include_its_root_spans_but_not_its_content() -> None:
    """docs/schema.md: a run's attributes are its resource and root attributes."""
    run, spans = mapped(graded_root)

    assert run.attributes["service.name"] == "issue-triager"  # resource
    assert run.attributes["triager.grade"] == "wrong"  # root, not the child's
    assert run.attributes["triager.grade.labels"] == "wrong"
    assert "input.value" not in run.attributes  # content stays on the span
    assert spans["triage sonicjoy/agent-flight-recorder#19"].input is not None


def test_a_batch_without_the_root_has_only_resource_attributes() -> None:
    spans = record(graded_root)
    [trace] = traces(export([s for s in spans if s.parent is not None]))

    run, _ = map_trace(trace)
    assert "triager.grade" not in run.attributes and run.fills_gaps


def root_with_events_and_a_clashing_key(tracer: Tracer) -> None:
    with tracer.start_as_current_span(
        "root", attributes={"service.name": "a span's own idea", "triager.grade": "ok"}
    ) as span:
        span.add_event("checkpoint", {"step": 1})


def test_a_runs_attributes_leave_events_to_the_span_and_keep_the_resources_names() -> None:
    run, spans = mapped(root_with_events_and_a_clashing_key)

    assert "events" not in run.attributes and spans["root"].attributes["events"]
    assert run.attributes["service.name"] == "issue-triager"  # the resource wins, like run.agent
    assert run.attributes["triager.grade"] == "ok"
