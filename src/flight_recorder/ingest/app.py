"""The ingest API: POST /v1/traces takes standard OTLP/HTTP trace exports.

Each trace in a request is written in its own transaction through record_run(), which
redacts first. A trace that can't be mapped is rejected on its own and reported in the
response's partial_success; the others are still stored. Error messages never include
the rejected data, which may be sensitive.

No authentication yet (#25): serve on 127.0.0.1 only.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import FastAPI, Header, Request, Response
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from sqlalchemy import Engine

from flight_recorder.ingest import otlp
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


def create_app(engine: Engine, redactor: Redactor) -> FastAPI:
    app = FastAPI(title="Agent Flight Recorder ingest", docs_url=None, redoc_url=None)

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
        try:
            export = otlp.parse(otlp.decompress(bytes(body), content_encoding), kind)
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
            except (ValueError, ValidationError) as error:
                rejected += len(trace.spans)
                reasons.append(f"trace {trace.trace_id or '?'}: {_reason(error)}")
        return Response(
            otlp.response_body(kind, rejected, "; ".join(reasons)[:1000]), media_type=kind
        )

    return app
