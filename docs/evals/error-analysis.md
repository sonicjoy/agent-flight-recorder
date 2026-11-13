# Error analysis: 50 wrong triager runs, read by hand

Hand error analysis of the [run corpus](corpus.md) ([#14](https://github.com/sonicjoy/agent-flight-recorder/issues/14)). It was done before building any metric, so that the metrics measure the failures that actually happen.

**In short:**
- **Over-labelling is the main failure:** 29 of the 49 labels on agent failures are an extra or wrong label.
- **Duplicate links are too eager:** 10 runs link a duplicate, 9 of them where there is none.
- **The eval set caused some of the "failures":** 9 of the 50 runs (18%) were wrong only because of a bad truth. Fixing the truth flipped them to correct.
- **Search finds runs by topic** (recall@5 0.98) **better than by decision** (0.60), and can't tell a wrong decision from a right one. **bge-small stays:** 0.81 overall, against 0.77 for bge-base.

## Method

**Terms.**
- **Issue numbers.** The triager triages this repo's own GitHub issues, so `#16` in an example is this repo's issue #16. They were generated with Claude Code as the project's plan, one piece of work each. Cases `s01` to `s65` are synthetic: drafted with Claude for the project and kept in the triager's `synthetic.yaml`, some as paraphrases of real issues.
- **Truth:** the answer a run is graded against: the issue's labels, milestone and links on GitHub, or the synthetic case's.
- **Labels and milestones:** `stretch` marks optional work. Milestones are the plan's weeks, e.g. "Week 1: Demo agent and foundations".

**Steps.**
- **Sample.** 50 of the corpus's 130 wrong runs (as graded before the fixes below), drawn with seed 14 in proportion to each batch group:

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
- **Labelling.** Failure modes were named while reading, not picked from a list, one per independent failure, so a run can have several. That gave **61 labels on 50 runs**, with 9 runs having more than one.
- **Settling.** The names were already consistent (`extra-` / `missed-` / `wrong-` for labels and links), so none were merged. Two labels were corrected on review: s58 became `debatable-truth`, and a #23 run's `missed-duplication` became `wrong-duplication`.
- **Truth problems.** Where the truth was wrong rather than the agent, the run got a truth-quality mode, so the two are counted apart.

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

Eval-set problems: the truth was wrong or debatable. After the fixes, 9 of these 12 runs are correct; the other 3 also have agent errors (finding 5).

| Mode | Runs | What happened |
|---|---|---|
| `bad-real-truth` | 9 | A real issue was under-labelled, and the agent's extra label was right (#1, #7, #17, #26, #33) |
| `bad-synthetic-truth` | 2 | A synthetic case's truth was wrong (s19, s65) |
| `debatable-truth` | 1 | Both answers are reasonable (s58: a duplicate of #23 or not) |

By group, where over-labelling stands out in `synthetic-nano`:

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
- **Truth, before the fix:** regression of #2.
- **Why it's wrong:** #2 (provider config) never promised retries. It's now a plain bug.

**`debatable-truth`: s58 "How good is the failure clustering?" (synthetic-nano, 6f1a5986)**
- **Truth:** duplicate of #23.
- **Agent:** no duplicate.
- **Note:** "can't decide whether it duplicates #23 … accept both".

## Findings

1. **Over-labelling is the main failure:** 29 of 49 agent labels (59%) are `extra-label` or `wrong-label`.
   - The agent picks labels for the areas an issue *touches* rather than what it *is*. It reaches for `stretch`, `recorder` and `documentation` most.
   - 13 of 18 sampled `synthetic-nano` runs are `extra-label`, against 4 of 18 `real-mini` runs. Model and data change together between those groups, so this doesn't say whether nano or the synthetic issues are to blame.
2. **Duplicate linking is too eager:** 10 runs.
   - In 9 of them, the agent linked a duplicate for an issue that has none. Its rationale is usually "overlaps with #N".
   - For the 7 runs on real issues, "none" means only that no "Duplicate of #N" comment marks one on GitHub. The issues were written as a plan, so duplicates are unlikely, and each pair was read side by side while labelling; nobody else has checked them.
   - The prompt's warning ("wrongly closing a real issue as a duplicate is worse than missing one") isn't followed.
3. **Milestone errors don't come alone:** each of the 5 runs with a wrong milestone also has a wrong label or link. In #4 and s62 the rationale shows the milestone following the label; the other three weren't traced.
4. **Injected tool failures weren't what went wrong.** In the corpus, 23 of the 30 `injected-mini` runs had a tool call broken ([corpus.md](corpus.md#injected-tool-failures)). None of the 9 sampled runs' labels is about a tool failure: they show the same failures as the clean runs. How the agent coped with broken results wasn't examined here.
5. **The eval set caused some of the "failures."** 12 runs got a truth-quality mode. After the fixes below, 9 of them (18% of the sample) are graded correct: they were wrong only because of the truth.
   - The real issues' labels were thinner than the work: `mcp`, `infra` and `documentation` were missing.
   - Two synthetic cases were wrong, and one is debatable.
   - The other 3 are still wrong because the agent also erred: the s58 run added `stretch`, the s65 run has the wrong labels and milestone, and one #33 run answered `infra` alone (see below).

## What was done about the eval set

The eval set was fixed in [issue-triage-agent#10](https://github.com/sonicjoy/issue-triage-agent/pull/10):

- **Accepted answers, not overrides.** Six real issues (#1, #7, #17, #20, #26 and #33) accept the agent's fuller label set as well as their exported truth. #20 is there because of s19, its paraphrase. The synthetic cases that paraphrase or duplicate these issues accept the same answers.
  - Overriding the truth was tried first, but would have turned 8 correct runs wrong. The search tools show past issues' truth, so those runs had copied the labels they were shown.
- **s58** accepts "no duplicate".
- **s65** is a plain bug, not a regression.

After re-grading, the corpus went from 70 correct and 130 wrong to **81 correct and 119 wrong**: 11 runs flipped, all to correct, 9 of them in the sample.

**Decided against:** one #33 run answered `infra` alone, and the labeller's note says infra "makes more sense". #33 accepts `documentation` or `documentation, infra`, so that run stays wrong: dropping `documentation` from a launch checklist isn't an acceptable answer.

## Can search find these runs?

Recall@5 of `afr search` on 20 queries ([retrieval.yaml](retrieval.yaml), checked with [`afr search-check`](../search.md#afr-search-check-queries-is-the-model-good-enough)): the share of a query's right runs in the top 5, out of as many as 5 can hold. So for a query with more than 5 right runs, it's the share of the top 5 that are right: 0.80 on "marked as a duplicate" means 4 of the top 5 linked one. It matters because the analyst (#22) and the MCP server (#24) find failures through search.

| | bge-small-en-v1.5 (stored, 384-d) | bge-base-en-v1.5 (768-d) |
|---|---|---|
| **Mean, 20 queries** | **0.81** | 0.77 |
| Topic, 11 queries | 1.00 on all but one (0.80) | 1.00 on all but two (0.80) |
| Behaviour, 9 queries | 0.00 to 1.00 | 0.00 to 1.00 |

**Decision: keep bge-small.** bge-base isn't better here, and it would need a migration to 768 dimensions and three times the download. Twenty queries is a small set, so this only says bge-base isn't worth it *now*; `afr search-check` reruns the comparison when the corpus or the summaries change.

- **The queries.** Both kinds take their right runs from every triager run with a decision (200 of the store's 218), so the sets are complete. A script writes the file from the store ([scripts/retrieval_queries.py](../../scripts/retrieval_queries.py)).
  - **Topic (11):** the issue a run triaged, e.g. "containerise the API and MCP server". The right runs are every run of that issue and its paraphrases.
  - **Behaviour (9):** what the agent decided, e.g. "marked the issue as a duplicate of another issue". The right runs are every run that decided it, right or wrong.
- **Where the models differ.** bge-base wins on "added the stretch label" (1.00 against 0.80) and "MCP work" (0.80 against 0.60). It loses on "marked as a duplicate" (0.20 against 0.80), "UI work" (0.40 against 0.60), "the week 1 milestone" (0.60 against 0.80) and "promptfoo or DeepEval" (0.80 against 1.00).
- **Both miss label pairs.** "Labelled infrastructure work as documentation too" scores 0 on both, and "an eval issue as recorder work too" 0.20. A query for two labels matches runs with either.
- **Search can't find *wrong* runs.** The first queries described failures ("called a related issue a duplicate"), with the labelled runs as the right ones, and scored about 0. A summary has what the agent decided but never the truth, so a wrong duplicate reads like a correct one. And only 41 of the 119 wrong runs are labelled, so search's other good matches counted as misses. Finding wrong runs is a job for the grade attributes (`triager.grade.*`); a search filter on them is a follow-up.

## What it means for the evals (#15 onwards)

- **Golden cases** should cover the two big failure modes: multi-area issues where only one label belongs (against over-labelling), and near-duplicate pairs that aren't duplicates (against over-eager linking). Real duplicates need issues filed by people, which this repo doesn't have.
- **Grading** should keep field-level marks. Most wrong runs have one wrong field, often a single extra label, so a run-level pass rate hides how close they are.
- **Truth quality** needs its own check before a case enters the golden set. Synthetic cases especially should be reviewed by a person.

## Caveats

- **One labeller.** A first labelling pass misread which side of the screen was the agent and which the truth. It was discarded and redone after the screen was fixed (#53); the first pass is kept in a local backup only.
- **50 runs.** Counts under 5 are anecdotes.
- **Model and source are confounded** (nano with synthetic, mini with real), as noted in finding 1.
- **The s65 run's agent errors aren't labelled:** its only mode names the bad truth, though its labels and milestone are wrong too.
- **The corpus ran on OpenAI and Gemini models,** not Claude (see [corpus.md](corpus.md)).
- **Real issues' duplicate truth is unmarked, not reviewed.** It comes from "Duplicate of #N" comments, which a maintainer writes, not the person filing. The plan's issues have one (#3 of #2), so the set says little about real-world duplicates, filed by people who didn't search first.
- **The retrieval queries were written by Claude, which also built search.** The topic queries reuse words from the issue titles, so they're easy.
