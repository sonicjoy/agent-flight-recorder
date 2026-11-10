"""Real OTLP exports for tests, made by the OpenTelemetry SDK and its own OTLP encoder.

So the bytes are exactly what an exporter would send, not a hand-built imitation.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Callable, Mapping
from typing import Any

from google.protobuf import json_format
from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import Status, StatusCode, Tracer

Attributes = Mapping[str, Any]


def record(build: Callable[[Tracer], None], service: str = "issue-triager") -> list[ReadableSpan]:
    """Run `build` with a fresh tracer and return the finished spans."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider(resource=Resource.create({"service.name": service}))
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    build(provider.get_tracer("tests"))
    provider.shutdown()
    return list(exporter.get_finished_spans())


def export(spans: list[ReadableSpan]) -> ExportTraceServiceRequest:
    return encode_spans(spans)


def protobuf(spans: list[ReadableSpan]) -> bytes:
    return export(spans).SerializeToString()


def otlp_json(spans: list[ReadableSpan]) -> bytes:
    """OTLP/JSON as the spec defines it: like protobuf's JSON, but with hex ids."""
    document = json_format.MessageToDict(export(spans))

    def to_hex(node: Any) -> Any:
        if isinstance(node, dict):
            return {
                k: base64.b64decode(v).hex()
                if k in ("traceId", "spanId", "parentSpanId") and isinstance(v, str)
                else to_hex(v)
                for k, v in node.items()  # pyright: ignore[reportUnknownVariableType]
            }
        if isinstance(node, list):
            return [to_hex(v) for v in node]  # pyright: ignore[reportUnknownVariableType]
        return node

    return json.dumps(to_hex(document)).encode()


def triage_run(secret: str = "", *, tool_error: bool = True) -> Callable[[Tracer], None]:
    """An agent span with an LLM call (OTel GenAI attributes) and a failing tool call."""

    def build(tracer: Tracer) -> None:
        with tracer.start_as_current_span(
            "invoke_agent triage_agent",
            attributes={
                "gen_ai.operation.name": "invoke_agent",
                "gen_ai.agent.name": "triage_agent",
            },
        ) as root:
            with tracer.start_as_current_span(
                "chat gpt-5.4-mini",
                attributes={
                    "gen_ai.operation.name": "chat",
                    "gen_ai.provider.name": "openai",
                    "gen_ai.request.model": "gpt-5.4-mini",
                    "gen_ai.response.model": "gpt-5.4-mini-2026-03-17",
                    "gen_ai.usage.input_tokens": 1000,
                    "gen_ai.usage.output_tokens": 65,
                    "gen_ai.input.messages": json.dumps(
                        [{"role": "user", "content": f"Triage #19. Key: {secret}"}]
                    ),
                    "gen_ai.output.messages": json.dumps([{"role": "assistant", "content": "ok"}]),
                },
            ):
                pass
            with tracer.start_as_current_span(
                "execute_tool search_issues",
                attributes={
                    "gen_ai.operation.name": "execute_tool",
                    "gen_ai.tool.name": "search_issues",
                    "gen_ai.tool.call.arguments": json.dumps({"query": "evals in CI"}),
                },
            ) as tool:
                if tool_error:
                    tool.record_exception(TimeoutError("search_issues timed out after 30s"))
                    tool.set_status(Status(StatusCode.ERROR))
            root.set_status(Status(StatusCode.OK))

    return build
