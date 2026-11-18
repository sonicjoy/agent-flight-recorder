# The triager's golden eval suite (#16)

*   **Author:** Joe Luo
*   **Status:** Approved
*   **Date:** 2026-11-17

--------------------------------------------------------------------------------

## Overview

The issue triager gets its first eval suite in the format of the harness merged in #15 ([docs/evals/harness.md](../evals/harness.md)). A generator converts 15 cases from the triager's own dataset, chosen to cover the failure modes in the [error analysis](../evals/error-analysis.md), every field's counter-cases, and a few cases the agent gets right. The harness calls a small adapter, `issue_triager.eval:triage`, marked `@agent`.

The suite, the adapter and the generator live in the triager repo (issue-triage-agent), and `afr eval` runs from that checkout. This repo (agent-flight-recorder) changes docs only.

**Scope change from #16 as written:** the issue says to draft cases with `afr case new --from-run`, in this repo. This design converts the dataset instead (Alternative A) and puts the suite in the triager repo (Alternative C). #16's "Done when" is edited to match before coding.

### Glossary

*   **afr, agent-flight-recorder, `flight_recorder`:** this project, its repo and its Python package. "The store" is afr's Postgres database of recorded runs.
*   **The triager:** the demo LLM agent in the issue-triage-agent repo. Given a GitHub issue's title and body, it decides `labels`, `milestone`, `duplicate_of` and `regression_of`. It searches this repo's other issues with tools, validates its decision against the repo's rules (retrying once with feedback), drafts a comment with a second model call, stops at a human-review step, then writes a dry-run file. It is a LangGraph graph (`graph.py`).
*   **The dataset:** `src/issue_triager/datasets/sonicjoy__agent-flight-recorder/` in the triager repo:
    *   `issues.json`: 35 real issues exported from this repo, each with its exported truth;
    *   `synthetic.yaml`: 65 hand-written cases (ids `s01`-`s65`): paraphrases of real issues, which are duplicates of them, and tricky cases;
    *   `repo.json`: the labels and milestones; `rules.yaml`: rules such as "at least one label";
    *   `accepted_answers.yaml`: other answers that also count as right for some real issues (#14).
    Real ids are issue numbers as text (`"3"`); synthetic ids are `s` plus a number.
*   **Truth:** the answer a case is graded against (`GroundTruth`), plus its accepted answers. The agent never receives its own case's truth, but the search tools do show the other real issues' exported labels, milestone and duplicate status: they are past triage decisions, fair to show.
*   **`TriageInput`:** what the agent is given: `id`, `title`, `body`.
*   **The corpus:** 200 recorded triager runs from 12 November ([corpus.md](../evals/corpus.md)). The `real-mini` group triaged every real issue twice with gpt-5.4-mini. Each run's per-field decisions are on its run in the store.
*   **Harness terms** (from [harness.md](../evals/harness.md)):
    *   **check:** one graded assertion on one output field (`equals`, or `one_of` with a list of `values`);
    *   **baseline:** each check's accepted result (pass, fail or error), in `baseline.json`;
    *   **stale:** a case whose input changed since its saved output was made; it must be run again;
    *   **changed checks:** a case whose checks differ from the baseline's; cleared by accepting a new baseline;
    *   **the gate:** `afr eval run` fails on regressions, changed checks or stale cases; `afr eval grade` fails on any difference from the baseline.
*   **`@agent`:** the marker `afr eval run` requires on any function it calls (#15's allowlist).
*   **The two-week rule:** new dependency versions must be at least two weeks old, a supply-chain precaution this project follows.

## The Problem

The harness exists, but no real agent has a suite. So:

- **A change to the triager can't be measured against cases.** `triager batch` grades the whole dataset, but it costs a full batch, has no baseline, and reports per run, not per check.
- **The harness has never met a real agent.** Its design assumed an adapter, normalised output (labels sorted, a leading `#` stripped from issue references) and noise from a live model. None of that has been tried.
- **Later Week 3 work in [PLAN.md](../PLAN.md) acts on this suite:** the judge (#17) and the red-team set (#18) add checks and cases to it, CI evals (#19) run it live, and the provider comparison (#21) runs it on several models.

**If it is never done,** the harness has only ever graded a rule-based test agent, and #17-#19 have nothing to build on. The label-to-case step (`afr case`) is not what this suite exercises: it stays the path for agents without a dataset, and is exercised by the analyst (#22, #23).

## Goals

*   **One suite of 15 cases** in the triager repo at `evals/cases/triager/`:
    *   every agent failure mode in the error analysis at least once, and over-labelling and false duplicates at least twice;
    *   counter-cases for the fields that can be "nothing" (`milestone`, `duplicate_of`, `regression_of`), so an agent that always or never fills one fails some checks;
    *   three cases the agent got right in both `real-mini` runs.
*   **Cases generated from the dataset,** which stays the only source of truth. A committed selection file says which cases and why. A test fails if the committed cases differ from what the generator writes.
*   **An adapter the harness can call:** `issue_triager.eval:triage`.
*   **Case inputs that pin what the answer depends on:** each input carries the model to use and a hash of everything the agent can see, so a change to either makes every case stale.
*   **A first baseline that describes the triager as it is now,** from one live run on gpt-5.4-mini, read before it is accepted.
*   **CI grading with no model calls:** the triager repo's CI grades the saved outputs.

## Non-Goals

*   **Live runs in CI:** #19.
*   **All 100 dataset cases** (Alternative B). `triager batch` still grades the whole dataset.
*   **Confirmation reruns, or rerunning one case, in the harness:** still deferred (#15, Alternative P); see "Noise".
*   **Recording eval runs in the store:** the adapter starts no recording.
*   **Refactoring the triager's graph** to take a `TriageInput` instead of a whole case (Alternative H).
*   **The judge (#17), red-team cases (#18), changing the triager's prompts.**

## Critical User Journeys (CUJs)

### CUJ 1: Measure a change to the triager

*   **Given:** the triager checkout with `evals/cases/triager/` committed (cases, `suite.yaml`, `outputs.jsonl`, `baseline.json`), and an OpenAI key in `.env`.
*   **When:** the owner changes a prompt and runs `uv run afr eval run --suite triager`.
*   **Then:** each case is triaged once on the pinned model. The report shows each check's regressions and improvements and the per-field counts. The run gate will often be red from noise alone (see "Noise"), so the owner reads which checks moved: they copy `outputs.jsonl` aside, run the suite again, and believe only a change that both runs show. To keep a result, they accept the second run (`uv run afr eval grade --suite triager --accept`) only if the checks they believe moved in both; otherwise they run again. They commit outputs and baseline.

### CUJ 2: Change the dataset

*   **Given:** the owner edits the dataset.
*   **When:** they run the tests.
*   **Then:** the sync test fails and names the cases that differ; `uv run triager suite` rewrites them. What happens next depends on what changed:
    *   **What the agent can see** (a real issue's text, its exported labels, milestone or duplicate status, a label or milestone in `repo.json`, a rule): every case's `dataset` hash changes, so every case is stale until the suite is run again live and accepted.
    *   **Only a truth the agent can't see** (an accepted answer, a synthetic case's truth): the checks change, and `afr eval grade` reports changed checks until `grade --accept`. No model call is needed.
    *   The PR that changes the dataset carries the regenerated cases and, if needed, the new outputs and baseline.

### CUJ 3: CI grades the suite on every triager pull request

*   **Given:** the suite, outputs and baseline are committed.
*   **When:** a PR touches the triager repo.
*   **Then:** CI runs `uv run afr eval grade --suite triager`: no model call, no store. It fails on a stale case, a changed check, or a grading difference. A green result means the saved outputs grade as recorded, not that the current code would answer the same: a code change doesn't touch any input, so it is caught only by running the suite live (CUJ 1, and #19 later).

## Technical Plan (Architecture)

### Where things live

```text
issue-triage-agent/
  src/issue_triager/eval.py        the adapter (@agent), the dataset hash, the generator
  evals/selection.yaml             the model, and which cases, with tags and a note each
  evals/cases/triager/             written by `triager suite`:
    suite.yaml                     agent: issue_triager.eval:triage, model: <from selection>
    3.yaml, s47.yaml, ...          one case per selected dataset case
    outputs.jsonl, baseline.json   written by afr eval
  tests/test_eval.py               adapter and generator tests, and the sync test
```

`afr eval run` runs from the triager checkout, where afr is installed as a dependency, so the outputs header's `git_sha` is the triager's commit (with `-dirty` for uncommitted changes): that is what identifies the agent's code. The version field is not useful here (a fixed `0.1.0`, or `null` for an editable install). afr can't import the triager (#15, Alternative J), so the suite couldn't run from this repo anyway.

### The case input

A case's input is:

```json
{"id": "s47", "title": "...", "body": "...", "model": "openai:gpt-5.4-mini", "dataset": "1a2b3c4d5e6f"}
```

*   **`id`, `title`, `body`** are the case's `TriageInput`. `id` is the dataset id: the search tools exclude the issue being triaged by id.
*   **`model`** is the model spec for both of the triager's model calls (triage and comment), from `selection.yaml`. It is in the input because the answer depends on it: switching models makes every case stale, so a new model needs a new run and baseline instead of being graded against another model's.
*   **`dataset`** is `dataset_hash(data)`: the first 12 hex digits of SHA-256 over `flight_recorder.evals.checks.canonical` of:

    ```text
    {"corpus": [tools.summarise(c, full=True) for c in data.corpus.values(), sorted by id],
     "repo":   repo.json, parsed,
     "rules":  rules.yaml, parsed}
    ```

    *   `summarise` is the function the search tools use to show an issue, so the hash covers what the agent sees by construction: each real issue's id, title, status (which carries its state and duplicate link), sorted labels, milestone and full body.
    *   `repo.json` and `rules.yaml` are read and parsed from the dataset folder, not taken from the loaded models, so no set's order can vary between processes. A missing `rules.yaml` (it is optional) hashes as `"rules": null`.
    *   Accepted answers and synthetic cases are left out: the agent can't see them (a case's own text is in the input already).
    *   A change to `summarise` itself makes every case stale. That is wanted: it changes what the agent sees.
*   The harness hashes the whole input, so these rules give CUJ 2's behaviour.

### The adapter: `issue_triager.eval:triage`

**Contract:** `triage(input: dict) -> dict`, marked `@agent`.

*   **Returns** `{"labels": [...], "milestone": ..., "duplicate_of": ..., "regression_of": ..., "meta": {...}}`:
    *   labels sorted; `milestone` a title or `null`; `duplicate_of` and `regression_of` an issue id without `#`, or `null` (as `grading._normal` normalises them);
    *   `meta` is never checked: `{"model": <spec>, "status": "applied" or "failed", "feedback": [...]}`.
*   **Raises** (the harness records an error) when:
    *   the input's `dataset` hash differs from the loaded dataset's, the id isn't in the dataset, or its title or body differ: the suite was written for another dataset;
    *   the model can't be built (no key) or a provider error persists after retries.
*   **A failed validation is an answer, not an error.** When the agent's decision still breaks the rules after its retry, the graph ends with status `failed`. The adapter returns that last decision with `meta.status: "failed"` and the feedback, or, if there was no decision at all, the text `"(no decision)"` in every graded field. Its checks then fail, like any wrong answer: an error is for when the agent couldn't be run. (Amended in the code review: `null` fields, as first written here, would pass every check whose truth is empty, since the harness reads null, missing and `[]` as one "nothing".)

**How it runs a case:**

1.  Load the dataset (`load_dataset(DEFAULT_DATASET)`), check the hash, look up the case by id, and check the title and body.
2.  Run one attempt, `_run_once(case, data, spec)`:
    *   build the models with `flight_recorder.llm.chat_model(spec)` called twice, triage first, then comment, into a `graph.Models` (both specs equal `spec`);
    *   in a `tempfile.TemporaryDirectory`, build the graph with `graph.build_graph(case, data, models, checkpointer=InMemorySaver(), runs_dir=<the temporary folder>)`;
    *   invoke it with `{"case_id": case.id}` and a config whose `thread_id` is a new `uuid4`, then, if it stopped at the human-review interrupt, invoke again with `Command(resume=True)`;
    *   this is the path `triager run --yes` takes, without the CLI's review printout (which shows the truth) and without importing `cli.py`. The `apply` step's dry-run file goes into the temporary folder, deleted afterwards.
3.  Retry the whole attempt when `batch.classify(error)[0] == "retry"` (a rate limit that isn't a daily quota, or a 503 or 529), after 15 and then 30 seconds, waiting with a module-level `SLEEP = time.sleep` that tests replace. Any other error raises at once. The provider SDKs already retry each call twice (`chat_model` passes `max_retries=2`); this covers the longer outages the corpus saw.

The graph needs a whole `TriageCase`, truth included, although it reads only `case.id` and `case.agent_input()`. Looking the case up in the dataset gives it one without building a fake truth. So every suite case must be a dataset case; a red-team case (#18) is added to `synthetic.yaml` first.

### The generator: `triager suite`

*   `load_selection(path) -> Selection`, where `Selection` is a frozen pydantic model `{model: str, cases: list[Selected]}` and `Selected` is `{id: str, tags: list[str], note: str}` with `coerce_numbers_to_str`, so an unquoted `3` reads as `"3"`. Duplicate ids are refused. Tags are free text for people.
*   `build_suite(data, selection) -> dict[str, str]`: file name to text, for `suite.yaml` and one `<id>.yaml` per selected case, written with afr's `flight_recorder.evals.cases.dump`. An id not in the dataset raises `ValueError` naming it.
*   `triager suite` (in `cli.py`, importing `issue_triager.eval` inside the command, as `batch` does) reads `evals/selection.yaml` and writes `evals/cases/triager/`, both relative to the working folder (the repo root), with `encoding="utf-8", newline="\n"`. It deletes `*.yaml` files no longer selected, never touches `outputs.jsonl` or `baseline.json`, and on a `ValueError` prints it and exits 2.

A case:

```yaml
id: s47
tags: [extra-label, missed-regression]
note: "Saw the regression in its own words but didn't link #1; padded the labels."
input: {id: s47, title: Tests fail on Windows, body: "...", model: "openai:gpt-5.4-mini", dataset: 1a2b3c4d5e6f}
checks:
- {type: equals, field: labels, value: [infra]}
- {type: equals, field: milestone, value: "Week 1: Demo agent and foundations"}
- {type: equals, field: duplicate_of, value: null}
- {type: equals, field: regression_of, value: "1"}
```

*   **One check per field, from the truth.** When the case has accepted answers for a field (the dataset merges `accepted_answers.yaml` into real issues and their paraphrases), the check is `one_of` with `values` = the truth, then each accepted answer not equal to the truth; otherwise `equals`. Labels are sorted lists. `milestone`, `duplicate_of` and `regression_of` are `null` when empty. Labels are never empty (`rules.yaml` requires one).
*   **`suite.yaml`** is `{agent: "issue_triager.eval:triage", model: <selection.model>}`, fully generated.
*   **The sync test** runs `build_suite` on the committed selection and checks that the set of `*.yaml` names in `evals/cases/triager/` equals its keys and that each file's text (`read_text(encoding="utf-8")`) equals its value.

### The selection

`evals/selection.yaml` holds `model: openai:gpt-5.4-mini` and 15 cases:

| Case | Why | Tags |
|---|---|---|
| s47 | Padded the labels and missed the regression of #1 (error analysis) | extra-label, missed-regression |
| 16 | Overlap with #14 taken for a duplicate (error analysis) | wrong-duplication |
| 20 | Called a duplicate of #15 in the corpus | wrong-duplication |
| 28 | Chose the area (`recorder`) over the planning label (`stretch`) (error analysis) | wrong-label |
| 4 | A wrong label pulled the milestone with it (error analysis) | wrong-milestone, wrong-label |
| 30 | Saw only the README side, missed `infra` (error analysis) | missed-label |
| s62 | Used the word "regression" without linking #2 (error analysis) | missed-regression |
| 1 | Over-labelling that is now an accepted answer (`documentation, infra`) | extra-label, accepted-answer |
| s58 | Debatable duplicate of #23; both answers accepted | debatable-truth |
| 3 | The only real duplicate (of #2) | true-duplicate |
| s61 | A real regression (of #1) | true-regression |
| s50 | Out of scope: no milestone | no-milestone |
| 5, 9, 19 | Right in both `real-mini` runs | control |

*   **The rules the noise measurement applies** (before coding, "Noise"):
    *   **A control** is a real issue whose four fields are right in both `real-mini` runs, re-graded against today's truth. Candidates in order: 5, 9, 19, 10, 27, 29; the first three that qualify are used.
    *   **A failure-mode case is kept even if it is flaky**: it exists because the agent gets it wrong at least sometimes. Its flakiness goes in its note. Coverage wins over quiet.
    *   **A case is left out for flakiness only when another, steadier case covers every one of its tags.** So #12 (flaky; #16 and #20 cover false duplicates) is left out. #26 is left out as redundant, not flaky: re-graded against today's accepted answers, both its runs are right, and #1 already covers an accepted answer.
    *   **Note format:** what the corpus shows, for example `real-mini: right 1 of 2; labels wrong 1 of 2`, or `synthetic-nano: one run, wrong (gpt-5.4-nano)`, followed by why the case is in.
*   **Counter-cases:** `duplicate_of` has a value in #3 (and is `one_of ["23", null]` in s58) and `null` in the rest; `regression_of` has a value in s47, s61 and s62 and `null` in the rest; `milestone` is `null` in s50 and a week in the rest.
*   **Controls and flakiness are confirmed before coding** (see "Noise").

### Noise

*   **What to expect.** With 15 cases and 60 checks, if each check gives the same result 97% of the time, `1 - 0.97^60` ≈ 84% of live runs show at least one regression that isn't real. The corpus suggests the triager is less stable than 97%. So a red `run` gate is normal: what matters is which checks moved, and whether a second run moves them too (CUJ 1).
*   **A rough measurement, from data already recorded.** The `real-mini` group ran every real issue twice on gpt-5.4-mini. Before coding, a one-off, read-only query of the store re-grades each pair per field against today's truth and accepted answers (the 12 November grades predate #14's corrections, and the batch manifest holds only overall grades). The result picks the controls and drops flaky cases, and is recorded in each case's `note`. No script is committed.
*   **What it found** (17 November, before coding): controls 5, 9 and 19 qualify; #12 is flaky and #26 is right in both runs. Per field, the two runs of a real issue agreed on `labels` 27 of 35 times, `milestone` 33 of 35, `duplicate_of` 25 of 35 and `regression_of` 35 of 35: 86% over all four. That is far below the 97% assumed above, so nearly every live run moves some checks: noise is the normal state, and comparing two runs (CUJ 1) is the only way to read a change. Of the failure-mode cases with mini data, 4, 20 and 28 are flaky; they stay in by the rules above.
*   **What it can't show.** Two runs can show that a case is flaky, not that it is stable. The synthetic cases (s47, s50, s58, s61, s62) ran once each, on gpt-5.4-nano, so they have no mini data. #16's recorded failure was in `injected-mini` (mini, with tool failures injected). `real-mini` used nano for the comment step; the suite uses mini for both, which doesn't affect the graded fields. The suite pins gpt-5.4-mini because it is the model the real issues' pairs were run on, and the cheapest one with that data.
*   **What's still missing.** Rerunning one case, and comparing two runs, are manual. A harness change for that (a case filter, or confirmation reruns) is a follow-up, needed before #19 gates on live runs.

### The first baseline

1.  The owner is asked before spending: 15 cases on gpt-5.4-mini, at the corpus's cost of about $0.004-0.007 per case, so under $0.15.
2.  `uv run afr eval run --suite triager`, without `--accept`. Read the report: an error outcome (a missing key, a persistent rate limit) means run again; an error must not enter a baseline. A wrong answer, a failed validation included, is fine: it is what the agent does.
3.  `uv run afr eval grade --suite triager --accept` writes `baseline.json` from those saved outputs. Outputs and baseline are committed with the suite.

### Dependencies

The triager depends on afr from git (`branch = "main"`), with the commit fixed in `uv.lock` at a Week 1 commit that has no `flight_recorder.evals`. `uv lock --upgrade-package agent-flight-recorder` moves it to main's latest commit, which includes #15. afr has grown since Week 1 (the store, ingest, search: fastapi, sqlalchemy, fastembed and others), so the triager's environment and CI install grow too. That is accepted: the triager already uses afr's LLM layer and pricing, `afr eval run` must import the adapter in the same environment, and a slimmer afr install (an extra for the client parts) is a follow-up, not a reason to split the environment (Alternative I). Any newly pulled release under two weeks old gets an `exclude-newer-package` hold in the triager's `pyproject.toml`, as afr's own does.

## Risks & Mitigations

| Risk | Mitigation or acceptance |
|---|---|
| The run gate is red from noise most of the time. | Expected and documented (about 84% at 97% per check). Flaky cases left out; the owner compares two runs before believing a change. A harness rerun feature is a follow-up before #19. |
| A baseline is accepted with errors in it. | Run, read, then `grade --accept`. Errors are kept for a model that couldn't run; a failed validation is a wrong answer, not an error. |
| The cases drift from the dataset. | The sync test; the dataset hash makes a change the agent can see show up as stale cases. |
| A re-export of `issues.json` makes every case stale, and CI stays red until a paid live run. | Re-export only in a PR that also reruns and re-accepts the suite, with the old and new reports in its description. |
| CI is green although the agent's code changed. | Accepted and stated (CUJ 3): grading saved outputs can't see code. Live runs are CUJ 1 and #19. |
| The model that ran isn't the one the suite says. | The model is in every input and in `suite.yaml`, both generated; the adapter uses the input's model. |
| A transient provider error becomes an error outcome. | Two retries with backoff; anything still failing is an error to rerun before accepting. |
| The dependency bump pulls in many packages, some under two weeks old. | Checked in its own PR; holds added where needed. |
| The suite tests the triager on the dataset it was developed against. | Accepted: this is a regression suite, not a test of generalisation. |
| Security. | See Non-Functional Requirements. |

## Open Questions

*   None for the design. The pre-coding measurement applies the rules in "The selection"; it can change which controls are used and which covered cases are left out, never the coverage.

## Alternatives Considered

### Alternative A: Draft cases with `afr case new --from-run` (the issue as written)

*   **Pros:** the path any agent's suite takes; uses the labelling work.
*   **Cons:** the input would be the recorded prompt text, which the adapter would have to parse back into a title and body; checks are `equals` only, with no accepted answers; the truth is one run's label.
*   **Reason for Rejection:** the dataset has reviewed truths and accepted answers, and its `TriageInput` needs no parsing. `afr case` stays the path for agents without a dataset (the analyst, #22).

### Alternative B: All 100 dataset cases

*   **Pros:** complete coverage.
*   **Cons:** 400 checks: at 97% each, a false regression on almost every run; 100 cases is more than a PR review reads; about seven times the calls.
*   **Reason for Rejection:** a golden suite is a small reviewed sample. `triager batch` covers the whole dataset.

### Alternative C: Keep the suite in this repo

*   **Pros:** where the issue and PLAN.md put it.
*   **Cons:** this repo can't import the triager, so the suite couldn't run here; the header would record this repo's commit, so a triager change would leave no trace in the outputs.
*   **Reason for Rejection:** a suite belongs with the code it tests. This repo documents it.

### Alternative D: Copy the truth into the cases once

*   **Pros:** no generator.
*   **Cons:** two copies of every truth; each correction like #14's would be made twice, and a missed one goes unnoticed.
*   **Reason for Rejection:** the generator and the sync test keep one copy.

### Alternative E: First baseline from the corpus's recorded answers

*   **Pros:** free.
*   **Cons:** those answers came from the 12 November triager through `triager batch`, not through the adapter.
*   **Reason for Rejection:** a baseline must describe the agent and the adapter as they are now; one live run costs under $0.15.

### Alternative F: Stop at the review interrupt and return the decision

*   **Pros:** no `apply` step and no dry-run file.
*   **Cons:** the comment call still happens before the interrupt, so nothing is saved; and the approve-and-apply path, which every real run takes, would go untested.
*   **Reason for Rejection:** resuming costs one discarded file in a temporary folder.

### Alternative G: Hash only `id`, `title`, `body`, or a hand-picked list of fields

*   **Pros:** a simpler input.
*   **Cons:** a change to what the search tools show (labels, milestones, duplicate links of other issues) would leave old outputs looking fresh. A hand-picked list is a second copy of what the tools show, which drifts when `summarise` changes.
*   **Reason for Rejection:** hash `summarise`'s own output.

### Alternative H: Change the graph to take a `TriageInput`

*   **Pros:** the truth wouldn't be in the eval process at all; red-team cases wouldn't need a dataset entry.
*   **Cons:** a signature change in `graph.py` and every caller and test, for a guarantee the adapter already gives (the graph reads only the case's id and input).
*   **Reason for Rejection:** not needed for #16; worth doing if #18's cases don't fit `synthetic.yaml`.

### Alternative I: Set the marker without importing afr, and run afr in its own environment

*   **Pros:** the triager's install stays small.
*   **Cons:** `afr eval run` imports the agent, so the agent's packages must be in afr's environment anyway; two environments to keep in step.
*   **Reason for Rejection:** one environment. A slim afr extra is a follow-up.

--------------------------------------------------------------------------------

## Non-Functional Requirements

*   **Cost:** a live run is 15 call chains (up to two triage attempts with tool calls, plus a comment call each), under $0.15 on gpt-5.4-mini. `grade`, CI and the generator make no model calls.
*   **Security and privacy:** the dataset is this repo's public issues and hand-written cases. Outputs are redacted by the harness (secret shapes become placeholders) and reviewed in the PR. No key reaches CI.
*   **Rollback:** everything new is in the triager repo; this repo changes docs only.

## Detailed Implementation Plan

Three PRs, in order. In PR 2, tests are written first and shown failing.

### Before coding

*   **Edit issue #16:** "Done when" becomes: a suite of about 15 cases in the triager repo's `evals/cases/triager/`, generated from the dataset with a sync test; the adapter; a first baseline from one live run; triager CI grades the suite. Estimate 3.5 h.
*   **The noise measurement:** a read-only query of the store (as `scripts/eval_agreement.py` reads it) re-grades the `real-mini` pairs per field against today's dataset, and applies the rules in "The selection" to confirm or replace the controls and the cases left out. The results go into the selection's notes in the stated format; nothing is committed in this repo.

### PR 1 (triager): move the agent-flight-recorder pin

#### `uv.lock` (Modify)
*   `uv lock --upgrade-package agent-flight-recorder` (main's latest commit). Check every newly added or changed package's release date; any under two weeks old gets a hold.
#### `pyproject.toml` (Modify, only if a hold is needed)
*   `[tool.uv] exclude-newer-package = { <name> = "<date two weeks back>" }`, as in afr's `pyproject.toml`.
*   **Checkpoint:** `uv sync --locked`, `uv run pytest`, `uv run python -c "from flight_recorder.evals import agent"`, `uv run afr eval --help`.

### PR 2 (triager): the adapter, the generator and the suite

#### `tests/fakes.py` (Create) and `tests/test_triager.py` (Modify)
*   Move the fake chat model helpers (`FakeModel`, `scripted`, `call`, `decide`, `models`) from `test_triager.py` into `tests/fakes.py`, imported by both test files (`from fakes import ...`; `tests/` is on the import path because it holds `conftest.py`).

#### `tests/test_eval.py` (Create)
*   `dataset_hash`:
    *   changes when a real issue's body, labels, milestone or duplicate changes, or a label in `repo.json`, or a rule;
    *   doesn't change when an accepted answer or a synthetic case changes;
    *   is the same for a dataset copy with CRLF line endings; hashes a missing `rules.yaml` as `null`.
*   `triage`, with `monkeypatch.chdir(tmp_path)` and `issue_triager.eval.chat_model` patched with a `side_effect` list of fakes, triage then comment, per attempt; `issue_triager.eval.SLEEP` patched to record waits:
    *   returns sorted labels, ids without `#`, `meta.model` and `meta.status: "applied"`;
    *   returns the last decision with `meta.status: "failed"` when validation fails twice;
    *   raises on a hash mismatch, an unknown id, a changed title;
    *   a fake that raises an exception with `status_code = 503` once is retried after 15 seconds and then succeeds; one with `status_code = 401` raises at once;
    *   leaves `tmp_path` empty; is marked `@agent`.
*   `build_suite`:
    *   `one_of` with `values` where accepted answers exist, truth first, no repeats; `equals` otherwise; `null` for empty fields;
    *   refuses an unknown id and a duplicate id; reads an unquoted numeric id as text;
    *   every selected case's input round-trips through `cases.dump` and `cases.load` unchanged.
*   **The sync test:** `build_suite(load_dataset(DEFAULT_DATASET), load_selection(ROOT / "evals/selection.yaml"))` equals the files in `ROOT / "evals/cases/triager"`, where `ROOT = Path(__file__).parents[1]`; its failure message says to run `uv run triager suite`.

#### `src/issue_triager/eval.py` (Create)
*   `dataset_hash`, `triage` (`@agent`), `Selection`, `Selected`, `load_selection`, `build_suite`, as above. Imports `graph`, `dataset`, `tools`, `batch.classify` and `flight_recorder`; not `cli`.

#### `src/issue_triager/cli.py` (Modify)
*   `suite` command: `triager suite`, importing `issue_triager.eval` inside the function.

#### `evals/selection.yaml` (Create), then `evals/cases/triager/` (generated)
*   The selection as confirmed by the noise measurement; then `uv run triager suite`.
*   **Checkpoint:** `uv run pytest`.

#### `evals/cases/triager/outputs.jsonl`, `baseline.json` (Create, by the live run)
*   The first-baseline procedure, after the owner approves the spend.

#### `.github/workflows/ci.yml` (Modify)
*   After Pytest: "Eval suite (no model calls)": `uv run afr eval grade --suite triager`. Added in the same commit as the outputs and baseline, so CI never sees the suite without them.

#### `README.md` (Modify)
*   "Eval suite": what it is, how to run, compare two runs and accept, how to regenerate, what a green CI grade does and doesn't mean.

### PR 3 (this repo): docs

#### `docs/evals/harness.md` (Modify)
*   "The first real suite": the triager's suite, where it lives and why, the input pinning the model and the dataset hash (an example of covering everything the answer depends on), the noise numbers, and the first baseline's result.

#### `docs/PLAN.md` (Modify)
*   The #16 row: "Triager golden suite from its dataset", 3.5 h.
*   A note under Week 3: #17, #18, #19 and #21 act on the suite in the triager repo, so #19's live CI and its key belong to that repo's workflow.

### Verification Plan

*   **Triager:** `uv run ruff check`, `uv run ruff format --check`, `uv run pyright`, `uv run pytest` (the sync test included), `uv run afr eval grade --suite triager` (exit 0 after the baseline).
*   **The live run's report**, saved as PR 2's evidence: per-field counts, errors (none), and the model.
*   **This repo:** docs only; the existing checks.
