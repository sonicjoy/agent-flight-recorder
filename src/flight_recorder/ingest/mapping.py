"""Map OTLP spans onto the store's RunIn/SpanIn: one trace is one run.

Understands both OpenTelemetry GenAI attributes (gen_ai.*) and OpenInference ones
(openinference.span.kind, llm.*, input.value). The mapping table is in docs/schema.md.
Nothing here writes anything: record_run() redacts and stores what this returns.
"""

from __future__ import annotations

import base64
import json
import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from opentelemetry.proto.common.v1.common_pb2 import AnyValue, KeyValue
from opentelemetry.proto.trace.v1.trace_pb2 import Span as OtlpSpan
from opentelemetry.proto.trace.v1.trace_pb2 import Status as OtlpStatus

from flight_recorder.store.write import Kind, RunIn, SpanIn, Status

KIND_BY_OPERATION: dict[str, Kind] = {
    "chat": "llm",
    "text_completion": "llm",
    "generate_content": "llm",
    "embeddings": "llm",
    "execute_tool": "tool",
    "invoke_agent": "agent",
    "create_agent": "agent",
    "invoke_workflow": "step",
    "retrieval": "step",
}
KIND_BY_OPENINFERENCE: dict[str, Kind] = {
    "LLM": "llm",
    "EMBEDDING": "llm",
    "TOOL": "tool",
    "RETRIEVER": "tool",
    "AGENT": "agent",
    "CHAIN": "step",
    "RERANKER": "step",
    "GUARDRAIL": "step",
    "EVALUATOR": "step",
    "PROMPT": "step",
    "DECISION": "step",
}
# Provider names differ between conventions; these are the ones pricing.yaml uses.
PROVIDERS = {
    "openai": "openai",
    "azure.ai.openai": "openai",
    "anthropic": "anthropic",
    "gcp.gemini": "google_genai",
    "gcp.gen_ai": "google_genai",
    "gcp.vertex_ai": "google_genai",
    "google": "google_genai",
    "vertexai": "google_genai",
    "google_genai": "google_genai",
}
STATUS: dict[int, Status] = {OtlpStatus.STATUS_CODE_OK: "ok", OtlpStatus.STATUS_CODE_ERROR: "error"}
INPUT_TOKENS = ("gen_ai.usage.input_tokens", "llm.token_count.prompt", "gen_ai.usage.prompt_tokens")
OUTPUT_TOKENS = (
    "gen_ai.usage.output_tokens",
    "llm.token_count.completion",
    "gen_ai.usage.completion_tokens",
)
DATED_MODEL = re.compile(r"^(?P<alias>.+)-\d{4}-\d{2}-\d{2}$")  # gpt-5.4-mini-2026-03-17
# Moved into input/output, so not stored twice in attributes.
CONTENT_KEYS = (
    "gen_ai.input.messages",
    "gen_ai.output.messages",
    "gen_ai.tool.call.arguments",
    "gen_ai.tool.call.result",
    "input.value",
    "output.value",
)
FLATTENED_CONTENT = ("llm.input_messages.", "llm.output_messages.")


def any_value(value: AnyValue) -> Any:
    kind = value.WhichOneof("value")
    if kind == "array_value":
        return [any_value(v) for v in value.array_value.values]
    if kind == "kvlist_value":
        return attributes(value.kvlist_value.values)
    if kind == "bytes_value":
        return base64.b64encode(value.bytes_value).decode()
    return getattr(value, kind) if kind else None


def attributes(items: Iterator[KeyValue] | Any) -> dict[str, Any]:
    return {item.key: any_value(item.value) for item in items}


class MappingError(ValueError):
    """A span that can't be stored; the message is safe to return (it holds no span data)."""


def _time(unix_nano: int) -> datetime | None:
    if not unix_nano:
        return None
    try:
        return datetime.fromtimestamp(unix_nano / 1e9, tz=UTC)
    except (OverflowError, OSError, ValueError) as error:
        raise MappingError("timestamp out of range") from error


def _hex(raw: bytes) -> str | None:
    return raw.hex() or None


def _first(attrs: Mapping[str, Any], *keys: str) -> Any:
    return next((attrs[k] for k in keys if attrs.get(k) not in (None, "")), None)


def _maybe_json(value: Any) -> Any:
    if isinstance(value, str) and value[:1] in ("{", "["):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def _kind(attrs: Mapping[str, Any]) -> Kind:
    if (operation := attrs.get("gen_ai.operation.name")) is not None:
        return KIND_BY_OPERATION.get(str(operation), "other")
    if (span_kind := attrs.get("openinference.span.kind")) is not None:
        return KIND_BY_OPENINFERENCE.get(str(span_kind).upper(), "other")
    return "step"


def _model(attrs: Mapping[str, Any]) -> str | None:
    name = _first(
        attrs, "gen_ai.response.model", "gen_ai.request.model", "llm.model_name",
        "llm.response.model_name", "llm.request.model_name", "embedding.model_name",
    )  # fmt: skip
    if name is None:
        return None
    name = str(name)
    if dated := DATED_MODEL.match(name):  # priced by alias, not by snapshot date
        name = dated.group("alias")
    provider = _first(attrs, "gen_ai.provider.name", "gen_ai.system", "llm.provider", "llm.system")
    normalised = PROVIDERS.get(str(provider).lower()) if provider else None
    return f"{normalised}:{name}" if normalised else name


def _tokens(attrs: Mapping[str, Any], *keys: str) -> int | None:
    value = _first(attrs, *keys)
    return int(value) if isinstance(value, int | float) or str(value).isdigit() else None


def _error(span: OtlpSpan) -> str | None:
    if span.status.code != OtlpStatus.STATUS_CODE_ERROR:
        return None
    if span.status.message:
        return span.status.message
    for event in span.events:
        if event.name == "exception":
            attrs = attributes(event.attributes)
            parts = [attrs.get("exception.type"), attrs.get("exception.message")]
            return ": ".join(str(p) for p in parts if p) or None
    return None


def _content(attrs: Mapping[str, Any], kind: Kind) -> tuple[Any, Any]:
    if kind == "tool":
        input_value = _first(attrs, "gen_ai.tool.call.arguments", "input.value")
        output_value = _first(attrs, "gen_ai.tool.call.result", "output.value")
    else:
        input_value = _first(attrs, "gen_ai.input.messages", "input.value")
        output_value = _first(attrs, "gen_ai.output.messages", "output.value")
    return _maybe_json(input_value), _maybe_json(output_value)


def map_span(span: OtlpSpan) -> SpanIn:
    attrs = attributes(span.attributes)
    kind = _kind(attrs)
    input_value, output_value = _content(attrs, kind)
    kept = {
        k: v
        for k, v in attrs.items()
        if k not in CONTENT_KEYS
        and not (input_value is not None and k.startswith(FLATTENED_CONTENT))
    }
    if span.events:
        kept["events"] = [
            {
                "name": e.name,
                "time": (t := _time(e.time_unix_nano)) and t.isoformat(),  # JSON-safe
                "attributes": attributes(e.attributes),
            }
            for e in span.events
        ]
    if span.links:
        kept["links"] = [
            {"trace_id": _hex(link.trace_id), "span_id": _hex(link.span_id)} for link in span.links
        ]
    started = _time(span.start_time_unix_nano)
    if started is None:
        raise MappingError("span has no start time")
    return SpanIn(
        external_id=span.span_id.hex(),
        parent_external_id=_hex(span.parent_span_id),
        kind=kind,
        name=str(_first(attrs, "gen_ai.tool.name", "tool.name") or span.name)
        if kind == "tool"
        else span.name,
        model=_model(attrs) if kind == "llm" else None,
        status=STATUS.get(span.status.code, "unset"),
        error=_error(span),
        started_at=started,
        ended_at=_time(span.end_time_unix_nano),
        input_tokens=_tokens(attrs, *INPUT_TOKENS),
        output_tokens=_tokens(attrs, *OUTPUT_TOKENS),
        input=input_value,
        output=output_value,
        attributes=kept,
    )  # fmt: skip


@dataclass
class Trace:
    """One trace's spans from one export request, with its resource's attributes."""

    trace_id: str
    resource: dict[str, Any] = field(default_factory=dict[str, Any])
    spans: list[OtlpSpan] = field(default_factory=list[OtlpSpan])


def traces(request: ExportTraceServiceRequest) -> list[Trace]:
    by_id: dict[str, Trace] = {}
    for resource_spans in request.resource_spans:
        resource = attributes(resource_spans.resource.attributes)
        for scope_spans in resource_spans.scope_spans:
            for span in scope_spans.spans:
                trace_id = span.trace_id.hex()
                trace = by_id.setdefault(trace_id, Trace(trace_id))
                trace.resource |= resource
                trace.spans.append(span)
    return list(by_id.values())


def _outermost_agent(trace: Trace) -> str | None:
    """The agent nearest the root; agents below it (a comment writer called mid-run) are
    helpers. Real runs showed "most frequent" picking those, and start times can't decide:
    on Windows a parent and child can share a timestamp.
    """
    parents = {s.span_id: s.parent_span_id for s in trace.spans}

    def depth(span_id: bytes) -> int:
        steps = 0
        while (span_id := parents.get(span_id, b"")) and steps < len(parents):
            steps += 1
        return steps

    named = sorted(
        (depth(raw.span_id), raw.start_time_unix_nano, str(name))
        for raw in trace.spans
        if (name := _first(attributes(raw.attributes), "gen_ai.agent.name", "agent.name"))
    )
    return named[0][2] if named else None


def map_trace(trace: Trace) -> tuple[RunIn, list[SpanIn]]:
    """The run as far as this batch knows it; record_run merges batches of the same trace."""
    if not trace.trace_id:
        raise MappingError("span has no trace id")
    spans = [map_span(s) for s in trace.spans]
    roots = [s for s in spans if s.parent_external_id is None]
    root = roots[0] if roots else None
    agent = _outermost_agent(trace) or trace.resource.get("service.name")
    run = RunIn(
        source="otel",
        external_id=trace.trace_id,
        agent=str(agent) if agent else None,
        name=root.name if root else None,
        status=root.status if root else "unset",
        started_at=min(s.started_at for s in spans),
        ended_at=root.ended_at if root else None,  # only the root says when the run ended
        attributes=trace.resource,
    )
    return run, spans
