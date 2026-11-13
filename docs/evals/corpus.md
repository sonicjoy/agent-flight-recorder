# The run corpus

200 recorded triager runs for error analysis (#13, read in #14: [error-analysis.md](error-analysis.md)). Generated on 12 Nov 2026 with [issue-triage-agent](https://github.com/sonicjoy/issue-triage-agent)'s `triager batch` and recorded by `afr serve`.

## What's in it

| Group | Cases | Model (triage / comment) | Runs | Correct | Wrong |
|---|---|---|---|---|---|
| `real-mini` | 35 real issues, twice each | gpt-5.4-mini / gpt-5.4-nano | 70 | 22 | 48 |
| `synthetic-nano` | 65 synthetic cases | gpt-5.4-nano | 65 | 18 | 47 |
| `injected-mini` | 30 real issues, tool failures injected (rate 0.3) | gpt-5.4-mini / gpt-5.4-nano | 30 | 8 | 22 |
| `real-gemini` | 35 real issues | gemini-3.5-flash-lite (free tier) | 35 | 22 | 13 |
| **Total** | | | **200** | **70** | **130** |

No run ended in an error or failed validation. The batch cost **$0.51** (Gemini's free tier counts as $0) and ran in about 1 h 15 min.

The paid groups were planned on Claude Sonnet 5.5 and Haiku 5.5. They ran on OpenAI instead because no Anthropic credit was available. Gemini ran first, unchanged.

### Which field was wrong

A run is `wrong` if any field differs from the truth, so the counts below overlap.

| Group | labels | duplicate_of | milestone | regression_of |
|---|---|---|---|---|
| `real-mini` | 39 | 20 | 4 | 0 |
| `synthetic-nano` | 41 | 14 | 14 | 4 |
| `injected-mini` | 18 | 6 | 4 | 0 |
| `real-gemini` | 12 | 1 | 0 | 0 |

### Injected tool failures

In `injected-mini`, 23 of the 30 runs had at least one tool call broken: 36 failures, nearly all on `search_issues`. By kind: bad-data 12, timeout 11, noise 10, empty 3. 17 of those 23 runs were wrong, against 5 of the 7 untouched ones. Both rates are high, so the injected failures add little beyond mini's own mistakes; #14 should read whether the agent noticed bad data.

### Gemini's rate limits

At one run every 30 seconds, Gemini's free tier returned no 429s. It did return five 503s ("high demand"), which the batch now retries. That pace is safe for the CI eval suite (#19).

### A first look at the failures

Labels dominate everywhere. Before the proper error analysis in #14:
- **Over-labelling.** gpt-5.4-mini adds labels the truth doesn't have, e.g. `documentation` on #1 "Set up Python tooling and CI", and `evals` and `infra` on #2 "Provider-switchable LLM config".
- **Missed priority labels.** Gemini misses `stretch` on #21, a stretch goal the issue body doesn't call optional.
- **False duplicates of related issues.** For example, #12 (CLI list/show/replay) is called a duplicate of #29 (replay UI), and #20 (compare with promptfoo) a duplicate of #15 (eval harness). Related work isn't the same work.

## Where the truth comes from

The agent never sees it. Each run is graded after it ends, against the triager's bundled dataset:

- **Real issues:** labels, milestone and duplicates come from this repo's GitHub issues, read-only via `triager export`. The issues were generated with Claude Code as the project's plan. A duplicate is an issue with a comment starting "Duplicate of #N", so an unmarked issue reads as having none. So "wrong" means "disagrees with how the issue was labelled on GitHub". Some disagreements may be the labels' fault, not the agent's: #14 should keep those apart.
- **Synthetic cases:** truth written for the project in `synthetic.yaml`, drafted with Claude.

The counts on this page are the grades as recorded. #14 found some truths wrong and fixed them: re-graded, the corpus is 81 correct and 119 wrong ([error-analysis.md](error-analysis.md#what-was-done-about-the-eval-set)).

## Finding runs

Each run's root span carries its grade: `triager.grade` (`correct`, `wrong` or `failed`) and one `ok` or `wrong` per field (`triager.grade.labels`, `.milestone`, `.duplicate_of`, `.regression_of`). It also has `triager.batch.group`, `.repeat`, `.attempt`, `triager.case`, `triager.inject` and `triager.triage_model`. OTel ingest copies root span attributes onto the run (since #13), so they're on `runs.attributes` too.

```sql
-- wrong runs, newest first (the four corpus groups; trial runs use other group names)
SELECT id, name, attributes->>'triager.batch.group' AS grp, attributes->>'triager.case' AS issue
FROM runs
WHERE attributes->>'triager.grade' = 'wrong'
  AND attributes->>'triager.batch.group' IN ('real-mini', 'synthetic-nano', 'injected-mini', 'real-gemini')
ORDER BY started_at DESC;

-- wrong per field, by group
SELECT attributes->>'triager.batch.group' AS grp, field, count(*)
FROM runs, LATERAL (VALUES
  ('labels', attributes->>'triager.grade.labels'),
  ('milestone', attributes->>'triager.grade.milestone'),
  ('duplicate_of', attributes->>'triager.grade.duplicate_of'),
  ('regression_of', attributes->>'triager.grade.regression_of')) AS f(field, grade)
WHERE grade = 'wrong' AND attributes->>'triager.batch.group' IN ('real-mini', 'synthetic-nano', 'injected-mini', 'real-gemini')
GROUP BY 1, 2 ORDER BY 1, 3 DESC;
```

Then use `uv run afr runs show <id>` or `uv run afr replay <id>` for one run, or `uv run afr search "duplicate of a related issue"` across them.

**Older runs:** the 35 Gemini runs came in before the copying existed. Their `triager.*` attributes (only those) were copied onto the run once, by hand:

```sql
UPDATE runs r SET attributes = r.attributes || (
  SELECT jsonb_object_agg(key, value) FROM jsonb_each(s.attributes) WHERE key LIKE 'triager.%')
FROM spans s
WHERE s.run_id = r.id AND s.parent_id IS NULL AND s.attributes ? 'triager.grade'
  AND NOT r.attributes ? 'triager.grade';
```

**Decision and truth (#14):** since issue-triage-agent#9, the root span also has `triager.decided.<field>` and `triager.expected.<field>` for labels, milestone, duplicate_of and regression_of (labels a sorted list; `(none)` when empty), which `afr label` shows side by side. The 200 corpus runs came before that. Their values were added once, on the run row and the root span, by a local script: `expected` from the triager's dataset by `triager.case`, `decided` from the last `triage` step's output. Re-grading the 200 decided values against the expected ones gave every run its stored grade.

**Extra traces that aren't corpus runs:** a retried attempt leaves its own error trace (`triager.batch.attempt` > 1, or an earlier attempt that hit a 503). The manifest is the list of record: count the latest line per key, or runs with `triager.grade` in the four corpus groups (a few trial runs from testing the batch have other group names, such as `smoke-nano`). The batch's first pass had 5 Gemini runs end on 503s; they were retried and completed once the 503 retry fix landed (issue-triage-agent#7).

## Regenerating it

In issue-triage-agent, with `afr serve` running:

```bash
uv run triager batch batches/afr-corpus.yaml --record http://127.0.0.1:4318
```

- **Manifest:** `runs/batch-afr-corpus.jsonl`, one line per run: case, model, repeat, seed, status, grade, cost and trace id. The trace id is the recorder's `runs.external_id`.
- **Resuming:** running the batch again skips runs already done and retries errors.
- **Spend:** capped at $10 by the plan.
- **Repeatable:** the samples and injected failures are seeded, so a re-run asks the same questions. The models' answers vary; that's what the repeats measure.
