# The eval harness

`afr eval` runs an agent against a suite of YAML cases, saves what it answered, grades each answer with simple checks, and compares the result with a committed baseline (#15). It is built from scratch on purpose, before any library is tried (plan decision 6; the comparison is #20), so every rule below can be explained line by line. The code is `src/flight_recorder/evals/`: `checks.py`, `grade.py` and `run.py`. The design, with the alternatives that were turned down, is [docs/designs/eval-harness.md](../designs/eval-harness.md).

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

**Why the marker.** `suite.yaml` is data, and it names the function `run` calls. Without the marker a YAML file alone could choose any installed function: `subprocess:run` runs a command, and afr's own `flight_recorder.store.db:downgrade` drops every table in the store. The review found three such functions in a row, each after refusing the one before, so the harness doesn't try to list dangerous functions (a blocklist that never ends); it accepts only functions written as agents (an allowlist). CI runs only `afr eval grade`, which imports no agent at all. What an agent prints goes to stderr, and one case that raises, exits, or answers something that can't be stored gets an error line while the run goes on.

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

The triager graded its 200 corpus runs per field when they ran, with its own `grading.py` in another repo. `scripts/eval_agreement.py` rebuilds those runs as a scratch suite: one case per run, the run's recorded decisions as the output, its truth (and accepted answers, as `one_of`) as checks. It grades the suite with the harness and compares every field with the triager's mark. It reads the store only, makes no model calls, and writes only a temporary folder.

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

**What it can't show.** The triager normalises its values before recording them: labels sorted, a leading `#` stripped. So this shows the two graders agree on normalised, flat values. Label order and `#` handling meet raw agent output first in #16, where the triager's adapter normalises them.

## Limits

- **No timeout.** A hung agent hangs the run. Suites are tens of hand-run calls; command agents will bring a timeout.
- **Noise.** One live LLM run is noisy. With 20 checks that each pass 97% of the time, about 46% of runs show at least one regression that isn't real. Rerun before believing a single regression. Confirmation reruns wait until a noisy suite exists to measure (#16 or #19).
- **One case at a time.** Calls run one after another, and a changed input means rerunning the suite.
- **Not in the store.** Eval runs aren't linked to recorded runs yet, so tool checks (`tool_called`) and results in `eval_results` come later (#34).
