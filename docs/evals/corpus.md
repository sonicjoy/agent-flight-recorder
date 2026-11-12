# The run corpus

200 recorded triager runs for error analysis (#13, read in #14). Generated on 12 Nov 2026 with [issue-triage-agent](https://github.com/sonicjoy/issue-triage-agent)'s `triager batch` and recorded by `afr serve`.

## What's in it

| Group | Cases | Model (triage / comment) | Runs | Correct | Wrong |
|---|---|---|---|---|---|
| `real-mini` | 35 real issues, twice each | gpt-5.4-mini / gpt-5.4-nano | 70 | 22 | 48 |
| `synthetic-nano` | 65 synthetic cases | gpt-5.4-nano | 65 | 18 | 47 |
| `injected-mini` | 30 real issues, tool failures injected (rate 0.3) | gpt-5.4-mini / gpt-5.4-nano | 30 | 8 | 22 |
| `real-gemini` | 35 real issues | gemini-3.5-flash-lite (free tier) | 35 | 22 | 13 |
| **Total** | | | **200** | **70** | **130** |

No run ended in an error or failed validation. The batch cost **$0.51** (Gemini's free tier counts as $0) and ran in about an hour.

The paid groups were planned on Claude Sonnet 5.5 and Haiku 5.5. They ran on OpenAI instead because no Anthropic credit was available. Gemini ran first, unchanged.

### Which field was wrong

A run is `wrong` if any field differs from the truth, so the counts below overlap.

| Group | labels | duplicate_of | milestone | regression_of |
|---|---|---|---|---|
| `real-mini` | 39 | 20 | 4 | 0 |
| `synthetic-nano` | 41 | 14 | 14 | 4 |
| `injected-mini` | 18 | 6 | 4 | 0 |
| `real-gemini` | 12 | 1 | 0 | 0 |

Labels dominate everywhere. A first look, before the proper error analysis in #14:
- **Over-labelling.** gpt-5.4-mini adds labels the truth doesn't have, e.g. `documentation` on #1 "Set up Python tooling and CI", and `evals` and `infra` on #2 "Provider-switchable LLM config".
- **Missed priority labels.** Gemini misses `stretch` on #21, a stretch goal the issue body doesn't call optional.
- **False duplicates of related issues.** For example, #12 (CLI list/show/replay) is called a duplicate of #29 (replay UI), and #20 (compare with promptfoo) a duplicate of #15 (eval harness). Related work isn't the same work.

## Where the truth comes from

The agent never sees it. Each run is graded after it ends, against the triager's bundled dataset:

- **Real issues:** labels, milestone and duplicates come from this repo's GitHub issues, read-only via `triager export`. So "wrong" means "disagrees with how the issue was labelled on GitHub". Some disagreements may be the labels' fault, not the agent's: #14 should keep those apart.
- **Synthetic cases:** hand-written truth in `synthetic.yaml`.

## Finding runs

Each run's root span carries its grade: `triager.grade` (`correct`, `wrong` or `failed`) and one `ok` or `wrong` per field (`triager.grade.labels`, `.milestone`, `.duplicate_of`, `.regression_of`). It also has `triager.batch.group`, `.repeat`, `.attempt`, `triager.case`, `triager.inject` and `triager.triage_model`. Since this PR, OTel ingest copies root span attributes onto the run, so they're on `runs.attributes` too.

```sql
-- wrong runs, newest first
SELECT id, name, attributes->>'triager.batch.group' AS grp, attributes->>'triager.case' AS issue
FROM runs
WHERE attributes->>'triager.grade' = 'wrong'
ORDER BY started_at DESC;

-- wrong per field, by group
SELECT attributes->>'triager.batch.group' AS grp, field, count(*)
FROM runs, LATERAL (VALUES
  ('labels', attributes->>'triager.grade.labels'),
  ('milestone', attributes->>'triager.grade.milestone'),
  ('duplicate_of', attributes->>'triager.grade.duplicate_of'),
  ('regression_of', attributes->>'triager.grade.regression_of')) AS f(field, grade)
WHERE grade = 'wrong'
GROUP BY 1, 2 ORDER BY 1, 3 DESC;
```

Then use `uv run afr runs show <id>` or `uv run afr replay <id>` for one run, or `uv run afr search "duplicate of a related issue"` across them.

**Older runs:** the 35 Gemini runs came in before the copying existed. Their attributes were copied onto the run once, by hand:

```sql
UPDATE runs r SET attributes = r.attributes || (
  SELECT jsonb_object_agg(key, value) FROM jsonb_each(s.attributes) WHERE key LIKE 'triager.%')
FROM spans s
WHERE s.run_id = r.id AND s.parent_id IS NULL AND s.attributes ? 'triager.grade'
  AND NOT r.attributes ? 'triager.grade';
```

**Extra traces that aren't corpus runs:** a retried attempt leaves its own error trace (`triager.batch.attempt` > 1, or an earlier attempt that hit a 503). The manifest is the list of record: count the latest line per key, or only runs with `triager.grade`. The batch's first pass had 5 Gemini runs end on 503s; they were retried and completed once the 503 retry fix landed (issue-triage-agent#7).

## Regenerating it

In issue-triage-agent, with `afr serve` running:

```bash
uv run triager batch batches/afr-corpus.yaml --record http://127.0.0.1:4318
```

- **Manifest:** `runs/batch-afr-corpus.jsonl`, one line per run: case, model, repeat, seed, status, grade, cost and trace id. The trace id is the recorder's `runs.external_id`.
- **Resuming:** running the batch again skips runs already done and retries errors.
- **Spend:** capped at $10 by the plan.
- **Repeatable:** the samples and injected failures are seeded, so a re-run asks the same questions. The models' answers vary; that's what the repeats measure.
