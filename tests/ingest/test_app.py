"""POST /v1/traces end to end: OTLP in, redacted rows out, against a real Postgres."""

from __future__ import annotations

import gzip
import json
from collections.abc import Callable, Generator
from typing import Any

import pytest
from fake_secrets import secrets_by_kind
from fastapi.testclient import TestClient
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceResponse
from opentelemetry.trace import Tracer
from otlp_payloads import otlp_json, protobuf, record, triage_run, two_services
from sqlalchemy import Engine, create_engine, func, select, text
from sqlalchemy.orm import Session
from typer.testing import CliRunner

from flight_recorder.cli import app as cli
from flight_recorder.ingest.app import create_app
from flight_recorder.ingest.otlp import JSON, MAX_BODY_BYTES, PROTOBUF
from flight_recorder.redact import Redactor
from flight_recorder.store.db import upgrade
from flight_recorder.store.models import Run, Span

pytestmark = pytest.mark.db

REDACTOR = Redactor(b"test-key-0123456789abcdef0123456789abcdef")


@pytest.fixture
def store(make_database: Callable[[], str]) -> Generator[Engine]:
    """A fresh, migrated database per test: the app commits, so no rollback trick here."""
    url = make_database()
    upgrade(url)
    engine = create_engine(url)
    yield engine
    engine.dispose()


@pytest.fixture
def client(store: Engine) -> TestClient:
    return TestClient(create_app(store, REDACTOR), base_url="http://127.0.0.1:4318")


def post(client: TestClient, body: bytes, kind: str = PROTOBUF, **headers: str) -> Any:
    return client.post("/v1/traces", content=body, headers={"Content-Type": kind, **headers})


def rows(store: Engine) -> tuple[list[Run], list[Span]]:
    with Session(store) as session:
        return list(session.scalars(select(Run))), list(session.scalars(select(Span)))


def test_protobuf_export_is_stored_as_a_run_with_a_span_tree(
    client: TestClient, store: Engine
) -> None:
    response = post(client, protobuf(record(triage_run())))

    assert response.status_code == 200
    assert response.headers["content-type"] == PROTOBUF
    parsed = ExportTraceServiceResponse.FromString(response.content)
    assert not parsed.HasField("partial_success")
    [run], spans = rows(store)
    by_name = {s.name: s for s in spans}
    root = by_name["invoke_agent triage_agent"]
    assert {s.parent_id for s in spans if s is not root} == {root.id}
    assert (run.agent, run.status, run.name) == ("issue-triager", "ok", root.name)
    llm = by_name["chat gpt-5.4-mini"]
    assert llm.model == "openai:gpt-5.4-mini" and llm.cost_usd and llm.cost_usd > 0
    assert (run.input_tokens, run.cost_usd) == (1000, llm.cost_usd)
    assert by_name["search_issues"].error == "TimeoutError: search_issues timed out after 30s"


def test_json_export_is_stored_the_same_way(client: TestClient, store: Engine) -> None:
    response = post(client, otlp_json(record(triage_run())), JSON)

    assert response.status_code == 200
    assert response.json() == {}
    [run], spans = rows(store)
    assert len(spans) == 3 and run.agent == "issue-triager"


def test_gzip_body(client: TestClient, store: Engine) -> None:
    response = post(
        client, gzip.compress(protobuf(record(triage_run()))), **{"Content-Encoding": "gzip"}
    )

    assert response.status_code == 200
    assert len(rows(store)[1]) == 3


def test_secrets_in_spans_never_reach_the_database(client: TestClient, store: Engine) -> None:
    secret = secrets_by_kind()["openai_key"]

    post(client, protobuf(record(triage_run(secret))))

    with store.connect() as connection:
        dump = " ".join(
            connection.scalar(text(f"SELECT coalesce(string_agg(t::text, ' '), '') FROM {t} t"))
            or ""
            for t in ("runs", "spans")
        )
    assert secret not in dump
    assert "[REDACTED:openai_key:" in dump


def test_batches_of_one_trace_merge_and_link(client: TestClient, store: Engine) -> None:
    """Exporters send spans as they end: children first, the root last."""
    spans = record(triage_run())
    children = [s for s in spans if s.parent is not None]
    root = [s for s in spans if s.parent is None]

    post(client, protobuf(children))
    [run], _ = rows(store)
    assert run.name is None  # the root hasn't arrived

    post(client, protobuf(root))
    [run], stored = rows(store)
    root_row = next(s for s in stored if s.parent_id is None)
    assert run.name == root_row.name and run.status == "ok"
    assert all(s.parent_id == root_row.id for s in stored if s is not root_row)


def bad_tokens(secret: str) -> Callable[[Tracer], None]:
    def build(tracer: Tracer) -> None:
        with tracer.start_as_current_span(
            "chat",
            attributes={
                "gen_ai.operation.name": "chat",
                "gen_ai.usage.input_tokens": -5,
                "note": secret,
            },
        ):
            pass

    return build


def test_one_bad_trace_is_rejected_and_the_rest_are_stored(
    client: TestClient, store: Engine
) -> None:
    secret = secrets_by_kind()["github_token"]
    body = protobuf(record(triage_run()) + record(bad_tokens(secret)))

    response = post(client, body)

    assert response.status_code == 200
    partial = ExportTraceServiceResponse.FromString(response.content).partial_success
    assert partial.rejected_spans == 1
    assert "input_tokens" in partial.error_message
    assert (
        secret not in partial.error_message and "-5" not in partial.error_message
    )  # no data echoed
    [run], _ = rows(store)
    assert run.agent == "issue-triager"


def bad_for_postgres(attributes: dict[str, Any]) -> Callable[[Tracer], None]:
    def build(tracer: Tracer) -> None:
        with tracer.start_as_current_span("chat", attributes=attributes):
            pass

    return build


@pytest.mark.parametrize(
    "attributes",
    [
        {"note": "nul \x00 char"},
        {"score": float("nan")},
        {"gen_ai.operation.name": "chat", "gen_ai.usage.input_tokens": "9" * 25},
    ],
    ids=["nul-in-jsonb", "nan-in-jsonb", "tokens-over-int8"],
)
def test_a_trace_postgres_refuses_is_rejected_and_the_rest_are_stored(
    client: TestClient, store: Engine, attributes: dict[str, Any]
) -> None:
    body = protobuf(record(triage_run()) + record(bad_for_postgres(attributes)))

    response = post(client, body)

    assert response.status_code == 200
    partial = ExportTraceServiceResponse.FromString(response.content).partial_success
    assert partial.rejected_spans == 1
    assert "DataError" in partial.error_message
    [run], _ = rows(store)
    assert run.agent == "issue-triager"


def ends_before_it_starts(tracer: Tracer) -> None:
    """Clock skew, or a buggy exporter: breaks the ended_at >= started_at check."""
    span = tracer.start_span("chat", start_time=2_000_000_000_000_000_000)
    span.end(end_time=1_000_000_000_000_000_000)


def nul_in_name(tracer: Tracer) -> None:
    """A NUL in a plain text column, not JSONB: psycopg refuses it before Postgres does."""
    with tracer.start_as_current_span("nul \x00 name"):
        pass


@pytest.mark.parametrize(
    ("build", "error"),
    [(ends_before_it_starts, "IntegrityError"), (nul_in_name, "DataError")],
    ids=["ends-before-it-starts", "nul-in-text-column"],
)
def test_a_span_postgres_refuses_is_rejected_and_the_rest_are_stored(
    client: TestClient, store: Engine, build: Callable[[Tracer], None], error: str
) -> None:
    response = post(client, protobuf(record(triage_run()) + record(build)))

    partial = ExportTraceServiceResponse.FromString(response.content).partial_success
    assert (response.status_code, partial.rejected_spans) == (200, 1)
    assert error in partial.error_message
    [run], _ = rows(store)
    assert run.agent == "issue-triager"


def test_an_unreachable_database_is_a_503_so_the_exporter_retries() -> None:
    """OTLP exporters retry 429, 502, 503 and 504 only; a 500 would lose the spans."""
    engine = create_engine(
        "postgresql+psycopg://afr@127.0.0.1:1/afr", connect_args={"connect_timeout": 2}
    )  # nothing listens on port 1
    try:
        response = post(
            TestClient(create_app(engine, REDACTOR), base_url="http://127.0.0.1:4318"),
            protobuf(record(triage_run())),
        )
    finally:
        engine.dispose()

    assert response.status_code == 503
    assert response.headers["retry-after"] == "5"
    assert "OperationalError" in response.json()["error"]


def nested_agents(tracer: Tracer) -> None:
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


@pytest.mark.parametrize("helper_last", [True, False], ids=["helper-last", "helper-first"])
def test_a_later_batch_does_not_rename_the_runs_agent(
    client: TestClient, store: Engine, helper_last: bool
) -> None:
    """A batch holding only the helper agent used to rename the run after it.

    With no service name, the run's agent is the outermost agent's name.
    """
    spans = record(nested_agents, service=None)
    helper = [s for s in spans if s.name == "comment"]
    rest = [s for s in spans if s.name != "comment"]

    for batch in (rest, helper) if helper_last else (helper, rest):
        post(client, protobuf(batch))

    [run], _ = rows(store)
    assert run.agent == "triage_agent"


@pytest.mark.parametrize("server_first", [True, False], ids=["server-first", "server-last"])
def test_a_downstream_service_does_not_rename_the_run(
    client: TestClient, store: Engine, server_first: bool
) -> None:
    """The tool server's spans usually end, and arrive, before the triager's root."""
    triager, server = two_services()

    for batch in (server, triager) if server_first else (triager, server):
        post(client, protobuf(batch))

    [run], spans = rows(store)
    assert (run.agent, run.attributes["service.name"]) == ("issue-triager", "issue-triager")
    assert len(spans) == 2 and run.name == "invoke_agent triage_agent"


@pytest.mark.parametrize(
    ("body", "headers", "status"),
    [
        (b"x", {"Content-Type": "text/plain"}, 415),
        (b"\x00\xff garbage", {"Content-Type": PROTOBUF}, 400),
        (b"x" * (MAX_BODY_BYTES + 1), {"Content-Type": PROTOBUF}, 413),
        (
            gzip.compress(b"\x00" * (MAX_BODY_BYTES * 3)),
            {"Content-Type": PROTOBUF, "Content-Encoding": "gzip"},
            413,
        ),
    ],
    ids=["wrong-type", "garbage", "too-large", "zip-bomb"],
)
def test_refused_requests_store_nothing(
    client: TestClient, store: Engine, body: bytes, headers: dict[str, str], status: int
) -> None:
    response = client.post("/v1/traces", content=body, headers=headers)

    assert response.status_code == status
    assert "error" in response.json()
    with Session(store) as session:
        assert session.scalar(select(func.count()).select_from(Run)) == 0


def test_health(client: TestClient) -> None:
    assert client.get("/healthz").json() == {"status": "ok"}


def test_serve_refuses_a_public_interface(clean_env: pytest.MonkeyPatch) -> None:
    result = CliRunner().invoke(cli, ["serve", "--host", "0.0.0.0"])

    assert result.exit_code == 2
    assert "no authentication" in result.output


def test_serve_refuses_an_unmigrated_database(
    make_database: Callable[[], str], clean_env: pytest.MonkeyPatch
) -> None:
    clean_env.setenv("AFR_DATABASE_URL", make_database())  # empty: no tables

    result = CliRunner().invoke(cli, ["serve"])

    assert result.exit_code == 1
    assert "afr db upgrade" in result.output


def test_json_partial_success_is_json(client: TestClient) -> None:
    response = post(client, otlp_json(record(bad_tokens("x"))), JSON)

    assert json.loads(response.content)["partialSuccess"]["rejectedSpans"] == "1"
