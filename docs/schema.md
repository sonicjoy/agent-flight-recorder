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
uv run afr db revision -m "add x" --rev-id 0003   # generate a migration after changing a model
docker compose down        # stop Postgres; the data stays in the afr-db volume
```

`docker compose down -v` **deletes the volume and every recorded run**. Use it only to start from nothing on purpose.

Tests that need the database skip when it isn't running. In CI they run against a Postgres service container, with `AFR_REQUIRE_DB=1` so they can't silently skip. Tests never touch the `afr` database: each test session creates throwaway `afr_test_*` databases and drops them afterwards.

## Changing the schema without losing data

Migrations change a database in place: `afr db upgrade` runs only the migrations it hasn't run yet, and existing rows stay. These rules keep it that way, and the ones marked *enforced* fail CI:

| Rule | How it's kept |
|---|---|
| Change the schema by adding a migration. Never edit, rename or delete one that's on `main`, because databases that already ran it won't run it again | *Enforced*: the "Merged migrations unchanged" CI job (`scripts/check_frozen_migrations.sh origin/main` locally) |
| A model change always comes with a migration | *Enforced*: `test_models_match_the_migrations` |
| Every migration works on a database that already has rows. For example, a new required column needs a default or a backfill | *Enforced*: `test_existing_rows_survive_every_migration` seeds rows at 0001, upgrades one step at a time and checks nothing is lost |
| Upgrades are additive. `drop_table`, `drop_column`, `TRUNCATE` and `DELETE FROM` need a comment starting `# allow-data-loss:` that says why | *Enforced*: `test_upgrades_never_drop_data_without_a_reason` |
| A rename is three steps across releases: add the new column, copy the data, drop the old one later | Review |
| Downgrades can drop data, so `afr db downgrade` refuses without `--yes` | *Enforced* in the CLI |
| Back up before a risky change: `docker compose exec db pg_dump -U afr -Fc afr > afr.dump` (restore with `pg_restore`) | Habit |

## Timestamps

`created_at` is when a row was first written, and `updated_at` is its last real change. A `set_updated_at` trigger (migration 0002) maintains `updated_at` on `runs`, `spans`, `embeddings` and `labels`. Because it's a trigger rather than ORM code, it's also right for `INSERT ... ON CONFLICT DO UPDATE`, which SQLAlchemy's `onupdate` doesn't cover, and for psql. It fires only when a row actually changes, so an identical re-send leaves `updated_at` alone, and it uses `clock_timestamp()`, so changes within one transaction get their own times. `eval_results` rows are never updated, so they have only `created_at`. A test checks that every table with an `updated_at` column has the trigger.

## Design decisions

| Decision | Choice | Why |
|---|---|---|
| Canonical model | OpenTelemetry GenAI conventions, plus OpenInference | Any instrumented agent works without custom code (plan decision 1) |
| Identity | Our own UUID per row, plus the source's id in `external_id`, unique per source (`runs`) or per run (`spans`) | Re-sent data updates a row instead of duplicating it (`INSERT ... ON CONFLICT`). A span is replaced; a run is merged, so a batch that knows less (no name, status `unset`) keeps what's stored, and the run spans the earliest start and latest end seen. Claude Code ids aren't OTel ids, and don't have to be |
| Span kind | Our five kinds: `agent`, `llm`, `tool`, `step`, `other` | Queries and the UI work the same for every source. The source's own words stay in `attributes` |
| Cost | Priced at ingest from `pricing.yaml` and stored as `numeric(14,8)`, with the `pricing.yaml` version that priced it | A later price change doesn't rewrite history, and the version says which prices to recompute from. An unknown model's cost is `NULL`, never $0 |
| Status | OTel's span status codes: `unset`, `ok`, `error`; spans can also be `rejected` (a tool call refused permission, Claude Code only) | No translation for OTel sources. A run is still going while `ended_at` is `NULL` |
| Raw data | Every source attribute is kept in `attributes` (JSONB), **after redaction** | Nothing is lost in mapping, and a later mapping can re-read it |
| Redaction | Everything a source sends is redacted before mapping, not just `input`/`output`; see the [redaction contract](#redaction-contract). `redactions` holds counts per pattern | The original values are never written (plan decision 3). Prompts also arrive as attributes (`gen_ai.input.messages`), so redacting only the mapped columns would leak them |
| Integrity | A span's parent, and a label's or embedding's span, must belong to the same run (composite foreign keys on `(run_id, id)`). Tokens and cost can't be negative, and nothing ends before it starts. Every foreign key has an index | A replay can't show another run's steps, and impossible numbers fail at write time. A test checks the indexes |
| Database access | Sync SQLAlchemy 2 with psycopg 3 | Ingest volume is small. Simpler for the CLI, migrations, tests and the analyst job |
| Migrations | `afr db ...`, which calls Alembic from code: no `alembic.ini`. Additive, and never edited once merged; see [Changing the schema without losing data](#changing-the-schema-without-losing-data) | The same code path works locally, in CI and in a deployed container. Upgrades keep existing rows |

## Tables

### `runs`
One agent run (an OTel trace) or one Claude Code session.

| Column | Notes |
|---|---|
| `source` | `otel` or `claude_code` |
| `external_id` | OTel trace id, or the Claude Code session id. Unique with `source` |
| `agent` | The application that ran (OTel `service.name`); the agents it ran are spans of kind `agent` |
| `name` | Root span name, or the session title (redacted: titles can quote the prompt) |
| `status`, `started_at`, `ended_at` | `ended_at` is `NULL` while the run is going |
| `input_tokens`, `output_tokens`, `cost_usd` | Totals over the run's spans. `cost_usd` is `NULL` if any priced span's model is unknown |
| `attributes` | Resource and root span attributes, after redaction. For OTel runs the root's attributes come without its events and links, and without the content keys the mapping moves to the span's `input` and `output`; content under keys it doesn't move (older or vendor prompt keys) can come along, redacted like everything else. A resource attribute wins over a root attribute with the same key. They arrive with the root's batch. An application can say something about the whole run there, such as the triager's `triager.grade` |
| `redactions` | `{"pattern": count}` for the run's own fields (migration 0003): the largest count per pattern in one batch, since batches may repeat the same attributes. Never the values |

### `spans`
One step in a run.

| Column | Notes |
|---|---|
| `external_id` | OTel span id, or the Claude Code tool-use id. Unique within a run |
| `parent_id` / `parent_external_id` | The parent's row (always in the same run), and the parent's id as sent. A child can arrive before its parent: its `parent_external_id` is kept and `parent_id` is filled in when the parent arrives |
| `kind` | `agent`, `llm`, `tool`, `step` or `other`; see the mapping below |
| `name`, `model` | `model` is `<provider>:<model>` when known, the same format as `pricing.yaml` |
| `status`, `error` | `error` holds the status description or exception message, redacted |
| `input_tokens`, `output_tokens`, `cost_usd` | `cost_usd` is priced at ingest |
| `pricing_version` | The `version` of `pricing.yaml` that priced `cost_usd` (the date its prices last changed). `NULL` if the span isn't priced, or was priced before migration 0004 |
| `input`, `output` | Redacted message or argument JSON |
| `attributes` | Every attribute as sent, after redaction, except prompt and output content, which is in `input`/`output`. Events and links are in `attributes.events` and `attributes.links` |
| `redactions` | `{"pattern": count}`. Never the values |

Times are `timestamptz`, with microsecond precision; OTel's nanoseconds are truncated. Latency is `ended_at - started_at`.

### `embeddings`
384-dimension vectors (bge-small), with an HNSW index using cosine distance. `kind` is `run_summary` (`span_id` is `NULL`) or `failed_span`. There is one row per run, span and model: the unique constraint uses `NULLS NOT DISTINCT`, so a run can't get two summaries from the same model. `content_tsv` (migration 0006) is a generated `tsvector` of `content`, with a GIN index, for keyword search. How they're made and searched: [search.md](search.md).

### `labels`
A failure mode on a run, or on one of its spans. `source` is `human`, `judge` or `analyst`, and `confidence` is from 0 to 1. `mode_key` is generated from `failure_mode` by `afr_mode_key()`: its words (letters and digits), lowercased and sorted, so `Tool Timeout`, `tool_timeout` and `timeout-tool` are one mode (migration 0007). A run (or one span) has each human mode once: the partial unique index `uq_labels_human_mode` on `(run_id, span_id, mode_key) WHERE source = 'human'`, `NULLS NOT DISTINCT` so the whole run (no span) counts as one value. It holds even with two people labelling at once. Model labels may repeat, because each judge run is its own opinion.

### `eval_results`
One case's result in one eval run (`suite_run_id`), with the model and git SHA. The cases themselves are YAML files in git (plan decision 7). `run_id` links to the recorded run the case produced; it's set to `NULL` if that run is deleted, so results history survives.

## Redaction contract

Runs and spans enter the store through one function, `store.write.record_run()`. It redacts **every free-text field before any row is built**: every string, at any depth, in the attributes, input, output, error, names and model, and the dictionary keys too. Ingest (#8, #9) maps each source to `RunIn`/`SpanIn` in memory: span events go into `attributes` or `output`, and the resource into the run's `attributes`. It must not persist or log the raw data. A test fails if anything else in `src` writes to `runs` or `spans`. How redaction works, and its placeholders: [redaction.md](redaction.md).

Columns that hold text from the agent, and are therefore redacted:

| Table | Columns |
|---|---|
| `runs` | `agent`, `name`, `attributes` |
| `spans` | `name`, `model`, `error`, `input`, `output`, `attributes` |
| `embeddings` | `content` (built only from redacted columns) |
| `labels` | `note` (a human or model may quote the run) |
| `eval_results` | `details` |

Ids (`external_id`, `parent_external_id`), kinds, status, token counts, costs and times pass through unchanged. `runs.redactions` and `spans.redactions` record how many values each pattern replaced; the replaced values themselves are never stored.

## Mapping sources onto the schema

The ingest code implements this mapping: [`ingest/mapping.py`](../src/flight_recorder/ingest/mapping.py) for OTLP (#8, see [ingest.md](ingest.md)), and [`ingest/claude_code.py`](../src/flight_recorder/ingest/claude_code.py) for the Claude Code hook (#9). The GenAI conventions are still at **Development** status and have moved to their own repository ([open-telemetry/semantic-conventions-genai](https://github.com/open-telemetry/semantic-conventions-genai)). Names were checked on 9 Nov 2026, so expect changes, and keep the raw attributes.

### Span kind

| Our kind | OTel `gen_ai.operation.name` | OpenInference `openinference.span.kind` | Claude Code hook |
|---|---|---|---|
| `llm` | `chat`, `text_completion`, `generate_content`, `embeddings` | `LLM`, `EMBEDDING` | each model response in the session transcript, read at turn end |
| `tool` | `execute_tool` | `TOOL`, `RETRIEVER` | `PreToolUse` with `PostToolUse` or `PostToolUseFailure`, paired by `tool_use_id` |
| `agent` | `invoke_agent`, `create_agent` | `AGENT` | the session (root), each subagent (`agent_id`, ended by `SubagentStop`) |
| `step` | `invoke_workflow`, `retrieval`, no operation name | `CHAIN`, `RERANKER`, `GUARDRAIL`, `EVALUATOR`, `PROMPT`, `DECISION` | a turn: `UserPromptSubmit` to `Stop` or `StopFailure` |
| `other` | any other value | any other value | (not used) |

The Claude Code mapping in full: [claude-code.md](claude-code.md).

### Attributes

| Column | OTel GenAI | OpenInference |
|---|---|---|
| `model` | `gen_ai.provider.name` + `gen_ai.response.model` (else `gen_ai.request.model`) | `llm.provider` + `llm.model_name` |
| `input_tokens` | `gen_ai.usage.input_tokens` (includes cached tokens) | `llm.token_count.prompt` |
| `output_tokens` | `gen_ai.usage.output_tokens` | `llm.token_count.completion` |
| `input` | `gen_ai.input.messages`, or `gen_ai.tool.call.arguments` on tool spans | `input.value` (parsed when `input.mime_type` is `application/json`) |
| `output` | `gen_ai.output.messages`, or `gen_ai.tool.call.result` | `output.value` |
| `name` on tool spans | `gen_ai.tool.name` | `tool.name` |
| `runs.agent` | The root's `service.name`, else the `gen_ai.agent.name` nearest the root | `service.name`, else the `agent.name` nearest the root |
| `status`, `error` | Span status code and description; exception events | Span status code and description; exception events |

Not mapped to columns, but kept (redacted) in `attributes`: `gen_ai.conversation.id` / `session.id`, cache and reasoning token counts, tool call ids, request parameters (temperature and so on), finish reasons.

Provider names differ between the two conventions. For example, OTel uses `gcp.gemini` and `x_ai`, while OpenInference uses `google` and `xai`. Ingest normalises both to the provider names in `pricing.yaml` (`anthropic`, `google_genai`, `openai`), so costs can be looked up.

Older instrumentation may still send `gen_ai.system`, which was replaced by `gen_ai.provider.name`, and `gen_ai.usage.prompt_tokens` / `completion_tokens`. Ingest should accept those as fallbacks.
