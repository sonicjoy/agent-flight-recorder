"""Decoding OTLP/HTTP bodies: protobuf, JSON, gzip, and refusing what we can't take."""

from __future__ import annotations

import gzip
import json
import time

import pytest
from otlp_payloads import export, otlp_json, protobuf, record, triage_run

from flight_recorder.ingest import otlp


def test_protobuf_and_json_decode_to_the_same_export() -> None:
    spans = record(triage_run())

    from_protobuf = otlp.parse(protobuf(spans), otlp.PROTOBUF)
    from_json = otlp.parse(otlp_json(spans), otlp.JSON)

    assert from_protobuf == from_json == export(spans)


def test_json_ids_must_be_hex() -> None:
    document = json.loads(otlp_json(record(triage_run())))
    document["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["traceId"] = "not-hex"

    with pytest.raises(otlp.PayloadError, match="traceId must be hex"):
        otlp.parse(json.dumps(document).encode(), otlp.JSON)


@pytest.mark.parametrize(
    ("body", "kind"),
    [
        (b"\x00\xff garbage", otlp.PROTOBUF),
        (b"{not json", otlp.JSON),
        (b'{"resourceSpans": 3}', otlp.JSON),
    ],
    ids=["bad-protobuf", "bad-json", "wrong-shape"],
)
def test_garbage_is_a_payload_error(body: bytes, kind: str) -> None:
    with pytest.raises(otlp.PayloadError):
        otlp.parse(body, kind)


def test_gzip_is_decompressed() -> None:
    body = protobuf(record(triage_run()))

    assert otlp.decompress(gzip.compress(body), "gzip") == body
    assert otlp.decompress(body, None) == body


def test_gzip_members_are_all_decompressed() -> None:
    """RFC 1952 allows several members in one body; none may be dropped."""
    assert otlp.decompress(gzip.compress(b"one ") + gzip.compress(b"two"), "gzip") == b"one two"


def test_truncated_gzip_is_refused() -> None:
    body = gzip.compress(protobuf(record(triage_run())))

    with pytest.raises(otlp.PayloadError, match="truncated"):
        otlp.decompress(body[:-4], "gzip")


def test_zip_bomb_across_members_is_refused() -> None:
    member = gzip.compress(b"\x00" * (otlp.MAX_BODY_BYTES // 2 + 1))

    with pytest.raises(otlp.PayloadTooLarge):
        otlp.decompress(member + member, "gzip")


def test_zip_bomb_is_refused_without_inflating_it() -> None:
    bomb = gzip.compress(b"\x00" * (otlp.MAX_BODY_BYTES * 5))  # 50 MB of zeros, ~50 KB gzipped

    started = time.perf_counter()
    with pytest.raises(otlp.PayloadTooLarge):
        otlp.decompress(bomb, "gzip")
    assert time.perf_counter() - started < 1.0


@pytest.mark.parametrize(
    ("encoding", "error"), [("br", "unsupported Content-Encoding"), ("gzip", "invalid gzip")]
)
def test_bad_encodings(encoding: str, error: str) -> None:
    with pytest.raises(otlp.PayloadError, match=error):
        otlp.decompress(b"not compressed", encoding)


@pytest.mark.parametrize(
    "content_type",
    ["application/x-protobuf", "application/json; charset=utf-8", "Application/JSON"],
)
def test_media_types_accepted(content_type: str) -> None:
    assert otlp.media_type(content_type) in (otlp.PROTOBUF, otlp.JSON)


@pytest.mark.parametrize("content_type", [None, "text/plain", "application/grpc"])
def test_media_types_refused(content_type: str | None) -> None:
    with pytest.raises(otlp.UnsupportedMediaType):
        otlp.media_type(content_type)
