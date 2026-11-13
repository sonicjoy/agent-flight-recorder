"""The ingest API: POST /v1/traces takes standard OTLP/HTTP trace exports, and
POST /v1/claude-code/events takes Claude Code hook events (ingest/claude_code.py).

Each trace in a request is written in its own transaction through record_run(), which
redacts first. A trace that can't be mapped is rejected on its own and reported in the
response's partial_success; the others are still stored. Error messages never include
the rejected data, which may be sensitive. If the database can't be reached, the request
gets a 503 so the exporter sends it again; anything else one trace or event gets wrong
rejects only that one, so a batch is never refused for one bad item.

No authentication yet (#25): serve on 127.0.0.1 only. Requests must also name a loopback
host, so a web page can't reach the API through DNS rebinding.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Annotated, Any, cast

from fastapi import FastAPI, Header, Request, Response
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from sqlalchemy import Engine
from sqlalchemy.exc import DBAPIError, OperationalError
from sqlalchemy.exc import TimeoutError as PoolTimeout
from starlette.concurrency import run_in_threadpool

from flight_recorder.ingest import otlp
from flight_recorder.ingest.claude_code import EventError, apply_event, items_over_limit
from flight_recorder.ingest.mapping import MappingError, map_trace, traces
from flight_recorder.redact import Redactor
from flight_recorder.store.db import session_scope
from flight_recorder.store.write import record_run


def _reason(error: Exception) -> str:
    """Why a trace was rejected, without echoing its data."""
    if isinstance(error, ValidationError):
        fields = sorted(
            {".".join(str(p) for p in e["loc"]) for e in error.errors(include_input=False)}
        )
        return f"invalid fields: {', '.join(fields)}"
    return str(error) if isinstance(error, MappingError) else type(error).__name__


# SQLSTATE classes that mean the database, not the data, is the trouble: connection
# exception (08), insufficient resources (53), operator intervention such as a shutdown
# (57), system error (58). Others, such as 54 (an id too long for an index row), come from
# the data: retrying the same trace would fail the same way for ever.
UNAVAILABLE_SQLSTATE_CLASSES = ("08", "53", "57", "58")
RETRY_AFTER_SECONDS = "5"


def _database_unavailable(error: Exception) -> bool:
    """The database itself unreachable or out of connections: nothing wrong with the data.
    OTLP exporters retry only 429, 502, 503 and 504, so this must be a 503, not a 500.
    Retrying is safe: traces already written are upserted again, not duplicated."""
    if isinstance(error, PoolTimeout):
        return True
    if isinstance(error, DBAPIError) and error.connection_invalidated:
        return True
    if isinstance(error, OperationalError):
        code = getattr(error.orig, "sqlstate", None)  # None: no server answered at all
        return not isinstance(code, str) or code.startswith(UNAVAILABLE_SQLSTATE_CLASSES)
    return False


def _ingest(
    engine: Engine, redactor: Redactor, body: bytes, encoding: str | None, kind: str
) -> Response:
    """Decode and store an export. Blocking (protobuf, the database): run off the event loop."""
    try:
        export = otlp.parse(otlp.decompress(body, encoding), kind)
    except otlp.PayloadTooLarge as error:
        return JSONResponse({"error": str(error)}, status_code=413)
    except otlp.PayloadError as error:
        return JSONResponse({"error": str(error)}, status_code=400)

    rejected = 0
    reasons: list[str] = []
    for trace in traces(export):
        try:
            run, spans = map_trace(trace)
            with session_scope(engine) as session:
                record_run(session, run, spans, redactor=redactor)
        except Exception as error:  # one trace's problem: a value, a check, an overflow...
            if _database_unavailable(error):
                return _unavailable(error)
            rejected += len(trace.spans)
            reasons.append(f"trace {trace.trace_id or '?'}: {_reason(error)}")
    return Response(otlp.response_body(kind, rejected, "; ".join(reasons)[:1000]), media_type=kind)


def _unavailable(error: Exception) -> Response:
    return JSONResponse(
        {"error": f"database unavailable ({type(error).__name__}); retry later"},
        status_code=503,
        headers={"Retry-After": RETRY_AFTER_SECONDS},
    )


def _ingest_claude_code(engine: Engine, redactor: Redactor, body: bytes) -> Response:
    """Apply hook events in the order they fired, each in its own transaction."""
    try:
        document = json.loads(body)
        raw = document["events"]
        if not isinstance(raw, list):
            raise TypeError
        events = cast("list[Any]", raw)
    except (ValueError, KeyError, TypeError):
        return JSONResponse({"error": 'expected {"events": [...]}'}, status_code=400)

    def fired_at(event: dict[str, Any]) -> float:
        value = event.get("received_at")
        try:
            return float(value) if isinstance(value, int | float) else 0.0
        except OverflowError:  # an int too big for a float: out of range, so unknown
            return 0.0

    valid = [cast("dict[str, Any]", e) for e in events if isinstance(e, dict)]
    ordered = sorted(valid, key=fired_at)
    accepted, dropped, reasons = 0, 0, list[str]()
    for event in ordered:
        try:
            with session_scope(engine) as session:
                apply_event(session, redactor, event)
            accepted += 1
            dropped += items_over_limit(event)
        except Exception as error:  # one event's problem; the others are still applied
            if _database_unavailable(error):  # the hook keeps the events and sends them again
                return _unavailable(error)
            reasons.append(str(error) if isinstance(error, EventError) else _reason(error))
    reply: dict[str, Any] = {
        "accepted": accepted,
        "rejected": len(events) - accepted,
        "errors": reasons[:20],
    }
    if dropped:  # usage or decision entries past MAX_ITEMS in an accepted event
        reply["dropped_items"] = dropped
    return JSONResponse(reply)


LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "[::1]"})


def _host(header: str) -> str:
    """The host part of a Host header, without the port ("[::1]:4318" -> "[::1]")."""
    return header.rsplit(":", 1)[0] if header.count(":") == 1 or "]:" in header else header


def create_app(
    engine: Engine, redactor: Redactor, *, allowed_hosts: frozenset[str] = LOOPBACK_HOSTS
) -> FastAPI:
    app = FastAPI(title="Agent Flight Recorder ingest", docs_url=None, redoc_url=None)

    @app.middleware("http")
    async def loopback_only(  # pyright: ignore[reportUnusedFunction]
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if _host(request.headers.get("host", "").lower()) not in allowed_hosts:
            return JSONResponse(
                {"error": "requests must be addressed to a loopback host"}, status_code=421
            )
        return await call_next(request)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:  # pyright: ignore[reportUnusedFunction]
        return {"status": "ok"}

    @app.post("/v1/traces")
    async def export_traces(  # pyright: ignore[reportUnusedFunction]
        request: Request,
        content_type: Annotated[str | None, Header()] = None,
        content_encoding: Annotated[str | None, Header()] = None,
    ) -> Response:
        try:
            kind = otlp.media_type(content_type)
        except otlp.UnsupportedMediaType as error:
            return JSONResponse({"error": str(error)}, status_code=415)

        body = bytearray()
        async for chunk in request.stream():  # read with a cap, not request.body()
            body += chunk
            if len(body) > otlp.MAX_BODY_BYTES:
                return JSONResponse({"error": "body too large"}, status_code=413)
        return await run_in_threadpool(
            _ingest, engine, redactor, bytes(body), content_encoding, kind
        )

    @app.post("/v1/claude-code/events")
    async def claude_code_events(  # pyright: ignore[reportUnusedFunction]
        request: Request, content_type: Annotated[str | None, Header()] = None
    ) -> Response:
        if (content_type or "").split(";")[0].strip().lower() != otlp.JSON:
            return JSONResponse({"error": "Content-Type must be application/json"}, status_code=415)
        body = bytearray()
        async for chunk in request.stream():
            body += chunk
            if len(body) > otlp.MAX_BODY_BYTES:
                return JSONResponse({"error": "body too large"}, status_code=413)
        return await run_in_threadpool(_ingest_claude_code, engine, redactor, bytes(body))

    return app
