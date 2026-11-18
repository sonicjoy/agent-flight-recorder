"""API keys: parsing AFR_API_KEY_HASHES, matching keys, and the startup mode table."""

from __future__ import annotations

import hashlib
import logging
import secrets
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from starlette.routing import Route
from typer.testing import CliRunner

from flight_recorder.cli import app as cli
from flight_recorder.ingest.app import OPEN_PATHS, create_app
from flight_recorder.ingest.auth import ApiKeys, AuthConfigError, auth_mode
from flight_recorder.ingest.otlp import MAX_BODY_BYTES
from flight_recorder.redact import Redactor


def new_key() -> str:
    """A stand-in key made for the test, never a real one."""
    return "afr_" + secrets.token_urlsafe(32)


def entry(name: str, key: str) -> str:
    return f"{name}:{hashlib.sha256(key.encode()).hexdigest()}"


KEY = new_key()
OTHER = new_key()
HASHES = entry("hook", KEY)


@pytest.mark.parametrize(
    "text",
    [
        HASHES,
        f"{HASHES},{entry('triager', OTHER)}",
        f"{HASHES}\n",  # what `echo ... | gcloud secrets versions add` stores
        f"  {HASHES} , {entry('triager', OTHER)}  ",
        HASHES.upper().replace("HOOK", "hook"),
    ],
)
def test_good_hash_lists_are_accepted(text: str) -> None:
    assert ApiKeys.parse(text).match(KEY)


def test_a_repeated_entry_is_counted() -> None:
    assert len(ApiKeys.parse(f"{HASHES},{HASHES}")) == 2


DIGEST = hashlib.sha256(KEY.encode()).hexdigest()


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("", "set but empty"),
        ("   \n", "set but empty"),
        (f"{HASHES},", "entry 2 is not"),
        (DIGEST, "entry 1 is not"),
        (f":{DIGEST}", "entry 1 is not"),
        (f"hook:{DIGEST[:63]}", "entry 1 is not"),
        (f"hook:{DIGEST}0", "entry 1 is not"),
        (f"hook:{DIGEST[:63]}g", "entry 1 is not"),
        (f"{HASHES},bad entry", "entry 2 is not"),
    ],
)
def test_malformed_hash_lists_are_refused_without_quoting_them(text: str, message: str) -> None:
    with pytest.raises(AuthConfigError, match=message) as error:
        ApiKeys.parse(text)
    if text.strip():
        assert DIGEST[:16] not in str(error.value) and "bad entry" not in str(error.value)


def test_only_a_configured_key_matches() -> None:
    keys = ApiKeys.parse(f"{HASHES},{entry('triager', OTHER)}")
    assert keys.match(KEY) and keys.match(OTHER)
    assert not keys.match(new_key())
    assert not keys.match("")
    assert not ApiKeys.parse(HASHES).match(OTHER)


# ---- The startup mode table ----------------------------------------------------------


def test_keys_turn_auth_on_wherever_it_listens() -> None:
    for host, insecure, cloud in [("127.0.0.1", False, False), ("0.0.0.0", True, True)]:
        mode = auth_mode(HASHES, host, insecure, cloud)
        assert mode.auth is not None and mode.reason == "1 key"
    assert auth_mode(f"{HASHES},{entry('t', OTHER)}", "0.0.0.0", False, True).reason == "2 keys"


def test_an_empty_secret_never_means_no_keys() -> None:
    for host in ("127.0.0.1", "0.0.0.0"):
        with pytest.raises(AuthConfigError, match="set but empty"):
            auth_mode("", host, True, False)


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1"])
def test_loopback_without_keys_is_open_as_before(host: str) -> None:
    mode = auth_mode(None, host, False, True)
    assert mode.auth is None and mode.reason == "loopback, no keys configured"


def test_beyond_loopback_without_keys_is_refused() -> None:
    with pytest.raises(AuthConfigError, match=r"refusing to listen on 0.0.0.0 without API keys"):
        auth_mode(None, "0.0.0.0", False, False)


def test_the_opt_out_is_refused_on_cloud_run() -> None:
    with pytest.raises(AuthConfigError, match="refused on Cloud Run"):
        auth_mode(None, "0.0.0.0", True, True)


def test_the_opt_out_serves_a_local_container_with_a_warning() -> None:
    mode = auth_mode(None, "0.0.0.0", True, False)
    assert mode.auth is None
    assert "unauthenticated on all interfaces" in mode.reason


# ---- The middleware ------------------------------------------------------------------
# An engine that never connects: no request below may reach the database.

NO_DB = create_engine("postgresql+psycopg://afr@127.0.0.1:1/afr")
REDACTOR = Redactor(b"test-key-0123456789abcdef0123456789abcdef")


def client(auth: ApiKeys | None) -> TestClient:
    return TestClient(create_app(NO_DB, REDACTOR, auth=auth), base_url="http://127.0.0.1:4318")


KEYED = ApiKeys.parse(HASHES)


def guarded_routes() -> list[tuple[str, str]]:
    app = create_app(NO_DB, REDACTOR, auth=KEYED)
    routes = [
        (method, route.path)
        for route in app.routes
        if isinstance(route, Route) and route.path not in OPEN_PATHS
        for method in sorted(getattr(route, "methods", None) or {"GET"})
        if method != "HEAD"
    ]
    assert ("POST", "/v1/traces") in routes and ("POST", "/v1/claude-code/events") in routes
    return routes


@pytest.mark.parametrize(("method", "path"), guarded_routes())
@pytest.mark.parametrize(
    "header", [None, "Basic dXNlcjpwYXNz", "Bearer", "Bearer ", "Bearer a b", "Bearer WRONG"]
)
def test_every_route_but_healthz_needs_a_key(method: str, path: str, header: str | None) -> None:
    headers = {"Authorization": header.replace("WRONG", new_key())} if header else {}
    response = client(KEYED).request(method, path, headers=headers)
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"
    assert response.json() == {"error": "missing or invalid API key"}


def test_a_valid_key_reaches_both_endpoints_and_the_key_check_writes_nothing() -> None:
    app = client(KEYED)
    for scheme in ("Bearer", "bearer"):
        headers = {"Authorization": f"{scheme} {KEY}"}
        events = app.post("/v1/claude-code/events", json={"events": []}, headers=headers)
        assert events.status_code == 200 and events.json()["accepted"] == 0
        traces = app.post(
            "/v1/traces", content=b"{}", headers=headers | {"Content-Type": "application/json"}
        )
        assert traces.status_code == 200


def test_healthz_is_open_in_both_modes() -> None:
    assert client(KEYED).get("/healthz", headers={"Host": "x.run.app"}).status_code == 200
    assert client(None).get("/healthz").status_code == 200


def test_the_host_guard_applies_only_without_keys() -> None:
    host = {"Host": "recorder.example.run.app"}
    keyed = client(KEYED).post(
        "/v1/claude-code/events",
        json={"events": []},
        headers=host | {"Authorization": f"Bearer {KEY}"},
    )
    assert keyed.status_code == 200
    assert (
        client(None).post("/v1/claude-code/events", json={"events": []}, headers=host).status_code
        == 421
    )


def test_a_caller_without_a_key_is_refused_before_the_body_is_read() -> None:
    body = b"x" * (MAX_BODY_BYTES + 1)
    response = client(KEYED).post(
        "/v1/traces", content=body, headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 401


def test_the_key_is_never_echoed_or_logged(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    app = client(KEYED)
    wrong = new_key()
    responses = [
        app.post(
            "/v1/claude-code/events", json={"events": []}, headers={"Authorization": f"Bearer {k}"}
        )
        for k in (KEY, wrong)
    ]
    for response in responses:
        assert KEY not in response.text and wrong not in response.text
        assert KEY not in str(response.headers) and wrong not in str(response.headers)
    assert KEY not in caplog.text and wrong not in caplog.text


# ---- `afr serve` ---------------------------------------------------------------------


@pytest.fixture
def serve_env(clean_env: pytest.MonkeyPatch, tmp_path: Path) -> pytest.MonkeyPatch:
    """No .env, no Cloud Run, and serve stops before the database or the server starts."""
    clean_env.chdir(tmp_path)
    clean_env.delenv("K_SERVICE", raising=False)

    def started(*_: object, **__: object) -> None:
        raise AssertionError("serve went past the auth check")

    clean_env.setattr("flight_recorder.store.db.make_engine", started)
    return clean_env


@pytest.mark.parametrize(
    ("env", "message"),
    [
        ({}, "refusing to listen on 0.0.0.0 without API keys"),
        ({"AFR_INSECURE_NO_AUTH": "1", "K_SERVICE": "afr"}, "refused on Cloud Run"),
        ({"AFR_API_KEY_HASHES": ""}, "set but empty"),
        ({"AFR_INSECURE_NO_AUTH": "maybe"}, "invalid setting: AFR_INSECURE_NO_AUTH"),
    ],
)
def test_serve_refuses_to_start_open(
    serve_env: pytest.MonkeyPatch, env: dict[str, str], message: str
) -> None:
    for name, value in env.items():
        serve_env.setenv(name, value)
    result = CliRunner().invoke(cli, ["serve", "--host", "0.0.0.0"])
    assert result.exit_code == 2
    assert message in result.output


def test_serve_with_keys_listens_beyond_loopback(serve_env: pytest.MonkeyPatch) -> None:
    serve_env.setenv("AFR_API_KEY_HASHES", HASHES)
    result = CliRunner().invoke(cli, ["serve", "--host", "0.0.0.0"])
    assert "auth: on (1 key)" in result.output
    assert isinstance(result.exception, AssertionError)  # it went on to open the database
