# Searching runs

Find recorded runs by describing them: by meaning, and by exact words.

```bash
uv run afr embed                                   # embed settled runs that are new or changed
uv run afr search "tool timed out while fetching"  # the runs that match best
```

## `afr embed`

Embeds every settled run that's new or has changed since last time, using a local model. No API key is needed, and nothing leaves the machine. A run is **settled** once it has finished, or once it's `unfinished`: no end, and nothing changed for 30 minutes. That's a crashed agent or a lost end event, often the most interesting failures. Runs still going are left until they settle, and a settled run that changes later is embedded again.

- **Model:** bge-small-en-v1.5 through fastembed (ONNX, on the CPU), giving 384-dimension vectors. The files are Qdrant's quantised ONNX conversion, `Qdrant/bge-small-en-v1.5-onnx-Q` on Hugging Face, pinned to revision `aa8f8b06` so a changed upstream repo can't swap the model. The first run downloads it (about 67 MB) into `AFR_HOME/models` (default `~/.afr/models`).
- **What gets a vector:**
  - a short **summary** of each run: agent and name, status, what it was asked and what it answered, up to 3 root-cause failures, up to 3 refused tool calls (with who refused them) and the tools it used. For a Claude Code session, the prompt and reply come from its first and last turns;
  - each **root-cause failure** on its own, at most 20 per run (the earliest): an `error` span with no failed child. One API error usually marks the llm call, its step and its agent; only the deepest says what happened. Refused tool calls (`rejected`) aren't failures, so they're only in the summary.
- **Short texts:** bge-small reads about 2,000 characters of English, so each text stays within that, most telling parts first.
- **Redacted:** every text is built from columns that were redacted at ingest ([redaction.md](redaction.md)), and it's stored in `embeddings.content`.
- **Only what changed is embedded.** Each run's texts are rebuilt every time, but a text is embedded only if it differs from what's stored. Vectors for failures that are gone (a retried call that then worked) are removed.
- **Other models' vectors are kept.** Switching models never loses them; search uses only the current model's.

Run it after recording, or on a schedule. It prints what it did, for example `embedded 3 runs (5 vectors); 16 unchanged, 2 still running`. It commits every 50 runs, so an interrupted job keeps its progress. Each run's texts are rebuilt every time, so the job gets slower as the store grows; skipping runs that haven't changed is a follow-up.

## `afr search <query>`

One line per run, best first. Each line has the id, start time, agent, name, status, how it matched and its similarity score. When a run's best match was one of its failures, the failed span and its error are shown under it.

The same filters as `afr runs list` (`--source`, `--agent`, `--status`, `--since`, `--until`), plus `--limit` (default 10, at most 100). `--status` takes `ok`, `error`, `unset` or `unfinished`; running runs aren't embedded yet. It only reads. If settled runs haven't been embedded yet, it says how many: `2 settled runs aren't embedded yet; run afr embed`.

**Hybrid ranking.** Two searches run over the embedded texts that pass the filters:
- **Meaning:** cosine similarity between the query's vector and each text's (pgvector, HNSW index). "tool timed out while fetching" finds `ReadTimeout: read timed out` without sharing many words with it.
- **Words:** Postgres full-text search (`websearch_to_tsquery('english', ...)`, GIN index on a generated `content_tsv` column), ranked by `ts_rank_cd`. Exact terms such as `429`, a tool name or an error code always match, even where embeddings find "error 429" and "error 503" alike. Quotes, `or` and `-word` work as on a web search engine; a query of only negations (`-timeout`) matches nothing on this side.

Each side ranks up to 50 texts, or 5 per run asked for if that's more, since a run can have several texts. They're combined by reciprocal rank fusion: every text scores `1/(60 + rank)` from each side that found it. A text both sides rank well beats one that only one side found, without putting a cosine similarity and a text rank on one scale. A run takes the score of its best text.

| Column | Meaning |
|---|---|
| `match` | `both`, `meaning` or `words`: which side found the run's best text. The meaning side ranks every candidate, so while a store has fewer texts than the pool, a word match shows as `both` |
| `score` | Cosine similarity of that text to the query (-1 to 1). Results are ordered by the fused rank, so a lower similarity can sit above a higher one when the words matched too |

Vector search always returns the nearest texts, so the bottom of the list can be loosely related. Filters are applied after the HNSW scan, with pgvector's iterative scan on, so filtered searches still find enough rows. Read `match` and `score` together.

## In tests

Tests use a deterministic fake embedder (a hashed bag of words), so they're fast and offline. One smoke test runs the real model:

```bash
uv run pytest -m smoke tests/search
```
