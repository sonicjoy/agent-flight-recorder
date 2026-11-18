# API key auth for the ingest API (#25)

- **Author:** Joe Luo
- **Status:** Approved
- **Date:** 2026-11-18

--------------------------------------------------------------------------------

## Overview

The recorder's API is about to leave the owner's machine: #26 puts it in a container and #27 deploys it to Cloud Run with Cloud SQL. Today its only protection is that it listens on loopback and answers 421 to any request not addressed to a loopback host. Neither works once it is deployed.

The API has three routes today: `POST /v1/traces` (OpenTelemetry trace exports, OTLP/HTTP), `POST /v1/claude-code/events` (events from the Claude Code hook) and `GET /healthz`. Both POST routes cap a request at 10 MB and answer 413 above that. The callers of the deployed instance will be the owner's own agents: the Claude Code hook, the issue triager's OpenTelemetry exporter, and the owner through MCP.

This design adds API keys: random keys, one per caller, stored on the server only as SHA-256 hashes in one setting (`AFR_API_KEY_HASHES`, from Secret Manager when deployed), and sent as `Authorization: Bearer <key>`. The server decides once, at startup, whether auth is on. It refuses to start when it would listen beyond loopback without keys, unless the local compose file has set an explicit opt-out. Local use (tests, `afr serve`, the compose quickstart) stays key-free.

## The Problem

- Anyone who can reach the API can write runs into the store, and once read endpoints (#29) and the MCP server (#24) exist, read them. The project's review ledger (`.review/LEDGER.md` in the main checkout, a git-ignored list of open review findings) tracks this as "any local process can post events: ingest has no authentication".
- `afr serve` refuses any non-loopback `--host` because "the ingest API has no authentication yet (#25)" (`src/flight_recorder/cli/__init__.py`), and the `loopback_only` middleware (`src/flight_recorder/ingest/app.py`) answers 421 to Cloud Run's `*.run.app` host. So the API cannot be deployed at all until this lands.

## Goals

- When keys are configured, every route except `GET /healthz` answers 401 to a request without a valid key. Routes added later (reads, MCP over HTTP) are covered without anyone remembering to protect them.
- The server cannot start listening beyond loopback without keys, except through one explicit opt-out (`AFR_INSECURE_NO_AUTH`) meant for the local compose file, and that opt-out is refused on Cloud Run.
- Local use needs no key: pytest, `afr serve` on loopback, and `docker compose up` with the port published on `127.0.0.1` only.
- The server never logs a key, never echoes one in a response, and stores only hashes. Clients hold their key in their own environment, never in the repo. A key that appears in a recorded session is redacted like any other secret.
- The Claude Code hook and OTel exporters can send the key, and the owner can check that a key's hash is configured on the server before relying on it. A refused key keeps the hook's events in its spool and says so in `hook.log`.

## Non-Goals

- Revoking a key within seconds. Revoking means removing its hash from the secret and redeploying, which takes minutes.
- A key table, a migration, or a key-management command.
- Separate read and write keys, or scopes. All callers are the owner's.
- Anonymous read access for the replay UI. #29 decides that, as an explicit change to the open-route list.
- Rate limiting. A 256-bit key is infeasible to brute-force, and a request without a valid key is refused before its body is read, so refusing costs almost nothing.
- Cloud Run IAM or IAP. Callers would need Google ID tokens that expire hourly, which OTel exporters and the hook can't renew.
- Secret Manager and Terraform wiring (#27), and the compose `api` service and Dockerfile (#26).

## Open Questions

None. Rejected options are under Alternatives Considered.

## Critical User Journeys (CUJs)

### CUJ 1: Local development, no key

- **Given**: no `AFR_API_KEY_HASHES` set, `afr serve` with its default `--host 127.0.0.1`.
- **When**: the hook or an exporter posts to `http://127.0.0.1:4318`.
- **Then**: the request is accepted as today. Startup prints `auth: off (loopback, no keys configured)`. A request whose Host isn't loopback still gets 421 (the DNS rebinding defence).

### CUJ 2: Local compose quickstart (#26)

- **Given**: compose runs `afr serve --host 0.0.0.0` in a container with `AFR_INSECURE_NO_AUTH=1`, and publishes the port as `127.0.0.1:4318:4318`.
- **When**: the hook on the host posts to `http://127.0.0.1:4318`.
- **Then**: accepted without a key. Startup prints `auth: off (AFR_INSECURE_NO_AUTH: unauthenticated on all interfaces; publish the port on 127.0.0.1 only)`. Without the opt-out the container exits with code 2, and the message says to set `AFR_API_KEY_HASHES` or `AFR_INSECURE_NO_AUTH=1`.

### CUJ 3: Deployed on Cloud Run

- **Given**: `AFR_API_KEY_HASHES` holds `hook:<sha256>,triager:<sha256>` from a pinned Secret Manager version; Cloud Run sets `K_SERVICE`.
- **When**: the hook posts with `Authorization: Bearer <its API key>`; a stranger posts with no header.
- **Then**: the hook's request is stored. The stranger gets 401 with `WWW-Authenticate: Bearer` and a fixed body, before the server reads the request body. `/healthz` answers 200 to both. Startup prints `auth: on (2 keys)`.

### CUJ 4: Deploy with the secret missing

- **Given**: Cloud Run, `AFR_API_KEY_HASHES` unset or empty, with or without `AFR_INSECURE_NO_AUTH=1`.
- **When**: the container starts. Cloud Run requires the server to listen on `0.0.0.0` at `$PORT`, so the bind is never loopback there.
- **Then**: `serve` exits with code 2 before listening. The new revision never becomes healthy, so Cloud Run keeps serving the previous one, which still has its own pinned secret version.

### CUJ 5: Making a key for a caller

- **Given**: the owner on their laptop, adding the triager as a caller.
- **When**: they run the one-line recipe from `docs/ingest.md` themselves.
- **Then**: it prints a new key and its `triager:<sha256hex>` entry. The owner sets the key in the triager's environment as `OTEL_EXPORTER_OTLP_HEADERS=Authorization=Bearer%20<key>`, with an `https://` endpoint. They add the entry to a new version of the secret, point the service at that version, and redeploy. The hook gets its own key the same way, as `AFR_API_KEY` in the owner's environment.

### CUJ 6: Revoking one key

- **Given**: the triager's key has leaked.
- **When**: the owner removes the `triager:` entry in a new secret version and redeploys.
- **Then**: the triager's requests get 401; the hook's key still works.

### CUJ 7: Checking a key, and a stale key

- **Given**: a caller holds a key, which may no longer be configured.
- **When**: the owner posts `{"events": []}` to the deployed `/v1/claude-code/events` with that key (the check documented in `docs/ingest.md`), or the hook posts its events.
- **Then**: the check answers 200 for a configured key (it writes nothing) and 401 otherwise. Against a recorder with auth off it answers 200 for any key, so it means something only against the deployed instance. On a 401 the hook spools its events and logs `recorder refused the API key (HTTP 401); check AFR_API_KEY`, without the key. Nothing is dropped until the spool cap, as for any other setup error.

## Technical Plan (Architecture)

### Deciding the mode at startup

`serve` calls one pure function, `auth_mode(hashes, host, insecure, on_cloud_run)`, and acts on its answer:

| `AFR_API_KEY_HASHES` | Bind host | `AFR_INSECURE_NO_AUTH` | `K_SERVICE` set | Result |
|---|---|---|---|---|
| valid entries | any | any | any | auth on |
| set but empty or malformed | any | any | any | refuse (exit 2): a broken secret is an error, never "auth off" |
| unset | loopback | any | any | auth off, Host guard on |
| unset | other | off | any | refuse (exit 2) |
| unset | other | on | no | auth off, Host guard on, warning printed |
| unset | other | on | yes | refuse (exit 2) |

`AFR_INSECURE_NO_AUTH` is a boolean setting: a true value (`1`, `true`, `yes`, `on`) turns it on; unset or a false value (`0`, `false`, `no`, `off`) leaves it off; any other value stops startup. The main lock is that a non-loopback bind needs keys or the explicit opt-out. `K_SERVICE` is a second lock: it stops a Cloud Run deploy from shipping the local opt-out by mistake, for the cost of one condition. The refusal for an empty value tells a local user to delete the line rather than leave it blank.

### Enforcing it

`create_app(engine, redactor, *, auth: ApiKeys | None, allowed_hosts=...)`. `allowed_hosts` already exists: the Host-header names the guard accepts, loopback only by default. `auth` is a required keyword with no default, so a caller that forgets it gets a TypeError instead of an open server. One HTTP middleware replaces `loopback_only`:

```text
request
  │
  ├─ path == /healthz ───────────────────────────────▶ handler
  │
  ├─ auth is on:
  │     header "Authorization: Bearer <token>"
  │     and auth.match(token) ───────────────────────▶ handler
  │     otherwise ───────────────────────────────────▶ 401, WWW-Authenticate: Bearer,
  │                                                     fixed body {"error": "missing or invalid API key"}
  │
  └─ auth is off:
        Host is loopback ────────────────────────────▶ handler
        otherwise ───────────────────────────────────▶ 421 (as today)
```

The check runs before the endpoint streams the body, so an unauthenticated client can't make the server read up to the 10 MB limit. The Host guard is skipped when auth is on: the key check is the access control, and the Host header is chosen by the client (and on Cloud Run is not loopback).

`ApiKeys` (new, in `src/flight_recorder/ingest/auth.py`) holds the digests parsed from `name:sha256hex,name:sha256hex`. The name is a label for the owner, saying which entry to delete when revoking; the server doesn't use it. `match(token)` hashes the token with SHA-256 and compares it with every stored digest using `hmac.compare_digest`, checking all of them so the time taken doesn't depend on which one matched. SHA-256 without a slow key-derivation function is enough because keys are 32 random bytes, not passwords. Keys are read only from the header, never a query string, because uvicorn's access log records URLs.

### Keys

A key is `afr_` followed by `secrets.token_urlsafe(32)` (43 characters). `docs/ingest.md` gives one Python one-liner that prints a new key and its `name:sha256hex` entry; the owner runs it themselves, once per caller. A new redaction pattern, `afr_api_key`, recognises the prefix, so a key that turns up in a recorded session (a `cat .env`, an `env` listing) is stored as a placeholder, not in clear. Secret scanners can match the prefix too.

### Clients

- **Hook, today.** It posts events in chunks. When a post fails it keeps the events in a local spool (a folder under `AFR_HOME`, default `~/.afr`, capped at 20 MB, oldest dropped first) and sends them with a later hook. It drops events only for the statuses in `DROP_STATUSES` (400, 413, 422), which mean the events themselves were refused; any other failure, such as a wrong URL or a recorder that is down, is a setup error and the events wait. The hook reads only environment variables, never `.env`, and imports nothing heavy, because it runs on every Claude Code event.
- **Hook, changes.**
    - `HookConfig` gains `api_key`, read from `AFR_API_KEY`, next to its existing `key` field (the redaction key).
    - `_post` adds the header with `add_unredirected_header`, so urllib doesn't copy it to another host if the URL redirects.
    - With a key and a plain `http` URL to a non-loopback host, the hook doesn't send: the events are spooled, and the log says `AFR_HOOK_URL must be https to send the API key`.
    - On 401 or 403 the log says `recorder refused the API key (HTTP <status>); check AFR_API_KEY` instead of "unreachable". The events are spooled as today; `DROP_STATUSES` is unchanged.
- **OTel exporters** (the triager) need no code: `OTEL_EXPORTER_OTLP_HEADERS=Authorization=Bearer%20<key>`, with the space URL-encoded as the OTel spec requires, set in the exporter's process environment. An OTLP exporter drops a batch on 401 (it retries only 429, 502, 503 and 504), so a wrong key there loses spans. The key check in CUJ 7 tells the owner beforehand whether the key's hash is configured. It doesn't check how the exporter sends it: a mistyped header value (a missing `%20`, for example) still fails, and shows only as 401s in the exporter's log.

### Loopback lists

`ingest/auth.py` holds the bind-address list (`127.0.0.1`, `localhost`, `::1`), and the CLI imports it from there. `ingest/app.py` keeps the Host-header form (`[::1]`). The hook keeps its own copy, because it must not import the ingest package.

### Boundaries

- **#26**: the compose `api` service sets `AFR_INSECURE_NO_AUTH=1` and publishes `127.0.0.1:4318:4318`, never `4318:4318`. The MCP server runs over stdio against the database, which needs no key and no Host change; this is a requirement handed to #26.
- **#27**:
    - Secret Manager supplies `AFR_API_KEY_HASHES` as a pinned secret version, never `latest`. Cloud Run resolves `latest` each time an instance starts, so a bad edit would take down the serving revision at its next cold start, and a revoked key would keep working on warm instances. Each change makes a new version and redeploys.
    - Secret Manager also supplies `AFR_REDACTION_KEY`, the key that fingerprints redacted secrets (`[REDACTED:kind:fingerprint]`). The server must use the same `AFR_REDACTION_KEY` value as the hook, so the same secret gets the same placeholder from both.
    - `max-instances` 1 or 2, to cap spend.
    - A smoke test runs the key check both ways: no key gets 401, and the hook's API key gets 200.
- **#24**: an MCP server over stdio that reads the database needs no key. One mounted in this app over HTTP is covered by the middleware.
- **#29**: opening any read route to visitors is an explicit change to the open-route list.

## Risks and Mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| A key leaks (a log, a transcript, a screen share) | Its holder can write runs, and read them once reads exist | One key per caller; revoke by new secret version and redeploy (CUJ 6). Keys in recorded sessions are redacted by the `afr_api_key` pattern |
| The local opt-out is used where the network isn't trusted | With `AFR_INSECURE_NO_AUTH=1` the server is reachable by any peer on the container network, or the LAN if the port is published on all interfaces. The Host guard is the only defence, and the client chooses the Host header | Accepted for local use. Compose publishes on `127.0.0.1` only; startup prints a warning; the opt-out is refused on Cloud Run |
| The secret references `latest` | See Boundaries > #27 | #27 pins the secret version |
| An exporter is pointed at an `http://` URL | The key crosses the network in clear before any redirect to https | The docs require `https://` endpoints. The hook refuses to send a key over plain http; exporters can't be guarded from here |
| An exporter's key or header format is wrong | The exporter drops batches on 401; spans are lost, visible only in its log | The key check before relying on a key; #27's smoke test |
| A hook key stays wrong for long | Events pile up in the spool; past 20 MB the oldest are dropped | `hook.log` names the refused key, not "unreachable" |
| A blank `AFR_API_KEY_HASHES=` line in a local `.env` | `serve` refuses to start | See the mode table's note; `.env.example` has the line commented out |
| Revocation takes minutes | See Non-Goals | Accepted |

## Alternatives Considered

### Alternative A: a Postgres key table with a migration and a CLI

- **Pros**: revoking takes effect on the next request with no redeploy; each key can carry a last-used time.
- **Cons**: a migration, a CLI that has to reach Cloud SQL from the laptop, a database lookup on every request, and a decision about what a failed lookup means (401 or 503). About 3 hours, against about 1.5 hours for this design.
- **Reason for Rejection**: Rejected by author: the only thing it buys is revocation within seconds, and for a recorder that only the owner's agents call, a redeploy is acceptable.

### Alternative B: the Host guard as the switch, with no opt-out flag

- **Pros**: no new setting; a deploy without keys answers 421 to everything.
- **Cons**: the client chooses the Host header, so this is not access control. Its safety on Cloud Run rests on how the front end routes `Host: localhost`, which this design has not verified.
- **Reason for Rejection**: Rejected by author: failing closed must not depend on unverified platform behaviour.

### Alternative C: decide by bind address alone

- **Pros**: no setting at all.
- **Cons**: a container must bind 0.0.0.0, so the local compose quickstart would need a key.
- **Reason for Rejection**: Rejected by author: local use must stay key-free.

### Alternative D: one plaintext key in Secret Manager

- **Pros**: the simplest comparison.
- **Cons**: the server holds the key in clear, so a dumped environment leaks a working key.
- **Reason for Rejection**: Rejected by author: a dumped environment or a debugging session on the server would expose a working key; #25 requires keys stored hashed for that reason.

### Alternative E: a FastAPI dependency per route or per router

- **Pros**: shows in the OpenAPI schema; the usual FastAPI way.
- **Cons**: a route added outside the router is open by default, and the dependency runs only after FastAPI has routed the request, so it can't stop work the middleware avoids.
- **Reason for Rejection**: Rejected by author: a middleware protects every route by default, including future ones, with one allowlisted path.

### Alternative F: Cloud Run IAM or IAP

- **Pros**: no code in the app.
- **Cons**: callers need Google ID tokens that expire hourly, and OTel exporters and the hook can't renew them.
- **Reason for Rejection**: Rejected by author: the clients can't use it.

### Alternative G: separate read and write keys

- **Pros**: a leaked write key couldn't read data.
- **Cons**: two kinds of key to hand out, and every client is the owner's.
- **Reason for Rejection**: Rejected by author: no caller needs the split today. One key per caller already lets one leaked key be revoked alone.

### Alternative H: an `afr key new` command writing a key file the hook reads

- **Pros**: the key never appears in the terminal; the hook finds it without an environment variable.
- **Cons**: a new command, a second copy of the private-file code in `redact/key.py`, and a file-reading branch in the hook. One fixed file also pushes every caller onto one key, which defeats revoking one alone.
- **Reason for Rejection**: Rejected by author: a documented one-liner and an environment variable per caller do the same job with no new code.

### Alternative I: a separate key-check route

- **Pros**: an explicit `GET /v1/auth/check`.
- **Cons**: a route to maintain.
- **Reason for Rejection**: Rejected by author: posting `{"events": []}` to the existing hook endpoint already writes nothing and answers 200 or 401.

## Non-Functional Requirements

- **Security.** See Risks and Mitigations. Comparison is constant-time. Startup output names the mode and the number of keys, never key material or hashes. New tests check that a known key reaches neither `caplog` nor any response.
- **Performance.** One SHA-256 and one constant-time comparison per configured key, per request; no database access.
- **Rollout and rollback.** Locally nothing changes for a user with no keys. Rolling back the code returns `serve` to loopback-only. Existing hook installs keep working against a local recorder; against a keyed recorder they need `AFR_API_KEY`.

## Future Improvements

- **Revoking without a redeploy.** Cloud Run can mount the Secret Manager secret as a file that follows its latest version. The server could keep the mode fixed at startup but re-read the hashes from that file every minute, keeping the last good set if a read fails. Revoking would then mean adding a secret version. Not built now: a redeploy is enough.

--------------------------------------------------------------------------------


Estimate: 1.5h, tests and docs included. Everything below ships with #25. The work goes in five phases; each ends with `uv run pytest`, `uv run ruff check`, `uv run ruff format --check` and `uv run pyright` passing.

### Phase 1: settings and the auth module

#### 1.1 `src/flight_recorder/settings.py` (Modify)

Two fields, in the existing `Field(..., validation_alias=AliasChoices(...))` form so `env_var_names()`, the `.env.example` test and the `clean_env` fixture see them:

- `api_key_hashes: SecretStr | None = Field(default=None, validation_alias=AliasChoices("AFR_API_KEY_HASHES"), description="Comma-separated name:sha256hex entries; set means the API requires keys.")`
- `insecure_no_auth: bool = Field(default=False, validation_alias=AliasChoices("AFR_INSECURE_NO_AUTH"), description="Serve beyond loopback without keys; local containers only.")`

`insecure_no_auth` takes pydantic's boolean values (`1/0`, `true/false`, `yes/no`, `on/off`, any case); anything else is a validation error, which `serve` turns into exit 2.

#### 1.2 `.env.example` (Modify)

A new "API keys (docs/ingest.md)" section after "Redaction", every line commented out:

```text
# Hashes of the keys the API accepts, one name:sha256hex entry per caller, comma-separated.
# Unset: no keys, and `afr serve` listens on loopback only. Set it when deployed (Secret Manager).
# Don't leave it blank: an empty value stops `afr serve`.
# AFR_API_KEY_HASHES=...
# Local containers only: serve on all interfaces without keys (compose.yaml sets this).
# AFR_INSECURE_NO_AUTH=...
# Clients: the hook reads AFR_API_KEY from the environment (not this file); an OTel
# exporter takes OTEL_EXPORTER_OTLP_HEADERS=Authorization=Bearer%20<key>.
```

#### 1.3 `src/flight_recorder/ingest/auth.py` (Create)

- `LOOPBACK_BINDS = frozenset({"127.0.0.1", "localhost", "::1"})`.
- `class AuthConfigError(ValueError)`.
- `@dataclass(frozen=True) class ApiKeys`, field `digests: tuple[bytes, ...]`:
    - `@classmethod parse(cls, text: str) -> ApiKeys`. Strip the whole value; split on `,`; strip each entry. Each entry must fully match `[A-Za-z0-9_.-]+:[0-9a-fA-F]{64}`; store `bytes.fromhex(hex)`. Duplicates are allowed. An empty value, or any empty entry (a trailing comma), is malformed. Errors: `AFR_API_KEY_HASHES is set but empty; set name:sha256hex entries, or delete the line to run without keys on loopback` for an empty value, `AFR_API_KEY_HASHES entry {n} is not name:sha256hex` (1-based) otherwise. The entry's content is never in the message.
    - `match(self, token: str) -> bool`: `digest = hashlib.sha256(token.encode()).digest()`, compare with every stored digest using `hmac.compare_digest`, no early return, return whether any matched.
    - `__len__`: the number of entries.
- `@dataclass(frozen=True) class Mode`: `auth: ApiKeys | None`, `reason: str` (the text `serve` prints in brackets).
- `auth_mode(hashes: str | None, host: str, insecure: bool, on_cloud_run: bool) -> Mode`, the mode table:

| Case | Result |
|---|---|
| `hashes` is not None | `Mode(ApiKeys.parse(hashes), "1 key" / "N keys")`; parse errors propagate |
| host in `LOOPBACK_BINDS` | `Mode(None, "loopback, no keys configured")` |
| not `insecure` | raise `refusing to listen on {host} without API keys: set AFR_API_KEY_HASHES, or AFR_INSECURE_NO_AUTH=1 for a local container only` |
| `insecure` and `on_cloud_run` | raise `AFR_INSECURE_NO_AUTH is refused on Cloud Run (K_SERVICE is set): set AFR_API_KEY_HASHES` |
| `insecure` | `Mode(None, "AFR_INSECURE_NO_AUTH: unauthenticated on all interfaces; publish the port on 127.0.0.1 only")` |

#### 1.4 `tests/ingest/test_auth.py` (Create), pure tests (no database)

- `parse` accepts: one entry; two entries; a trailing newline; spaces around entries; upper-case hex; a repeated entry (`len` counts both).
- `parse` refuses, each with its message: empty; whitespace only; a trailing comma; no colon; an empty name; 63 and 65 hex characters; a non-hex character. No message contains the entry's text.
- `match`: the right key matches, a wrong key and an empty string don't, and with two entries either key matches.
- `auth_mode`: one test per row of the table above, plus `insecure` with keys set (auth on).

### Phase 2: the middleware and every `create_app` caller

#### 2.1 `src/flight_recorder/ingest/app.py` (Modify)

- `create_app(engine, redactor, *, auth: ApiKeys | None, allowed_hosts=LOOPBACK_HOSTS)`; `auth` has no default.
- `loopback_only` becomes `guard`:
    - path `/healthz`: pass.
    - `auth is not None` (not truthiness: `ApiKeys` has `__len__`): `scheme, _, token = header.partition(" ")` on the `Authorization` header; pass when `scheme.lower() == "bearer"`, `token.strip()` is not empty and has no whitespace inside, and `auth.match(token.strip())`. Otherwise `JSONResponse({"error": "missing or invalid API key"}, status_code=401, headers={"WWW-Authenticate": "Bearer"})`. The Host check is skipped.
    - `auth is None`: the existing Host check and 421.
- The docstring's "No authentication yet (#25)" paragraph becomes: "With API keys configured (`afr serve` decides at startup, ingest/auth.py), every route but /healthz needs `Authorization: Bearer <key>`. Without them the API serves loopback only, and requests must name a loopback host, so a web page can't reach it through DNS rebinding."
- FastAPI's `/openapi.json` stays and needs a key like every other route.

#### 2.2 `src/flight_recorder/cli/__init__.py` (Modify)

`serve`, in this order, before any database access:

1. `settings = Settings()`; on `pydantic.ValidationError`, print `error: invalid setting: <field names>` (names from `error.errors(include_input=False)`, never values) and exit 2.
2. `mode = auth_mode(hashes, host, settings.insecure_no_auth, "K_SERVICE" in os.environ)`, where `hashes` is `settings.api_key_hashes.get_secret_value()` when the setting is present (an empty string stays an empty string, so it is refused, not read as "no keys") and None only when it is absent; on `AuthConfigError`, print `error: <message>` and exit 2.
3. Print `auth: on (<reason>)` or `auth: off (<reason>)`.
4. Then, as today: `make_engine()`, the migrations check, the `ingest:` line, and `uvicorn.run(create_app(engine, default_redactor(), auth=mode.auth), ...)`.

Remove `LOOPBACK` (now `LOOPBACK_BINDS` in `ingest/auth.py`) and the refusal; the `--host` help becomes "Interface to listen on; beyond loopback needs API keys (docs/ingest.md)."

#### 2.3 Existing `create_app` callers (Modify)

Add `auth=None` at `tests/ingest/test_app.py:45` and `:255`, `tests/ingest/test_claude_code.py:40`, `tests/ingest/test_errors.py:70`, and `tests/ingest/test_limits.py:72`, `:96`, `:112`, `:173`.

#### 2.4 `tests/ingest/test_app.py` (Modify): `serve`

Replace `test_serve_refuses_a_public_interface` with tests that use `clean_env`, `clean_env.chdir(tmp_path)` (no `.env` is read), `clean_env.delenv("K_SERVICE", raising=False)`, and `uvicorn.run` and `make_engine` patched to fail the test if called. Each exits 2 with its message: a non-loopback host with no keys; `AFR_INSECURE_NO_AUTH=1` with `K_SERVICE` set; `AFR_API_KEY_HASHES=""`; `AFR_INSECURE_NO_AUTH=maybe`. One more: with a valid hash and `--host 0.0.0.0`, `serve` prints `auth: on (1 key)` and calls the patched `uvicorn.run`.

#### 2.5 `tests/ingest/test_auth.py` (Modify): the middleware

These use `create_app` with an engine that never connects (`create_engine("postgresql+psycopg://afr@127.0.0.1:1/afr")`), so no database is needed; no request below reaches it.

- With auth on, every route in `app.routes` except `/healthz`, called with its own method, answers 401 with `WWW-Authenticate: Bearer` for: no header; `Basic <x>`; `Bearer` with nothing after it; `Bearer a b`; a wrong key.
- With a valid key: `POST /v1/claude-code/events` with `{"events": []}` answers 200 with `accepted` 0; `POST /v1/traces` with an empty OTLP/JSON export (`{}`) answers 200. Neither touches the engine.
- The scheme matches case-insensitively (`bearer <key>`).
- `/healthz` answers 200 with auth on and off, with any Host when auth is on.
- With auth on, `Host: recorder.example.run.app` is accepted; with auth off, it gets 421.
- An unauthenticated POST with a body over 10 MB gets 401, not 413.
- The raw key appears in no response body or header produced in this file and in no `caplog` record.

#### 2.6 Stale "#25" text (Modify)

- `src/flight_recorder/ingest/claude_code.py:587`: "anything local can post to the API until #25" becomes "a caller with a valid API key, or any local process when the recorder runs without keys, can post to the API".
- `tests/ingest/test_claude_code.py:401`: drop "(until #25)".

### Phase 3: the hook

#### 3.1 `src/flight_recorder/hook/client.py` (Modify)

- `HookConfig` gains `api_key: str | None = None` as its last field; `from_env` sets `api_key=os.environ.get("AFR_API_KEY") or None`. The existing `key` (redaction key) is unchanged.
- `_post(url, events, timeout, api_key: str | None = None)`: when `api_key` is set, `request.add_unredirected_header("Authorization", f"Bearer {api_key}")`, so a redirect doesn't carry it.
- `_deliver`:
    - At the top, before `_claim_backlog`: if `config.api_key` is set, `urlsplit(config.url).scheme == "http"` and its hostname is not in `LOOPBACK`, call `_spool(config.home, events)`, log `AFR_HOOK_URL must be https to send the API key; spooled {n} ({name})`, and return. This covers the synchronous SessionEnd too.
    - `_post(config.url, chunk, timeout, config.api_key)`.
    - A new `except urllib.error.HTTPError as error` before the generic `except Exception`: for 401 and 403 set `failure = f"recorder refused the API key (HTTP {error.code}); check AFR_API_KEY"`; otherwise `failure = "HTTPError"`; then `break`.
    - The spool log line uses `failure` as is when it starts with `recorder refused`, else `recorder unreachable ({failure})` as now, followed by `; spooled {n} ({name})`.
- Docstring: "its three settings" becomes four, adding `AFR_API_KEY`; "A chunk the server refuses outright (a 4xx)" becomes "(400, 413 or 422)".

#### 3.2 Hook tests (Modify)

- The autouse fixtures that wrap `_post` take and forward `api_key`: `tests/hook/test_delivery.py:59-66` and `tests/hook/test_turn_ends.py:72-79`.
- `tests/hook/test_delivery.py`, new tests against the module's stand-in recorder, extended to record request headers:
    - with `api_key` set, the request carries `Authorization: Bearer <key>`; without it, no `Authorization` header;
    - a recorder answering 401 leaves the events spooled and `hook.log` holds `recorder refused the API key (HTTP 401); check AFR_API_KEY` and not the key;
    - a key with `url="http://recorder.example:4318/..."` sends nothing (the stand-in sees no request), spools the events, and logs the https line;
    - a first stand-in answering 302 to a second that records the headers of the GET it receives: no `Authorization` header arrives there.
- `tests/hook/test_client.py`: `from_env` reads `AFR_API_KEY`, and leaves `api_key` None when it is unset or empty.

### Phase 4: the redaction pattern

#### 4.1 `src/flight_recorder/redact/patterns.py` (Modify)

Insert, directly after `gcp_api_key` and so before `jwt`, `bearer_token` and the `secret_assignment` patterns (the first match wins):

`Pattern("afr_api_key", re.compile(r"(?<![A-Za-z0-9_-])afr_[A-Za-z0-9_-]{43}(?![A-Za-z0-9_-])"))`

#### 4.2 `tests/fake_secrets.py` (Modify)

Add `"afr_api_key": "afr" + "_" + random_text(43, TOKEN)` to `secrets_by_kind()`, built in pieces as the other entries are so secret scanners don't flag the file. `test_every_pattern_has_a_case` then covers it.

#### 4.3 `tests/test_redact.py` (Modify)

A key ending in `-`, one ending in `_`, `Authorization: Bearer <key>` and `AFR_API_KEY=<key>` are each redacted with kind `afr_api_key`.

#### 4.4 `docs/redaction.md` (Modify)

A row after `gcp_api_key`: `afr_api_key` | `afr_` + 43 URL-safe characters | the recorder's own API keys.

### Phase 5: documentation

#### 5.1 `docs/ingest.md` (Modify)

- Endpoint table, "Refused" row: add "401 without a valid API key when keys are configured; nothing is read".
- Replace the line-29 paragraph with: the mode table; the startup lines; making a key with
  `python -c "import secrets, hashlib; k = 'afr_' + secrets.token_urlsafe(32); print(k); print('NAME:' + hashlib.sha256(k.encode()).hexdigest())"`
  (the hash is of the whole key, prefix included), run by the owner, one key per caller; revoking with a new pinned secret version and a redeploy.
- The key check against the deployed instance: `POST /v1/claude-code/events` with `{"events": []}` and the key answers 200, without it 401; against a recorder without keys it answers 200 to anything.
- Exporters: `OTEL_EXPORTER_OTLP_HEADERS=Authorization=Bearer%20<key>` in the exporter's process environment, an `https://` endpoint, and the note that an exporter drops a batch on 401.

#### 5.2 `docs/claude-code.md` (Modify)

- Settings table: a row `AFR_API_KEY` | unset: no header. Needed when the recorder has keys; sent only over https or to a loopback address.
- `AFR_HOOK_URL` row: add "must be https when `AFR_API_KEY` is set, unless it is a loopback address".
- "Refused" row: after "Anything else ... keeps them", add "A 401 or 403 means a missing or wrong `AFR_API_KEY`: the events are kept and `hook.log` says `recorder refused the API key`."

#### 5.3 `docs/PLAN.md` (Modify)

- Key decisions: add item 9, "API keys are SHA-256 hashes in one pinned Secret Manager secret, one key per caller; revoking one means a new version and a redeploy (docs/designs/api-key-auth.md)." No ADR: `docs/decisions/` doesn't exist yet and no earlier decision has one.
- Week 4 table: #25 from 0.5h to 1.5h; line 185's "week 4 is about 10h" becomes "about 11h".

### Verification Plan

- **Each phase**: `uv run pytest`, `uv run ruff check`, `uv run ruff format --check`, `uv run pyright`. The ingest tests that need Postgres use the scratch test database from `tests/conftest.py`; the new auth tests need none.
- **Manual check**, run by the owner since it handles a real key, against a scratch `AFR_HOME` and the local compose database:
    - `uv run afr serve --host 0.0.0.0` with no keys exits 2.
    - With `AFR_API_KEY_HASHES` set to a fresh key's entry, `uv run afr serve --host 127.0.0.1` prints `auth: on (1 key)`; `curl -s -o NUL -w "%{http_code}" -X POST -H "Content-Type: application/json" -d "{\"events\": []}" http://127.0.0.1:4318/v1/claude-code/events` prints 401, and with `-H "Authorization: Bearer <key>"` prints 200.

--------------------------------------------------------------------------------

*Drafted with EGM*
