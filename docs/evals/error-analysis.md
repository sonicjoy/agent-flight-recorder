# Error analysis: 50 wrong triager runs, read by hand

Hand error analysis of the [run corpus](corpus.md) (#14). It was done before building any metric, so that the metrics measure the failures that actually happen.

## Method

- **Sample.** 50 of the corpus's 130 wrong runs, drawn with seed 14 and stratified by batch group:

  | Group | Model | Wrong runs | Sampled |
  |---|---|---|---|
  | `real-mini` | gpt-5.4-mini | 48 | 18 |
  | `synthetic-nano` | gpt-5.4-nano | 47 | 18 |
  | `injected-mini` | gpt-5.4-mini, with tool failures injected | 22 | 9 |
  | `real-gemini` | gemini-3.5-flash-lite | 13 | 5 |

  ```bash
  uv run afr label --next --attr triager.grade=wrong --sample 50 --by triager.batch.group
  ```
- **Reading.** Each run was read on the `afr label` screen, which shows:
  - the agent's answer next to the truth on every field;
  - whether the truth came from a real issue or a synthetic case;
  - the issue the agent read;
  - its searches and their results;
  - its decision and rationale.
- **Labelling.** Open coding: failure modes were named freely while reading, one per independent failure, so a run can have several. That gave **61 labels on 50 runs**, with 9 runs having more than one.
- **Settling.** The names were then checked for consistency. They were already consistent (`extra-` / `missed-` / `wrong-` label, and the same pattern for links), so nothing was merged. Two labels were corrected after review: s58 became `debatable-truth`, and a #23 run's `missed-duplication` became `wrong-duplication`.
- **Truth problems.** Where the truth, rather than the agent, was wrong, the run got a truth-quality mode instead of an agent mode, so the two are counted apart.

## Taxonomy

Agent failures, grouped by cause. Counts are labels; each label is on a different run.

| Cause | Mode | Runs | What happened |
|---|---|---|---|
| **Over-labelling** | `extra-label` | 21 | The right labels plus a plausible extra one: `stretch` 9, `recorder` 6, `documentation` 6, `infra` 2, `evals` 2 |
| | `wrong-label` | 8 | A plausible label instead of the right one; 5 of 8 put `recorder` or `evals` where `stretch` belonged |
| **Over-eager linking** | `wrong-duplication` | 10 | A duplicate link where there is none (9 of 10), or the wrong issue |
| **Missed links** | `missed-regression` | 1 | A regression read as new work |
| **Under-labelling** | `missed-label` | 4 | A right label left out: `stretch` 2, `infra` 1, `analyst` 1 |
| **Milestone** | `wrong-milestone` | 5 | The wrong week; every one of these runs also has a wrong label or link |

Eval-set problems: the run was graded wrong because of the truth.

| Mode | Runs | What happened |
|---|---|---|
| `bad-real-truth` | 9 | A real issue was under-labelled, and the agent's extra label was right (#1, #7, #17, #26, #33) |
| `bad-synthetic-truth` | 2 | A synthetic case's truth was wrong (s19, s65) |
| `debatable-truth` | 1 | Both answers are reasonable (s58: a duplicate of #23 or not) |

By group:

| Mode | real-mini | synthetic-nano | injected-mini | real-gemini |
|---|---|---|---|---|
| `extra-label` | 4 | 13 | 3 | 1 |
| `wrong-duplication` | 6 | 3 | 1 | 0 |
| `wrong-label` | 4 | 1 | 1 | 2 |
| `wrong-milestone` | 2 | 3 | 0 | 0 |
| `missed-label` | 3 | 0 | 1 | 0 |
| `missed-regression` | 0 | 1 | 0 | 0 |
| `bad-real-truth` | 4 | 0 | 3 | 2 |
| `bad-synthetic-truth` | 0 | 2 | 0 | 0 |
| `debatable-truth` | 0 | 1 | 0 | 0 |

## One example of each

**`extra-label`: s47 "Tests fail on Windows" (synthetic-nano, run 823506ee)**
- **Agent:** `documentation, infra, stretch`, Week 2.
- **Truth:** `infra`, Week 1, regression of #1.
- **Rationale:** "…a CI/tooling configuration regression; label as infra and likely requires … documenting the fix".
- **Reading:** it saw the regression in its own words, but didn't link #1, and it padded the labels.

**`wrong-duplication`: #16 "Golden eval cases from labelled failures" (injected-mini, bb5582ed)**
- **Agent:** duplicate of #14.
- **Truth:** no duplicate.
- **Rationale:** "overlaps directly with the planned hand error analysis … in issue 14".
- **Reading:** overlap was treated as sameness, although the prompt says a false duplicate is worse than a missed one.

**`wrong-label`: #28 "Record the recorder's own agents" (real-mini, 65a00918)**
- **Agent:** `analyst, recorder`.
- **Truth:** `analyst, stretch`.
- **Reading:** it chose the area the work touches, the recorder, over the planning label, `stretch`.

**`wrong-milestone`: #4 "Issue fixtures for the triager" (real-mini, fb8b4a02)**
- **Agent:** `documentation`, Week 5.
- **Truth:** `demo-agent`, Week 1.
- **Rationale:** "documentation/project-data work … touches launch/readme-style workflow".
- **Reading:** the wrong label pulled the milestone with it.

**`missed-label`: #30 "Seed data and five-minute quickstart" (real-mini, 051253eb)**
- **Agent:** `documentation`.
- **Truth:** `documentation, infra`.
- **Reading:** the seed data and boot timing are infra, and it saw only the README side.

**`missed-regression`: s62 "llm-ping shows $0.00 for a model with no price" (synthetic-nano, a683d4f0)**
- **Agent:** `evals, recorder, stretch`, Week 3, no regression.
- **Truth:** `demo-agent`, Week 1, regression of #2. #2 made an unknown model's cost `None`, never $0.
- **Rationale:** "warrants a golden/eval case to prevent regressions".
- **Reading:** it used the word "regression" without linking one.

**`bad-real-truth`: #26 "Containerise the API and MCP server" (injected-mini, a11eb62c)**
- **Agent:** `infra, mcp`.
- **Truth:** `infra`.
- **Note:** "should have the mcp label".

**`bad-synthetic-truth`: s65 "Add retries when the provider returns 503" (synthetic-nano, 0096e78a)**
- **Truth:** regression of #2.
- **Why it's wrong:** #2 (provider config) never promised retries.

**`debatable-truth`: s58 "How good is the failure clustering?" (synthetic-nano, 6f1a5986)**
- **Truth:** duplicate of #23.
- **Agent:** no duplicate.
- **Note:** "can't decide whether it duplicates #23 … accept both".

## Findings

1. **Over-labelling is the main failure:** 29 of 49 agent labels (59%) are `extra-label` or `wrong-label`.
   - The agent picks labels for the areas an issue *touches* rather than what it *is*. It reaches for `stretch`, `recorder` and `documentation` most.
   - On synthetic cases, gpt-5.4-nano does this far more than gpt-5.4-mini does on real issues: 13 of 18 sampled synthetic-nano runs are `extra-label`, against 4 of 18 real-mini runs. Nano and synthetic issues are confounded here, so this can't separate model from data.
2. **Duplicate linking is too eager:** 10 runs.
   - In 9 of them, the agent linked a duplicate for an issue that has none. Its rationale is usually "overlaps with #N".
   - The prompt's warning ("wrongly closing a real issue as a duplicate is worse than missing one") isn't followed.
3. **Milestone errors don't come alone:** each of the 5 runs with a wrong milestone also has a wrong label or link. In #4 and s62 the rationale shows the milestone following the label; the other three weren't traced.
4. **Injected tool failures weren't what went wrong.** All 9 sampled injected-mini runs had timeouts, bad data, empty results and noise injected, yet none of their labels is about a tool failure. They show the same kinds of failure as the clean runs: over-labelling, over-eager linking and bad truth. How the agent coped with the injected failures wasn't examined here.
5. **A quarter of the "failures" were the eval set's fault:** 12 of 50 runs (24%).
   - The real issues' labels were thinner than the work: `mcp`, `infra` and `documentation` were missing.
   - Two of the synthetic cases (which Claude generated) were wrong, and one is debatable.
   - This is the largest single correction to the measured error rate.

## What was done about the eval set

The eval set was fixed in [issue-triage-agent#10](https://github.com/sonicjoy/issue-triage-agent/pull/10):

- **Accepted answers, not overrides.** The six under-labelled real issues (#1, #7, #17, #20, #26 and #33) accept the agent's fuller label set as well as their exported truth.
  - #20's comes from s19, its paraphrase.
  - Overriding the truth was tried first, but would have turned 8 correct runs wrong. The search tools show past issues' truth, so those runs had copied the labels they were shown.
- **s58** accepts "no duplicate".
- **s65** is a plain bug, not a regression.

After re-grading, the corpus went from 70 correct and 130 wrong to **81 correct and 119 wrong**: 11 runs flipped, all to correct. Of the 50 sampled runs, 9 are now graded correct, and their labels say why they used to be wrong.

**Decided against:** one #33 run answered `infra` alone, and the labeller's note says infra "makes more sense". #33 still accepts only `documentation, infra`, so that run stays wrong: dropping `documentation` from a launch checklist isn't an acceptable answer.

## Can search find these runs?

The failures here only help if they can be found again (#22's analyst and #24's MCP server search for them). So `afr search` was checked on 20 queries in [retrieval.yaml](retrieval.yaml), recall@5 over the 200 triager runs ([search.md](../search.md#afr-search-check-queries-is-the-model-good-enough)):

- **Topic (11 queries):** the issue a run triaged, e.g. "containerise the API and MCP server". The right runs are every run of that issue.
- **Behaviour (9 queries):** what the agent decided, e.g. "marked the issue as a duplicate of another issue". The right runs are every run whose decided attributes show it, right or wrong.

The behaviour queries started out as failure patterns ("called a related issue a duplicate"), with the labelled runs as the right ones. They scored about 0, for two reasons that are about the check, not the model:

- **A summary can't tell right from wrong.** It has what the agent decided, never the truth, so a wrong duplicate reads like a correct one.
- **Only 50 of 119 wrong runs are labelled.** Search returned unlabelled runs with the same failure, and they counted as misses.

So finding *wrong* runs is a job for the grade attributes (`triager.grade.*`), not for search. A search filter on them is a follow-up.

| | bge-small-en-v1.5 (stored, 384-d) | bge-base-en-v1.5 (768-d) |
|---|---|---|
| Mean recall@5, 20 queries | **0.82** | 0.78 |
| Topic, 11 queries | 1.00 on all | 1.00 on all but one (0.80) |
| Behaviour, 9 queries | 0.00 to 1.00 | 0.00 to 1.00 |

- **bge-base wins 2 queries and loses 4:** "added the stretch label" (1.00 against 0.80) and "MCP work" (0.80 against 0.60); it loses most on "marked as a duplicate" (0.20 against 0.80).
- **Both miss label combinations:** "labelled infrastructure work as documentation too" scores 0 on both, and "an eval issue as recorder work too" 0.20. A summary lists labels, but a query for two of them matches runs with either.

**Decision: keep bge-small.** The larger model isn't better here, and it would need a migration to 768 dimensions and three times the download. Twenty queries is a small set, so this says bge-base isn't worth it *now*; `afr search-check` can rerun the comparison when the corpus or the summaries change.

## What it means for the evals (#15 onwards)

- **Golden cases** should cover the two big failure modes: multi-area issues where only one label belongs (against over-labelling), and near-duplicate pairs that aren't duplicates (against over-eager linking).
- **Grading** should keep field-level marks. Most wrong runs have one wrong field, often a single extra label, so a run-level pass rate hides how close they are.
- **Truth quality** needs its own check before a case enters the golden set. One in four "failures" here was the eval set's fault. Synthetic cases especially should be reviewed by a person.

## Caveats

- **One labeller.** The first labelling pass read the screen's sides the wrong way round. It was discarded and redone after the screen was fixed (#53), and the first pass is kept in a local backup only.
- **50 runs.** Counts under 5 are anecdotes.
- **Model and source are confounded** (nano with synthetic, mini with real), as noted in finding 1.
- **The corpus ran on OpenAI and Gemini models,** not Claude (see [corpus.md](corpus.md)).
- **The retrieval queries were written by Claude, which also built search,** and the right runs come from attributes. The topic queries reuse words from the issue titles, so they're easy.
