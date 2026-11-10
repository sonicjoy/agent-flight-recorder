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
# gpt-5.4-mini-2026-03-17 (OpenAI) or claude-sonnet-5-5-20260115 (Anthropic)
DATED_MODEL = re.compile(r"^(?P<alias>.+)-(?:\d{4}-\d{2}-\d{2}|\d{8})$")
# Where (input, output) come from, first match wins. The key used is moved out of
# attributes, with OpenInference's flattened copy of the same messages, so it isn't
# stored twice. Content keys that weren't used stay in attributes.
TOOL_CONTENT = (
    ("gen_ai.tool.call.arguments", "input.value"),
    ("gen_ai.tool.call.result", "output.value"),
)
MESSAGE_CONTENT = (
    ("gen_ai.input.messages", "input.value"),
    ("gen_ai.output.messages", "output.value"),
)
FLATTENED_CONTENT = ("llm.input_messages.", "llm.output_messages.")
AGENT_NAME = ("gen_ai.agent.name", "agent.name")
UNKNOWN_SERVICE = "unknown_service"  # the SDKs' service.name when none is configured


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


def _first_key(attrs: Mapping[str, Any], *keys: str) -> str | None:
    return next((k for k in keys if attrs.get(k) not in (None, "")), None)


def _first(attrs: Mapping[str, Any], *keys: str) -> Any:
    key = _first_key(attrs, *keys)
    return None if key is None else attrs[key]


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
    """A count from an int, a whole float or a string of digits; anything else is unknown."""
    value = _first(attrs, *keys)
    if isinstance(value, bool):  # a bool is an int to Python, but not a count
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):  # is_integer() is False for inf and NaN too
        return int(value) if value.is_integer() else None
    if isinstance(value, str) and value.isascii() and value.isdigit():
        return int(value)
    return None


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


def _content(attrs: Mapping[str, Any], kind: Kind) -> tuple[Any, Any, dict[str, Any]]:
    """Input, output, and the attributes left once their sources are moved out."""
    sources = TOOL_CONTENT if kind == "tool" else MESSAGE_CONTENT
    values: list[Any] = []
    moved: set[str] = set()
    moved_prefixes: list[str] = []
    for keys, flattened in zip(sources, FLATTENED_CONTENT, strict=True):
        key = _first_key(attrs, *keys)
        values.append(None if key is None else _maybe_json(attrs[key]))
        if key is not None:
            moved.add(key)
            moved_prefixes.append(flattened)
    prefixes = tuple(moved_prefixes)
    kept = {k: v for k, v in attrs.items() if k not in moved and not k.startswith(prefixes)}
    return values[0], values[1], kept


def map_span(span: OtlpSpan) -> SpanIn:
    attrs = attributes(span.attributes)
    kind = _kind(attrs)
    input_value, output_value, kept = _content(attrs, kind)
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
    """One trace's spans from one export request, with its resources' attributes.

    A trace can cross services, each exporting its spans under its own resource, so each
    span's resource is kept too: the root's names the run.
    """

    trace_id: str
    resource: dict[str, Any] = field(default_factory=dict[str, Any])  # all, merged
    spans: list[OtlpSpan] = field(default_factory=list[OtlpSpan])
    resources: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])  # per span


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
                trace.resources.append(resource)
    return list(by_id.values())


def _agent_name(span: OtlpSpan) -> str | None:
    """The span's agent name, read without decoding all its attributes."""
    names = {kv.key: any_value(kv.value) for kv in span.attributes if kv.key in AGENT_NAME}
    name = _first(names, *AGENT_NAME)
    return None if name is None else str(name)


def _application(resource: Mapping[str, Any]) -> str | None:
    """The resource's service.name, sent with every batch. None if it is the SDKs' default
    ("unknown_service", or "unknown_service:<process>"), which names nothing.
    """
    name = resource.get("service.name")
    if name in (None, "") or str(name).startswith(UNKNOWN_SERVICE):
        return None
    return str(name)


def _outermost_agent(trace: Trace) -> str | None:
    """The run's agent when the application has no name: the agent nearest the root.
    Agents below it (a comment writer called mid-run) are helpers. Real runs showed "most
    frequent" picking those, and start times can't decide: on Windows a parent and child
    can share a timestamp.

    Only spans whose chain of parents reaches the root within this batch count. A batch
    without the root can't tell a helper from the outermost agent, and whatever it sent
    would overwrite the run's agent (record_run keeps the newest non-null value).
    """
    parents = {s.span_id: s.parent_span_id for s in trace.spans}

    def depth(span_id: bytes) -> int | None:
        """Steps up to the root, or None if the chain leaves this batch (or loops)."""
        for steps in range(len(parents)):
            parent = parents.get(span_id)
            if parent is None:
                return None
            if not parent:
                return steps
            span_id = parent
        return None

    named = sorted(
        (steps, raw.start_time_unix_nano, name)
        for raw in trace.spans
        if (name := _agent_name(raw)) and (steps := depth(raw.span_id)) is not None
    )
    return named[0][2] if named else None


def map_trace(trace: Trace) -> tuple[RunIn, list[SpanIn]]:
    """The run as far as this batch knows it; record_run merges batches of the same trace."""
    if not trace.trace_id:
        raise MappingError("span has no trace id")
    spans = [map_span(s) for s in trace.spans]
    at = next((i for i, s in enumerate(spans) if s.parent_external_id is None), None)
    root = None if at is None else spans[at]
    if at is None:
        # Maybe from another service the run called: it may only fill what's missing.
        resource, agent = trace.resource, _application(trace.resource)
    else:
        # The application, not one of its agents (those are spans). Without a service
        # name, the outermost agent, which only the batch with the root can tell.
        resource = trace.resources[at]
        agent = _application(resource) or _outermost_agent(trace)
    run = RunIn(
        source="otel",
        external_id=trace.trace_id,
        agent=agent,
        name=root.name if root else None,
        status=root.status if root else "unset",
        started_at=min(s.started_at for s in spans),
        ended_at=root.ended_at if root else None,  # only the root says when the run ended
        attributes=resource,
        fills_gaps=root is None,
    )
    return run, spans
