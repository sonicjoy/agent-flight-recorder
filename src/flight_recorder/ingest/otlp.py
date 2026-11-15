"""Decode an OTLP/HTTP trace export: protobuf or JSON, optionally gzip-compressed.

OTLP/JSON differs from protobuf's own JSON mapping in one way that matters: trace and span
ids are hex strings, not base64 (OTLP spec, "JSON Protobuf Encoding"). They're converted
before parsing.
"""

from __future__ import annotations

import base64
import json
import zlib
from typing import Any

from google.protobuf import json_format
from google.protobuf.message import DecodeError
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
    ExportTraceServiceResponse,
)

MAX_BODY_BYTES = 10 * 1024 * 1024  # after decompression too: a small gzip can expand hugely
PROTOBUF = "application/x-protobuf"
JSON = "application/json"
ID_FIELDS = ("traceId", "spanId", "parentSpanId")


class PayloadError(ValueError):
    """The request can't be decoded (HTTP 400)."""


class PayloadTooLarge(ValueError):
    """The body, or what it decompresses to, is over MAX_BODY_BYTES (HTTP 413)."""


class UnsupportedMediaType(ValueError):
    """Neither protobuf nor JSON (HTTP 415)."""


def media_type(content_type: str | None) -> str:
    kind = (content_type or "").split(";")[0].strip().lower()
    if kind not in (PROTOBUF, JSON):
        raise UnsupportedMediaType(f"Content-Type must be {PROTOBUF} or {JSON}, not {kind!r}")
    return kind


def decompress(body: bytes, content_encoding: str | None) -> bytes:
    encoding = (content_encoding or "identity").strip().lower()
    if encoding == "identity":
        return body
    if encoding != "gzip":
        raise PayloadError(f"unsupported Content-Encoding {encoding!r}; use gzip or none")
    data = bytearray()
    rest = body
    while rest:  # a gzip body may hold several members, one after another (RFC 1952)
        inflater = zlib.decompressobj(wbits=16 + zlib.MAX_WBITS)  # 16+: gzip header
        try:
            # Stop one byte past the limit instead of inflating a zip bomb in full.
            data += inflater.decompress(rest, MAX_BODY_BYTES + 1 - len(data))
        except zlib.error as error:
            raise PayloadError(f"invalid gzip body: {error}") from error
        if len(data) > MAX_BODY_BYTES or inflater.unconsumed_tail:
            raise PayloadTooLarge(f"body expands to more than {MAX_BODY_BYTES} bytes")
        if not inflater.eof:
            raise PayloadError("invalid gzip body: truncated")
        rest = inflater.unused_data
    return bytes(data)


def _hex_ids_to_base64(node: Any) -> Any:
    if isinstance(node, dict):
        converted: dict[str, Any] = {}
        for key, value in node.items():  # pyright: ignore[reportUnknownVariableType]
            if key in ID_FIELDS and isinstance(value, str):
                try:
                    value = base64.b64encode(bytes.fromhex(value)).decode()
                except ValueError as error:
                    raise PayloadError(f"{key} must be hex in OTLP/JSON: {error}") from error
            converted[str(key)] = _hex_ids_to_base64(value)  # pyright: ignore[reportUnknownArgumentType]
        return converted
    if isinstance(node, list):
        return [_hex_ids_to_base64(item) for item in node]  # pyright: ignore[reportUnknownVariableType]
    return node


def parse(data: bytes, kind: str) -> ExportTraceServiceRequest:
    request = ExportTraceServiceRequest()
    try:
        if kind == PROTOBUF:
            request.ParseFromString(data)
        else:
            document = json.loads(data)
            if not isinstance(document, dict):
                raise PayloadError("not a valid OTLP trace export: not a JSON object")
            json_format.ParseDict(_hex_ids_to_base64(document), request, ignore_unknown_fields=True)
    except (
        DecodeError,
        json.JSONDecodeError,
        json_format.ParseError,
        UnicodeDecodeError,
        RecursionError,  # nested deeper than a trace export ever is
    ) as error:
        raise PayloadError(f"not a valid OTLP trace export: {type(error).__name__}") from error
    return request


def response_body(kind: str, rejected_spans: int = 0, error_message: str = "") -> bytes:
    response = ExportTraceServiceResponse()
    if rejected_spans:
        response.partial_success.rejected_spans = rejected_spans
        response.partial_success.error_message = error_message
    if kind == PROTOBUF:
        return response.SerializeToString()
    return json_format.MessageToJson(response, indent=None).encode()
