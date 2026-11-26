# The eval harness

`afr eval` runs an agent against a suite of YAML cases, saves what it answered, grades each answer with simple checks, and compares the result with a committed baseline (#15). It is built from scratch on purpose, before any library is tried (plan decision 6; the comparison, #20, is now a stretch task), so every rule below can be explained line by line. The code is `src/flight_recorder/evals/`: `checks.py`, `grade.py` and `run.py`. The design, with the alternatives that were turned down, is [docs/designs/eval-harness.md](../designs/eval-harness.md).

```bash
uv run afr eval run --suite smoke      # call the agent once per case, save, grade
uv run afr eval grade --suite smoke    # grade the saved outputs again, calling nothing
```

Commands, options and exit codes: [docs/cli.md](../cli.md#afr-eval-run-and-afr-eval-grade).

## The agent contract

An agent is a Python function marked with `@agent`, named as `module:function` in the suite's `suite.yaml` (or with `--agent`). It takes a case's `input`, a dict, and returns a dict of fields: a Pydantic model counts.

```python
from flight_recorder.evals import agent


@agent
def triage(input: dict) -> dict: ...
```

It is imported into the `afr` process, so it must be installed in the same environment, and it runs with your permissions: run only agents you trust.

**Why the marker.** `suite.yaml` is data, and it names the function `run` calls. Without the marker a YAML file alone could choose any installed function: `subprocess:run` runs a command, and afr's own `flight_recorder.store.db:downgrade` drops every table in the store. The review found three such functions in a row, each after refusing the one before, so the harness doesn't try to list dangerous functions (a blocklist that never ends); it accepts only functions written as agents (an allowlist). The marker is checked after the module is imported, and importing a module runs its top-level code, so a `suite.yaml` still chooses which installed module loads. No harmful chain was found (such modules take their arguments from your command line, not from the YAML), but read a `suite.yaml` from someone else before `afr eval run`. CI runs only `afr eval grade`, which imports no agent at all. What an agent prints goes to stderr, and one case that raises, exits, or answers something that can't be stored gets an error line while the run goes on.

Command agents (JSON on stdin and stdout, with a timeout) come later. So do Claude Code sessions, which need them.

## A suite

```text
evals/cases/smoke/
  suite.yaml        agent: flight_recorder.evals.example:triage, model: rules-v1
  smoke-01.yaml     one case per file, named by its id
  ...
  outputs.jsonl     what the agent answered last time (written by `run`)
  baseline.json     each check's result when the baseline was accepted
```

A suite from Claude Code sessions lives under `evals/cases/local/`, which git ignores. A case is:

```yaml
id: smoke-03
input:
  title: Error and slow page
  body: ''
checks:
- {type: equals, field: labels, value: [performance, bug]}
```

`afr case new --from-run` writes cases in this format from recorded runs (#59). It also adds `expected`, a note for people, and `unreviewed: true` when `expected` is only what the agent did. Unreviewed cases are skipped and counted apart: their "right answer" is the agent's own earlier output, which would inflate the score. `--include-unreviewed` grades them, but they never enter the baseline.

Every case is checked when the suite loads, before anything runs. A bad file (a `.yml` or `.YAML` name, an `id` that isn't its file name, an `input` that isn't a mapping or that JSON can't hold, a check with a missing key, a YAML alias) stops the command with exit code 2. Check values are compared in the form outputs are stored in, so an unquoted YAML date in a check equals the ISO text an agent's date becomes.

## The outputs file

```json
{"run":{"agent":"flight_recorder.evals.example:triage","agent_version":"0.1.0","git_sha":"6c1ac98","model":"rules-v1","ran_at":"2026-11-17T14:01:12Z"}}
{"case":"smoke-01","error":null,"input_hash":"sha256:a7c6…","output":{"labels":["bug"],"meta":{"rules":1},"type":"bug"}}
```

- **Provenance once.** The header line records which agent made the outputs, its version, the model label, the commit, and the time.
- **Stale outputs.** Each line keeps a hash of its case's input. When the input changes, the saved output answers a different question, so the case is *stale*. Editing a check never makes an output stale: grading it again is enough.
- **Errors are results.** An agent that raises, or returns something that isn't a dict, gets an `error` line, and the run goes on.
- **Redacted.** Output and errors go through the recorder's redactor (plan decision 3), then lose their fingerprints: a secret becomes `[REDACTED:kind]`. Non-local outputs go into a public repo, so they're reviewed in the PR like cases.
- **Stable.** Canonical JSON (sorted keys, no spaces), one line per case in id order, written whole and then swapped in, so an interrupted run leaves the last good file. A rerun with the same answers changes only the header line.

## Checks and equality

| Type | Passes when |
|---|---|
| `equals` | the field equals `value` |
| `one_of` | the field equals one of `values` (accepted answers) |
| `contains` | the field's text holds `value`, or its list holds an item equal to it |
| `not_contains` | the opposite; a missing field passes |

`field` is a key, or a dotted path into nested dicts (`meta.rules`). The exact key is tried first, because recorded field names can contain dots.

One equality rule serves every check, and each part of it exists because of real agent output:

- **`null`, a missing field and `[]` are one "nothing".** An agent answering "no labels" with `null` and one answering `[]` mean the same thing.
- **Lists of plain values are multisets.** A list of labels has no meaningful order, so `[performance, bug]` equals `[bug, performance]`. Lists holding dicts or lists compare in order.
- **Numbers compare by value, but a boolean never equals a number.** In Python `True == 1`, which would let `[true]` pass for `[1]`; the harness refuses that, inside lists too.
- **Strings compare exactly.** Options such as ignoring case are left out until a case needs one.

The recorder's own "nothing", the text `"(none)"` (span attributes can't hold null), is not part of equality: a real agent that answers the text "(none)" is graded as text. Instead `afr case` turns `"(none)"` into `null` where it reads recorded decisions and typed answers.

Results are kept per check and counted per field, not only per case. Most wrong triager runs have one wrong field ([error analysis](error-analysis.md)), and a pass rate per case would hide which.

## Outcomes, the baseline and the gate

Each case gets one outcome: **skipped** (unreviewed, or no checks), **stale**, **error**, **pass** or **fail**. For a check, pass beats fail, which beats error: an error answered nothing at all.

A fixed bar ("95% of cases pass") was turned down. A suite built partly from wrong runs, which is how improvement shows, is red from the first run, and a bar that's always red says nothing. Instead `baseline.json` records each check's result, and every run is compared with it:

| What changed | Reported as | `run` fails? | `grade` fails? |
|---|---|---|---|
| A check got worse: pass to fail or error, or fail to error | regression | yes | yes |
| A check got better: error to fail or pass, or fail to pass | improvement | no | yes |
| A case's checks were edited | changed checks | yes, until accepted | yes |
| A case not in the baseline | new case | no | yes |
| A case in the baseline whose file is gone | removed case | no | yes |
| A case in the baseline now skipped | skipped (in baseline) | no | no |
| A stale case | stale | yes | yes |

**Why the two commands gate differently.** `run` asks "did the agent get worse?", so improvements and new cases are good news. `grade` re-grades outputs that were saved and haven't changed. Any difference there can only come from the grading code or the cases, so it fails on all of them, including a grading bug that wrongly passes a check. `--accept` writes the current results as the new baseline (never while a case is stale), and the outputs and baseline are committed together.

The baseline is per check, not per case, so a case that failed one field and now fails another still shows a regression.

## The smoke suite

`evals/cases/smoke/` is the harness's own test. Its agent, `flight_recorder.evals.example:triage`, is a few keyword rules with no model and no key, so anyone with a checkout can run it, and CI grades its saved outputs on every PR ("Eval harness (no agent calls)"). **Its score says nothing about the triager or any real agent.**

Its eight cases cover the rules above: label order (smoke-03), "nothing" as `[]` against `null` (smoke-04), a dotted path (smoke-07) and an unreviewed case (smoke-08). Two cases fail on purpose (smoke-05 and smoke-06), so the baseline holds failures: a grading change that wrongly passes them makes `grade` fail in CI.

CI makes no agent calls, so it catches a broken harness or case, not an agent getting worse. That's the live run in #19.

## Checking the harness against another grader

The triager graded its 200 corpus runs per field when they ran, with its own `grading.py` in another repo. `scripts/eval_agreement.py` rebuilds those runs as a scratch suite: one case per run, the run's recorded decisions as the output, its truth (and accepted answers, as `one_of`) as checks. It grades the suite with the harness and compares every field with the triager's mark. It reads the store only, makes no model calls, and writes only a temporary folder. This script checks the harness's own grades against the batch grader; `afr eval agreement` (below) is a different check, of a judge against hand grades.

The result, on 17 Nov 2026:

```text
corpus runs (latest attempt): 200; with a decision: 200
runs with accepted answers: 29
mixed truth (grade isn't the AND of its fields), left out: 0
agreement per field:
  labels: 200/200
  milestone: 200/200
  duplicate_of: 200/200
  regression_of: 200/200
disagreements: 0
```

A perfect score is only worth something if the check can fail, so the harness was broken on purpose and the script run again. With "everything is equal", 164 fields disagreed. With "null no longer equals null", 325 disagreed.

**What it can't show.** The triager normalises its values before recording them: labels sorted, a leading `#` stripped. So this shows the two graders agree on normalised, flat values. Label order and `#` handling meet raw agent output first in #16, where the triager's adapter normalises them (see "The first real suite").

## The first real suite

The issue triager's golden suite (#16) is the first suite with a real agent behind it. The design, with the alternatives turned down, is [docs/designs/triager-suite.md](../designs/triager-suite.md).

- **It lives in the triager's repo**, in [`evals/cases/triager/`](https://github.com/sonicjoy/issue-triage-agent/tree/main/evals/cases/triager), next to the code it tests. A prompt change and the outputs that measure it land in one PR, and the triager's CI grades them. The adapter `issue_triager.eval:triage` is marked `@agent`; it triages one case, approves the review step, and returns the four graded fields normalised (labels sorted, leading `#` stripped from issue numbers).
- **15 cases from the triager's own dataset** in the first baseline, chosen by hand: every failure mode in the [error analysis](error-analysis.md), cases where each field that can be empty should be empty and where it shouldn't, and three controls the agent got right in both earlier runs. A generator writes the case files from the dataset and a selection file, so the dataset stays the only copy of each truth, and a test fails when they drift apart.
- **Each input pins what the answer depends on.** That is the issue, the model (`openai:gpt-5.4-mini`), and a hash of everything else the agent can see: every issue as its search tools show it, the repo's labels and milestones, and its rules. Changing the model or anything the agent can see changes the input, so every case goes stale until the suite is run again. A stale case is better than an old output quietly graded as if the agent had seen today's data. The general rule: put in the input everything the answer depends on, or the staleness check can't see it change.
- **Red-team cases (#18)** joined in the 22 November baseline: s53 plants a prompt injection, and s66 and s67 put a fake `afr_` key in the issue that the agent must not echo. A selection entry adds `not_contains` checks on the graded fields or on `meta.rationale` and `meta.comment` through `extra_checks`. In the first run the agent obeyed s53's injection in full, kept as a known failure ([triager #15](https://github.com/sonicjoy/issue-triage-agent/issues/15)), and echoed no key. The design is [docs/designs/red-team-cases.md](../designs/red-team-cases.md).
- **The noise was measured before any code.** In the 12 November corpus, two runs of the same real issue on the same model agreed on a field 86% of the time: `labels` 27 of 35, `milestone` 33 of 35, `duplicate_of` 25 of 35, `regression_of` 35 of 35. With the first baseline's 60 checks, nearly every live run moves some of them, so a red `run` gate is normal for this suite. To measure a change, run twice and believe only what moved in both runs (the triager's README gives the steps). A harness feature to rerun or compare runs should come before #19: [#69](https://github.com/sonicjoy/agent-flight-recorder/issues/69).
- **The first baseline** (15 cases, 60 checks) comes from one live run on 17 November: no errors, 5 of 15 passing. Per field: `labels` 8 of 15, `milestone` 12, `duplicate_of` 13, `regression_of` 12. Labels are the weak field: extra labels on #3 and #9, a missed label on #28 (`stretch`, with two extra areas) and #30 (`infra`), the wrong area on #4, s50 and s62. Regressions are still missed (s47, s61, s62), and s47 linked #1 as a duplicate instead of a regression. Control #9 failed this time by adding labels, after two right runs: most likely the noise above, though one run can't show it. The failures are kept in the baseline, so a later change that fixes s47 shows as an improvement.
- **What the review caught.** The design first had a run that returned no decision answer `null` in every field. The harness reads `null`, missing and `[]` as one "nothing", so such a run passed every check whose truth is empty: 1 to 3 of 4 checks for an answer of nothing, depending on the case. The adapter now returns `"(no decision)"`, which no check accepts. Any grader that treats empty as equal to empty needs the agent's failure to be something no check can match.

## The LLM judge and its agreement

Some things `equals` can't grade, such as how a comment is worded. The judge (#17) is a model that grades one item against a rubric and answers `pass` or `fail` with a reason. It is an agent under test like any other: `flight_recorder.evals.judge:judge`, run by `afr eval run`. A judge is only worth its agreement with a person, so that is measured before anything relies on it. The design is [docs/designs/llm-judge.md](../designs/llm-judge.md); the commands are in [docs/cli.md](../cli.md#afr-eval-judge-suite-and-afr-eval-agreement).

**What was measured.** The rubric in [`evals/judges/triager-comment/`](../../evals/judges/triager-comment/rubric.yaml) grades a triager comment on two criteria: a duplicate or regression is suggested, not stated as decided (`suggests-not-settles`), and the comment names no issue number, label or milestone outside the decision (`only-decision-references`). The agreement set is 33 items: 23 corpus comments, and 10 edited to break one criterion each (tag `broken`). Each has a hand grade, committed before the first judge run. All 200 corpus runs have a comment call, so none was skipped when items were copied. The suite `comment-judge` has one case per item with the hand grade as its only check, so its pass rate is the percent agreement, and `afr eval agreement` adds Cohen's kappa (agreement with chance agreement removed). The target was kappa 0.6 overall.

**Results,** on 18 Nov 2026, judge model `openai:gpt-5.4-mini`:

| Rubric | Run | n | Agree | Kappa | Hand pass, judge fail | Hand fail, judge pass |
|---|---|---|---|---|---|---|
| Revision 0 | 1 | 33 | 81.8% | 0.63 | 5 | 1 |
| Revision 0 | 2 | 33 | 78.8% | 0.58 | 6 | 1 |
| Revision 0 | 3 | 33 | 84.8% | 0.69 | 4 | 1 |
| Revision 0 | 4 | 33 | 84.8% | 0.69 | 4 | 1 |
| Revision 1 | 1 | 33 | 93.9% | 0.86 | 0 | 2 |
| Revision 1 | 2 | 33 | 93.9% | 0.87 | 1 | 1 |
| Revision 1 | 3, agreement set | 33 | 90.9% | 0.80 | 1 | 2 |
| Revision 1 | 3, holdout | 5 | 80.0% | 0.00 | 1 | 0 |

`outputs.jsonl` holds only the last run. Each earlier run is its own commit (`git log -- evals/cases/comment-judge/outputs.jsonl`), and those commits are also the evidence that the grades came before the judge, so squashing those commits would drop them. Revision 0 has four runs, not the design's two: runs 3 and 4 happened by mistake, when revision 1's rubric file didn't parse and the suite was still revision 0's. They are kept as two more measurements. The last run judged all 38 items, so `afr eval agreement` prints 89.5% and kappa 0.76 for them together; its 33-item row above is the corpus and broken items counted without the holdout.

By tag, the same runs in the same order:

| Tag | n | Agree | Kappa |
|---|---|---|---|
| `corpus`, revision 0 | 23 | 78.3%, 73.9%, 82.6%, 82.6% | 0.36, 0.30, 0.43, 0.43 |
| `corpus`, revision 1 | 23 | 100%, 95.7%, 95.7% | 1.00, 0.78, 0.78 |
| `broken`, revision 0 | 10 | 90%, 90%, 90%, 90% | 0.00 (all 10 fail by hand) |
| `broken`, revision 1 | 10 | 80%, 90%, 80% | 0.00 |

- **On real comments revision 0 did worse than its overall number.** It wrongly failed 4 to 6 of the 21 good corpus comments in each run, for a kappa of 0.30 to 0.43 on the 23 corpus items. The overall 0.58 to 0.69 leaned on the broken items, where both graders mostly say fail.
- **Revision 0 sat on the target and wasn't stable.** Its first two runs gave 5 different verdicts on the same 33 items; over all four runs, 9 items got both verdicts. The judge was too strict. It failed comments for mentioning topics that aren't issue numbers, labels or milestones, and it read "looks like a duplicate of #20, could you confirm" as a settled duplicate. It also passed "This is a regression of #46." in all four runs: the examples only showed duplicates.
- **Revision 1 changed wording and examples only,** the one revision made of the three allowed. The design allows a revision only below the target; run 2's 0.58 was, run 1's 0.63 wasn't, so this one rests on a single run. It says that hedged or conditional wording is a suggestion, that only issue numbers, labels and milestones are references, and that the rationale is part of the decision. It adds a stated-regression example and a hedged-duplicate one, both invented. Wrong fails dropped from between 4 and 6 per run to 0 or 1, and the stated regression is caught in every run.
- **Revision 1 still flips.** Its first two runs differ on 4 items, and over three runs 5 items got both verdicts. Three of those are edited items whose extra issue number or milestone the judge missed in one or two runs, after catching them in every revision 0 run. Fewer wrong fails came with more missed failures: 9 of 10 edited items caught per run under revision 0, 8 or 9 under revision 1.
- **Real failures:** only 2 of the 23 corpus comments fail by hand, both on `suggests-not-settles`. The judge failed both in all seven runs. Two is too few to call a catch rate.
- **The holdout** is 5 corpus runs drawn at random before any judge run, graded and judged once after revision 1. All 5 pass by hand, so its kappa of 0.00 says nothing (one grader used a single class), and the judge wrongly failed one: it took "migration issue" for a reference, the mistake revision 1 was written to stop.

**Limits of this measurement.**

- **Small.** With 33 items kappa's interval is about ±0.3. The corpus rows are shakier still: 23 items with only 2 hand fails, so one verdict moves kappa from 1.00 to 0.78, and the counts say more than the kappa. The holdout's 5 items, all passes, test only how often the judge wrongly fails a good comment, and one item is 20 points.
- **Tuned.** Revision 1 was written after reading revision 0's disagreements on these 33 items, so its 0.80 to 0.87 flatters it. The holdout is the only number no rubric edit has seen.
- **One grader, with help.** The agreement set's hand grades are a first pass by Claude that the owner reviewed and accepted without changes; the holdout's are the owner's. Borderline calls (a paraphrased milestone, "listed as duplicate_of #30") were decided once and applied to every item. One broken item is itself borderline: b-7's comment names a milestone that the decision doesn't hold, but its rationale mentions "the recorder core milestone". It is graded fail, and the judge's two passes on it are counted as misses.
- **The model isn't the design's.** The design named `anthropic:claude-haiku-5-5`; no Anthropic credit was available, as for the [corpus](corpus.md). The comments in the 28 corpus and holdout runs were written by `gpt-5.4-nano` (24) and `gemini-3.5-flash-lite` (4), so the judge is from the same family as most of them. A judge can favour its own family's writing; here its errors were mostly too strict.
- **Easier than real.** The edited items may be easier to catch than real failures, and the corpus has only 2 of those.
- **One triager version.** The comments come from the 12 November corpus; its runs record the triager's models but no git SHA.
- **Not a gate.** The suite has no `baseline.json`, and CI never runs it. With verdicts that flip on 4 of 33 items between runs, judge a change twice and believe only what both runs say.

## Limits

- **No timeout.** A hung agent hangs the run. Suites are tens of hand-run calls; command agents will bring a timeout.
- **Noise.** One live LLM run is noisy. With 20 checks that each pass 97% of the time, about 46% of runs show at least one regression that isn't real. Rerun before believing a single regression. The triager measured noisier than that: two runs of one issue agreed on a field 86% of the time (see "The first real suite"). There is no rerun command yet ([#69](https://github.com/sonicjoy/agent-flight-recorder/issues/69)).
- **One case at a time.** Calls run one after another, and a changed input means rerunning the suite.
- **Not in the store.** Eval runs aren't linked to recorded runs yet, so tool checks (`tool_called`) and results in `eval_results` come later (#34).
