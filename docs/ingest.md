# Ingest: recording OpenTelemetry traces

Any agent that exports OpenTelemetry traces over OTLP/HTTP can be recorded, with no recorder-specific code. One trace becomes one run; its spans become the run's span tree.

```bash
uv run afr serve                 # POST http://127.0.0.1:4318/v1/traces
```

Then point an exporter at it, for example with the standard variable:

```bash
export OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:4318
```

The issue triager does this with `triager run --record http://127.0.0.1:4318`.

## The endpoint

| | |
|---|---|
| `POST /v1/traces` | OTLP/HTTP trace export: `application/x-protobuf` (what exporters send by default) or `application/json` (OTLP/JSON, with hex trace and span ids) |
| `Content-Encoding` | `gzip`, or none |
| Size limit | 10 MB, measured after decompression too, so a small gzip body can't expand into gigabytes |
| Response | An `ExportTraceServiceResponse` in the request's format. If some traces couldn't be stored, `partial_success` gives the number of rejected spans and why |
| `GET /healthz` | `{"status": "ok"}` |
| Refused | 415 for any other content type, 400 for a body that can't be decoded (OTLP/JSON must be an object), 413 for one over the limit, 401 without a valid API key when keys are configured (nothing is read). Nothing is stored |
| Database unavailable | 503 with `Retry-After: 5`, so the exporter sends the request again. Traces already written by then are updated on the retry, not duplicated. It counts only when the error is about the moment, not the trace: the database can't be reached, is out of connections or resources, was shut down or timed the statement out (SQLSTATE classes 08, 53, 57, 58), or a lock, a deadlock or a serialization failure got in the way (class 40, 55P03, 55006), or it is read-only or its snapshot is too old (25006, 25P03, 72000). An error about the trace, such as an id too long for an index (54000) or a deferred constraint broken at commit (40002), rejects that trace instead, since a retry would fail the same way for ever. Hook events are treated the same way |

`afr serve` listens on `127.0.0.1:4318`, the standard OTLP/HTTP port, so exporters work with their defaults, and refuses to start if the database has migrations it hasn't run (`afr db upgrade`).

## API keys

Locally the API needs no key. A deployed recorder needs one on every route but `/healthz` ([design](designs/api-key-auth.md)). `afr serve` decides once, at startup, and prints which:

| `AFR_API_KEY_HASHES` | Listening on | `AFR_INSECURE_NO_AUTH` | Result |
|---|---|---|---|
| `name:sha256hex` entries | anything | anything | `auth: on (N keys)`: a request without `Authorization: Bearer <key>` gets 401, before its body is read |
| set but empty or malformed | anything | anything | refuses to start: a broken secret never means "no keys" |
| unset | loopback | anything | `auth: off (loopback, no keys configured)` |
| unset | another interface | off | refuses to start |
| unset | another interface | on | `auth: off`, with a warning: for a local container whose port is published on `127.0.0.1` only. Refused on Cloud Run (`K_SERVICE` set) |

With auth off, requests must name a loopback host (`127.0.0.1`, `localhost` or `[::1]`), or they get a 421: a web page can't reach the API by pointing its own domain name at 127.0.0.1 (DNS rebinding). With auth on, the key is the access control and any host is served.

**Making a key.** One key per caller (the hook, the triager), so one can be revoked alone. Run this yourself; it prints the key and the entry for the server:

```bash
python -c "import secrets, hashlib; k = 'afr_' + secrets.token_urlsafe(32); print(k); print('NAME:' + hashlib.sha256(k.encode()).hexdigest())"
```

Replace `NAME` with the caller's name and add the entry to `AFR_API_KEY_HASHES` (comma-separated). The key goes to the caller only: the hook takes it as `AFR_API_KEY` in its environment ([claude-code.md](claude-code.md#settings)), an OpenTelemetry exporter as

```bash
OTEL_EXPORTER_OTLP_HEADERS=Authorization=Bearer%20<key>   # %20: the space, URL-encoded
```

in its process environment, with an `https://` endpoint so the key never crosses the network in clear. An exporter drops a batch it gets a 401 for, so check the key first. The server stores and logs only hashes, and a key that turns up in a recorded session is redacted (`afr_api_key`).

**Checking a key**, against the deployed recorder: post an empty batch, which writes nothing.

```bash
curl -s -o /dev/null -w "%{http_code}\n" -X POST -H "Content-Type: application/json" \
  -H "Authorization: Bearer $AFR_API_KEY" -d '{"events": []}' https://<recorder>/v1/claude-code/events
```

200 means the key's hash is configured, 401 that it isn't. A recorder without keys answers 200 to anything, so this means something only against a keyed one. It doesn't check how an exporter sends the key: a mistyped header shows only as 401s in the exporter's log.

**Revoking a key.** Remove its entry in a new version of the secret and redeploy. When deployed, the secret is pinned to a version, never `latest`: Cloud Run reads `latest` each time an instance starts, so a bad edit would stop the running service at its next cold start.

## What happens to a request

1. **Decode.** Protobuf, or OTLP/JSON with its hex ids converted.
2. **Group the spans by trace.** One trace becomes one run.
3. **Map each span** to the store's model ([`ingest/mapping.py`](../src/flight_recorder/ingest/mapping.py)). This reads both OpenTelemetry GenAI (`gen_ai.*`) and OpenInference (`openinference.span.kind`, `llm.*`, `input.value`) attributes. The mapping table is in [schema.md](schema.md#mapping-sources-onto-the-schema).
4. **Record.** Each trace goes through `store.write.record_run()` in its own transaction. That function **redacts** every free-text field ([redaction.md](redaction.md)), prices the model calls and totals the run.

A trace that can't be mapped or stored, for example one with a negative token count or a value Postgres won't take (a NUL character, NaN, a span that ends before it starts, a token count too big to store or price), is rejected on its own; the others are still stored. The same holds for each Claude Code hook event. An error no trace should cause, such as a bug in the recorder or a database behind on its migrations (`relation does not exist`), is not a rejection: the recorder logs it, with the exception's type, SQLSTATE and where in the code it was raised but never its message (which can quote the data), and answers 503 with `Retry-After: 5`, so the exporter or hook keeps the data and sends it again once the cause is fixed. Traces before it in the request are already stored. An attribute holding JSON nested too deeply to parse is kept as text. The rejection message names the fields that failed, never their values, because they may be sensitive.

### Traces arrive in pieces

Exporters send spans in batches as they finish, so a trace's children usually arrive before its root. Each batch is merged into the run:
- A child is linked to its parent once the parent arrives.
- The run's name, status and end time come from the batch holding the root span.
- The run's agent is the `service.name` of the service that sent the root span. A trace can cross services: if the agent calls an instrumented tool server, the server exports its spans in the same trace under its own `service.name`. So a batch without the root only fills in an agent and attributes that aren't stored yet, and the root's batch replaces them.
- Without a `service.name`, the agent falls back to an agent's name, and only the batch holding the root sets it: a batch without the root can't tell the outermost agent from a helper.
- The run covers the earliest start and latest end seen.

### The run's fields

| Field | From |
|---|---|
| `name`, `status`, `ended_at` | The root span (no parent). Until it arrives: unknown |
| `agent` | The application: the root span's resource `service.name`. The agents it ran (`triage_agent`, `comment_writer`) are spans of kind `agent`. If `service.name` is missing or the SDK's default `unknown_service`, the agent nearest the root (`gen_ai.agent.name` or `agent.name`) among the spans that arrive with the root |
| `attributes` | The root span's resource attributes, such as `service.name` and `triager.engine`. Until the root arrives, those of the batches so far |

A span's prompt and output content (`gen_ai.input.messages`, `input.value` and so on) goes into `input` and `output`, not also into `attributes`. If a span has more than one source for the same side, only the one used is moved; the others stay in `attributes`. Events (exceptions included) and links go into `attributes.events` and `attributes.links`.

## Checked against real runs

The issue triager recorded through this endpoint with both its versions: LangGraph with OpenInference's LangChain instrumentor, and the OpenAI Agents SDK with its own OpenInference instrumentor. For each run, the stored tokens and cost matched what the triager reported. Three problems showed up in those real runs and are fixed:
- **One triage became two runs.** LangGraph resumes after the human review in a second call, which started a second trace. The triager now wraps the whole triage in one root span.
- **A failed run looked successful.** The SDK's own root span doesn't see the exception. The triager's root span records it.
- **The wrong agent was named.** The run was attributed to its helper `comment_writer`, the most frequent agent name. The run's agent is now the application (`service.name`); its agents stay in the span tree.
