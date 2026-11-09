# Run store schema

Every recorded agent run, whatever produced it, lands in one Postgres schema. The models are in [`store/models.py`](../src/flight_recorder/store/models.py), and Alembic migrations are in [`store/migrations/`](../src/flight_recorder/store/migrations/versions/).

```
runs ─┬─ spans          a tree per run (parent_id)
      ├─ embeddings     run summaries and failed spans, for semantic search (#11)
      ├─ labels         failure modes, from a human, the LLM judge or the analyst (#14, #17, #22)
      └─ eval_results   one row per eval case per eval run (#15, #19)
```

## Running it

```bash
docker compose up -d       # Postgres 16 with pgvector, on 127.0.0.1:5433
uv run afr db upgrade      # apply migrations (AFR_DATABASE_URL, default: the compose database)
uv run afr db current      # show the current revision
uv run afr db revision -m "add x" --rev-id 0002   # generate a migration after changing a model
```

Tests that need the database skip when it isn't running. In CI they run against a Postgres service container, with `AFR_REQUIRE_DB=1` so they can't silently skip. `test_models_match_the_migrations` fails if a model changes without a migration.

## Design decisions

| Decision | Choice | Why |
|---|---|---|
| Canonical model | OpenTelemetry GenAI conventions, plus OpenInference | Any instrumented agent works without custom code (plan decision 1) |
| Identity | Our own UUID per row, plus the source's id in `external_id`, unique per source (`runs`) or per run (`spans`) | Re-sent data updates a row instead of duplicating it (`INSERT ... ON CONFLICT`). Claude Code ids aren't OTel ids, and don't have to be |
| Span kind | Our five kinds: `agent`, `llm`, `tool`, `step`, `other` | Queries and the UI work the same for every source. The source's own words stay in `attributes` |
| Cost | Priced at ingest from `pricing.yaml` and stored as `numeric(14,8)` | A later price change doesn't rewrite history. An unknown model's cost is `NULL`, never $0 |
| Status | OTel's span status codes: `unset`, `ok`, `error` | No translation for OTel sources. A run is still going while `ended_at` is `NULL` |
| Raw data | Every source attribute is kept in `attributes` (JSONB) | Nothing is lost in mapping, and a later mapping can re-read it |
| Redaction | `input`/`output` are stored after redaction (#10); `redactions` holds counts per pattern | The original values are never written (plan decision 3) |
| Database access | Sync SQLAlchemy 2 with psycopg 3 | Ingest volume is small. Simpler for the CLI, migrations, tests and the analyst job |
| Migrations | `afr db ...`, which calls Alembic from code: no `alembic.ini` | The same code path works locally, in CI and in a deployed container |

## Tables

### `runs`
One agent run (an OTel trace) or one Claude Code session.

| Column | Notes |
|---|---|
| `source` | `otel` or `claude_code` |
| `external_id` | OTel trace id, or the Claude Code session id. Unique with `source` |
| `agent` | Agent or service name |
| `name` | Root span name, or the session title |
| `status`, `started_at`, `ended_at` | `ended_at` is `NULL` while the run is going |
| `input_tokens`, `output_tokens`, `cost_usd` | Totals over the run's spans. `cost_usd` is `NULL` if any priced span's model is unknown |
| `attributes` | Resource and root attributes, as sent |

### `spans`
One step in a run.

| Column | Notes |
|---|---|
| `external_id` | OTel span id, or the Claude Code tool-use id. Unique within a run |
| `parent_id` / `parent_external_id` | The parent's row, and the parent's id as sent. A child can arrive before its parent: its `parent_external_id` is kept and `parent_id` is filled in when the parent arrives |
| `kind` | `agent`, `llm`, `tool`, `step` or `other`; see the mapping below |
| `name`, `model` | `model` is `<provider>:<model>` when known, the same format as `pricing.yaml` |
| `status`, `error` | `error` holds the status description or exception message |
| `input_tokens`, `output_tokens`, `cost_usd` | `cost_usd` is priced at ingest |
| `input`, `output` | Redacted message or argument JSON |
| `attributes` | Every attribute, as sent |
| `redactions` | `{"pattern": count}`. Never the values |

Times are `timestamptz`, with microsecond precision; OTel's nanoseconds are truncated. Latency is `ended_at - started_at`.

### `embeddings`
384-dimension vectors (bge-small), with an HNSW index using cosine distance. `kind` is `run_summary` (`span_id` is `NULL`) or `failed_span`. There is one row per run, span and model: the unique constraint uses `NULLS NOT DISTINCT`, so a run can't get two summaries from the same model.

### `labels`
A failure mode on a run, or on one of its spans. `source` is `human`, `judge` or `analyst`, and `confidence` is from 0 to 1.

### `eval_results`
One case's result in one eval run (`suite_run_id`), with the model and git SHA. The cases themselves are YAML files in git (plan decision 7). `run_id` links to the recorded run the case produced; it's set to `NULL` if that run is deleted, so results history survives.

## Mapping sources onto the schema

The ingest code (#8, #9) implements this mapping; this section is the contract. The GenAI conventions are still at **Development** status and have moved to their own repository ([open-telemetry/semantic-conventions-genai](https://github.com/open-telemetry/semantic-conventions-genai)). Names were checked on 9 Nov 2026, so expect changes, and keep the raw attributes.

### Span kind

| Our kind | OTel `gen_ai.operation.name` | OpenInference `openinference.span.kind` | Claude Code hook |
|---|---|---|---|
| `llm` | `chat`, `text_completion`, `generate_content`, `embeddings` | `LLM`, `EMBEDDING` | (token usage from the transcript) |
| `tool` | `execute_tool` | `TOOL`, `RETRIEVER` | `PreToolUse` / `PostToolUse` pair |
| `agent` | `invoke_agent`, `create_agent` | `AGENT` | session root, `SubagentStop` |
| `step` | `invoke_workflow`, `retrieval`, no operation name | `CHAIN`, `RERANKER`, `GUARDRAIL`, `EVALUATOR`, `PROMPT`, `DECISION` | `UserPromptSubmit` |
| `other` | any other value | any other value | any other event |

### Attributes

| Column | OTel GenAI | OpenInference |
|---|---|---|
| `model` | `gen_ai.provider.name` + `gen_ai.response.model` (else `gen_ai.request.model`) | `llm.provider` + `llm.model_name` |
| `input_tokens` | `gen_ai.usage.input_tokens` (includes cached tokens) | `llm.token_count.prompt` |
| `output_tokens` | `gen_ai.usage.output_tokens` | `llm.token_count.completion` |
| `input` | `gen_ai.input.messages`, or `gen_ai.tool.call.arguments` on tool spans | `input.value` (parsed when `input.mime_type` is `application/json`) |
| `output` | `gen_ai.output.messages`, or `gen_ai.tool.call.result` | `output.value` |
| `name` on tool spans | `gen_ai.tool.name` | `tool.name` |
| `runs.agent` | `gen_ai.agent.name`, else `service.name` | `agent.name`, else `service.name` |
| `status`, `error` | Span status code and description; exception events | Span status code and description; exception events |

Not mapped to columns, but kept in `attributes`: `gen_ai.conversation.id` / `session.id`, cache and reasoning token counts, tool call ids, request parameters (temperature and so on), finish reasons.

Provider names differ between the two conventions. For example, OTel uses `gcp.gemini` and `x_ai`, while OpenInference uses `google` and `xai`. Ingest normalises both to the provider names in `pricing.yaml` (`anthropic`, `google_genai`, `openai`), so costs can be looked up.

Older instrumentation may still send `gen_ai.system`, which was replaced by `gen_ai.provider.name`, and `gen_ai.usage.prompt_tokens` / `completion_tokens`. Ingest should accept those as fallbacks.
