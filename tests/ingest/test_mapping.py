"""OTLP spans -> RunIn/SpanIn, for OTel GenAI and OpenInference attributes. No database."""

from __future__ import annotations

import json

from opentelemetry.proto.common.v1.common_pb2 import AnyValue, ArrayValue, KeyValue, KeyValueList
from opentelemetry.trace import Tracer
from otlp_payloads import export, record, triage_run

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
        "triage_agent",
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
    assert run.agent == "issue-triager"  # no agent name: falls back to service.name
    assert run.name == "LangGraph"


def test_a_batch_without_the_root_leaves_run_fields_unknown() -> None:
    spans = record(triage_run())
    root_free = [s for s in spans if s.parent is not None]
    [trace] = traces(export(root_free))

    run, mapped_spans = map_trace(trace)

    assert (run.name, run.status, run.ended_at) == (None, "unset", None)
    assert run.started_at == min(s.started_at for s in mapped_spans)


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


def test_run_agent_is_the_first_agent_not_the_most_frequent() -> None:
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

    run, _ = mapped(build)

    assert run.agent == "triage_agent"
