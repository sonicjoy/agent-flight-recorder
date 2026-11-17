# Eval harness for any agent (#15)

*   **Author:** Joe Luo
*   **Status:** Reviewed
*   **Date:** 2026-11-17

--------------------------------------------------------------------------------

## Overview

`afr eval` runs an agent against a suite of YAML cases and reports pass counts per case and per check. The cases are the ones `afr case` writes from recorded runs (#59), plus hand-written ones. The harness turns labels and cases into a number that can be compared between two versions of an agent.

It is built from scratch on purpose, before any library is tried (PLAN.md key decision 6; the comparison is #20). The core should be small enough to explain line by line in a portfolio write-up.

Week 3 of the plan needs one generic runner and grader. Its users, in order:
*   **The triager's suite (#16)** is the first real suite. The triager is called through a small adapter in its own repo. The triager keeps its own batch-and-grade loop; the harness doesn't replace it.
*   **The triager's 200 recorded runs** are the first data the harness is checked against (the agreement check).
*   **The analyst (#22)** comes next. It is an agent with no truth dataset, so its right answers come only from `afr label`.

### Glossary

*   **afr / the recorder / the store:** this project, Agent Flight Recorder. Agents send OpenTelemetry traces to `afr serve`, which stores them as runs and spans in Postgres (the store). The harness never needs the store.
*   **The triager:** the demo agent, in the separate issue-triage-agent repo. It is an LLM agent that reads a GitHub issue and decides its `labels`, `milestone`, `duplicate_of` and `regression_of`.
    *   Its own loop is `triager batch`, which grades each run against a truth dataset with `grading.py`. It writes the result onto the run as attributes:
        *   `triager.grade` is `correct`, `wrong` or `failed` (no valid decision);
        *   `triager.grade.<field>` is `ok` or `wrong`;
        *   `triager.decided.<field>` is the agent's answer;
        *   `triager.expected.<field>` is the truth;
        *   `triager.accepted.<field>` holds other accepted answers.
    *   Span attributes can't hold null, so it writes "nothing" as the string `"(none)"`, including an empty label list on the 200 back-filled corpus runs.
*   **The analyst (#22):** a planned local job that clusters failed runs and drafts eval cases. Its output is structured (clusters, draft cases), and it has no truth dataset.
*   **PLAN.md key decisions:** numbered rules in `docs/PLAN.md`, cited by number:
    *   3: redact before write;
    *   6: build the harness before using a library;
    *   7: cases live in git.
*   **The two-week rule:** new dependencies must be versions released at least two weeks ago.
*   **Paths:** `src/flight_recorder/evals/` is the Python package. `evals/cases/` (at the repo root) is the data folder.

## The Problem

Today a labelled run can become an eval case (`afr case new --from-run`), but nothing runs the cases. So:

- **A change to an agent can't be measured** against its cases. Changing a prompt or a model can only be judged by reading runs by hand, or, for the triager, with its own loop, which can't grade cases written from labels.
- **The rest of Week 3 is blocked.** These all need something that loads cases, runs an agent and grades its output:
    - the golden cases (#16);
    - the judge (#17);
    - the red-team set (#18);
    - evals in CI (#19);
    - the comparison with a library (#20);
    - the analyst's evaluation (#23).
- **If it is never built,** the plan's loop (record, find, label, write cases, run the suite, record again) stops at "write cases".

The proposed solution has two commands:
- `afr eval run` calls the agent once per case, saves the outputs, and grades them;
- `afr eval grade` grades outputs saved earlier.

Each command compares its results with a committed baseline, under the rules in "Exit codes". CI grades committed outputs, which tests the harness and the cases but not the agent. Catching the agent getting worse is the live run in #19.

## Goals

*   **Run any Python-function agent against a suite.** `afr eval run --suite S` imports the agent named in the suite's `suite.yaml` (or `--agent module:function`), and calls it once per case with the case's `input`. It needs only the YAML files and the agent: no store.
*   **Save outputs with their provenance.**
    *   `outputs.jsonl` holds one header line for the run: agent, agent version, model label, git SHA and time.
    *   It then holds one line per case: the case id, a hash of the case's input, and the output or the error.
    *   An output whose input hash no longer matches its case is STALE.
*   **Grade with four generic checks** (`equals`, `one_of`, `contains`, `not_contains`) on a field path, with one result per check and per-field counts made from them. The rules are in "Checks".
*   **Gate per check against a committed baseline.** A live `run` fails on regressions. `grade`, on saved outputs, fails on any difference. The rules are in "The baseline and the gate" and "Exit codes".
*   **Keep unreviewed cases out of the score.** Cases marked `unreviewed: true` are skipped and counted apart. `--include-unreviewed` grades them, but they never enter the baseline.
*   **Check the harness in CI with no agent calls.** A CI step runs `afr eval grade --suite smoke` on the committed smoke suite.
*   **Check the grading logic against the triager's grader** on its recorded runs (the agreement check), with the limits stated in that section.
*   **Keep it explainable.** The run, grade and check modules were estimated at about 220 lines, and the CLI and report about 90. As built they are about 520 and 200 lines of code (not counting docstrings, comments and blank lines): the Stage 9 validation and error rules, and the pre-merge review's fixes (one case can't stop a run; every bad input exits 2), roughly doubled them. The pre-merge simplicity review found no cut that keeps the behaviour and saves more than about 30 lines.

## Non-Goals

*   **Command agents** (JSON on stdin and stdout, with a timeout): a later issue. The analyst can be called in-process. Claude Code sessions can't be called at all until command agents exist.
*   **Linking eval runs to the store** (`afr.eval.case`) and the `tool_called` and `tool_not_called` checks that need it: a later issue. See Alternative K for why a wrapper span was dropped.
*   **The `judge` check:** #17.
*   **Writing results to the `eval_results` table.** The table exists (migration 0001), but #15 writes files only; see Alternative L. PLAN.md key decision 7 and `docs/schema.md` are amended to say so.
*   **Live agent runs in CI:** #19.
*   **A fixed pass bar,** such as 95% of cases. It is replaced by the baseline gate; see Alternative A.
*   **Confirmation reruns for noisy agents:** deferred until there is noise to measure; see Alternative P.
*   **Writing cases** (`afr case`, #59, and the analyst, #22), and **choosing which runs go in a suite** (#16). A suite should mix wrong runs, which show improvement, with right runs, which catch regressions.

## Critical User Journeys (CUJs)

### CUJ 1: Measure a change to an agent

*   **Given:** an agent installed in the same Python environment as `afr`. It is the triager through its adapter in #16, or later the analyst. `evals/cases/<suite>/` holds reviewed cases, a `suite.yaml` naming the agent, a committed `outputs.jsonl` and a `baseline.json`.
*   **When:** the owner changes the agent's prompt and runs `afr eval run --suite <suite>`.
*   **Then:**
    *   every case is called once;
    *   the report lists regressions and improvements per check, with counts per case and per field;
    *   the exit code follows "Exit codes".

    One live run is noisy (see Risks), so the owner reruns a suite before believing a single regression. To keep an improvement, `afr eval run --suite <suite> --accept` (or `grade --accept` on the saved outputs) writes a new baseline, which is committed with the outputs.

### CUJ 2: Check the grading logic against the triager's grader

*   **Given:** the triager corpus runs in the owner's store.
*   **When:** the owner runs `scripts/eval_agreement.py`.
*   **Then:** the script:
    *   checks the data's consistency;
    *   builds a scratch suite from the runs' attributes;
    *   grades it with the harness's grading code;
    *   prints per-field agreement with `triager.grade.<field>`, and every disagreement.

    It makes no model calls, and its output is saved as PR evidence.

### CUJ 3: CI checks the harness and the smoke suite on every pull request

*   **Given:** a pull request, and the committed smoke suite with its outputs and baseline.
*   **When:** CI runs `afr eval grade --suite smoke`.
*   **Then:**
    *   the grade comes from saved outputs, with no agent call and no store;
    *   any change from the baseline fails the step: a check that passes or fails differently, an added or removed case, an edited case whose input no longer matches its output, or a broken case file.

    Suites other than `smoke` are not graded in CI.

### CUJ 4: Run a suite holding draft cases

*   **Given:** `evals/cases/drafts/` (or `evals/cases/local/drafts/` for Claude Code sessions) holds cases written by `afr case new`, some marked `unreviewed: true`.
*   **When:** the owner runs `afr eval run --suite drafts`.
*   **Then:**
    *   unreviewed cases are not called, and the report counts them as skipped;
    *   `--include-unreviewed` calls and grades them too, and marks them in the report;
    *   a case is reviewed when its `unreviewed` key is removed by hand.

## Technical Plan (Architecture)

### Shape

The harness has three parts with one file format between them:
*   **Run** calls the agent and writes `outputs.jsonl`.
*   **Grade** reads the cases and the outputs and gives each check and case a result.
*   **Gate** compares the results with `baseline.json` and sets the exit code.

`afr eval run` does all three. `afr eval grade` does the last two, from outputs already on disk.

```text
evals/cases/S/
  suite.yaml ──────┐  agent, model label
  <id>.yaml ... ───┼──► run ──► agent(input) ──► outputs.jsonl  (header + one line per case)
                   │                                    │
                   └───────────────────────────────► grade ──► check and case results
  baseline.json ─────────────────────────────────────────────► gate ──► report + exit code
```

No part opens the store.

### Suite folder

*   **Where.** `--root` (default `evals/cases`, relative to the working folder) holds the suites. `--suite S` uses `<root>/S/`, or `<root>/local/S/` for suites from Claude Code sessions. That folder is git-ignored, as `afr case` already writes it. If both exist, the command stops with exit code 2.
*   **Which files.** Only files directly in the folder are read; subfolders are ignored.
    *   `suite.yaml` is the suite's settings. Every other `*.yaml` file is a case, and its name (without `.yaml`) must pass `check_id`.
    *   A `*.yml` file is an error ("use .yaml"), so a case can't be skipped by accident.
*   **`suite.yaml`:**
    *   `agent: module:function` is needed for `run` unless `--agent` is given;
    *   `model:` is an optional free-text label;
    *   unknown keys are an error;
    *   `grade` reads it only for the report's agent warning, and works without it.
*   **A case file:**
    *   `id` must be a string equal to the file name.
    *   `input` must be a dict, because the agent contract passes a dict.
    *   `checks` must be a list.
    *   `unreviewed`, if present, must be a boolean.
    *   Other keys (`expected`, `tags`, `from_run`, `placeholders`) are kept but not graded. `expected` is a note for people.
    *   A case with an empty `checks` list is skipped, with the note "no checks". `afr case` writes such a case for a run with no output.
    *   Two files with the same `id` are an error. Unreviewed cases are validated too, so a broken draft is found early.
*   **Reserved names.** `suite` and `local` can't be case ids or suite names. The rule lives in `check_id`, so `afr case` can't write a case that collides with `suite.yaml`.

Paths go through `check_id` and a new `suite_dir` helper beside `case_path` in `src/flight_recorder/evals/cases.py`, so nothing is read or written outside the suite folder. Case files are read with the existing `load` (`yaml.safe_load`).

### Outputs file

```json
{"run":{"agent":"flight_recorder.evals.example:triage","agent_version":"0.4.0","git_sha":"1c0a77b","model":"rules-v1","ran_at":"2026-11-17T12:00:00Z"}}
{"case":"smoke-01","error":null,"input_hash":"sha256:…","output":{"labels":["bug"],"type":"bug"}}
{"case":"smoke-02","error":"ValueError: no title","input_hash":"sha256:…","output":null}
```

*   **The header line** holds the facts about the whole run, once:
    *   `agent_version` is the installed version of the distribution that provides the agent's top-level package, found with `importlib.metadata.packages_distributions()`, else that package's `__version__`: an editable install (`uv sync`, as for this repo and the triager's adapter) records no package files, so the lookup finds nothing. It is `null` when neither is found.
    *   `git_sha` is the short commit of the git repo holding the suite folder (`git -C <suite folder>`), with `GIT_*` environment variables cleared. It gets `-dirty` when tracked files have uncommitted changes (`git status --porcelain --untracked-files=no` is not empty). It is `null` outside a git repo.
    *   `ran_at` is UTC.

    Every run changes the header line. Other lines change only when a case's output or error changed.
*   **`input_hash`** is `"sha256:"` plus the full hex digest of the case's converted `input` in canonical JSON (see "Canonical JSON"). A case is stale only when its input changed, or it has no line. Editing a check never makes an output stale.
*   **Errors.** An agent that raises, or returns something that isn't a dict after conversion, gets `output: null` and `error: "<ExceptionType>: <message>"`. The run goes on to the next case.
*   **Value conversion,** one rule used for storage and hashing:
    *   an object with a `model_dump` method (a Pydantic model) becomes `model_dump(mode="json")`, so a top-level model counts as a dict;
    *   dates and times are written in ISO form;
    *   tuples and sets become lists, with sets sorted;
    *   dict keys must be strings;
    *   numbers must be finite.

    Anything else turns the line into an error: `"output not JSON: <type>"`. The agent itself receives a deep copy of the case's input as loaded, with YAML dates still as dates. Only hashing and storage use the converted form.
*   **Redaction.** Only `output` and `error` are redacted, because `case` and `input_hash` are an id and a hash.
    1.  They go through the run's redactor (PLAN.md key decision 3).
    2.  The fingerprints are stripped with a new `strip_fingerprints` helper in `cases.py`, which `kept()` also uses. So a secret becomes `[REDACTED:kind]`: never the secret, and never a fingerprint tied to one person's key. No `placeholders` marker is added to the output, so the graded value changes only where the placeholder text is.

    `run` grades the redacted lines it writes, so `run` and a later `grade` always agree. The CLI builds the redactor with `default_redactor()`; library code takes it as a parameter, and `grade` never builds one.
*   **Writing.** Lines are collected in memory, and the file is written to a temporary file in the same folder and swapped into place with `os.replace` when the run finishes, as `hook/transcript.py` does. An interrupted run leaves the previous `outputs.jsonl` as it was.
*   **Format:**
    *   UTF-8, with `\n` line endings and a final newline;
    *   lines in case-id order;
    *   one canonical JSON object per line.
*   **A damaged file** stops `grade` with exit code 2 and "outputs.jsonl is damaged: run the suite again". That covers:
    *   no header first;
    *   a line that isn't JSON;
    *   two lines for one case.

### Canonical JSON

One serialiser, used for every hash and for `outputs.jsonl`: `json.dumps(convert(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)`. Here `convert` is the value conversion above. Because hashes are committed in `baseline.json`, changing this formula would make every suite re-baseline, so it is fixed here.

### Calling the agent

`run` imports the agent from `module:function`. If the module or function is missing or not callable, it exits with code 2 and names what's missing, before any case runs. Then, for each case not skipped, it calls the agent with a deep copy of the case's `input`.

### Checks

A check is `{type, field, …}`. A registry maps each type to a function that takes the check and the evidence (for now, the output) and returns a check result.

| Type | Passes when | Keys |
|---|---|---|
| `equals` | the field's value equals `value` | `field`, `value` |
| `one_of` | the field's value equals one of `values` | `field`, `values` |
| `contains` | the field's text contains `value` as a substring (`value` must be a string), or its list contains an item equal to `value` | `field`, `value` |
| `not_contains` | the opposite of `contains`; a missing field passes | `field`, `value` |

Options such as case-insensitive matching or strict list order are left out until a case needs one. The registry is where they'd be added.

*   **Equality** (for `equals`, `one_of` and list items in `contains`), applied the same way inside nested dicts and lists:
    *   **Nothing:** `null`, a missing field and an empty list `[]` are all the same "nothing". An agent that answers "no labels" with `null` and one that answers `[]` mean the same thing.
    *   **Lists:** lists of strings, numbers or booleans compare as multisets (same items, any order, by this equality), because a list of labels has no meaningful order. Lists that hold dicts or lists compare in order.
    *   **Numbers and booleans:** numbers compare by value (`1` equals `1.0`), but a boolean never equals a number. That holds inside lists too, so the multiset comparison can't use a plain `Counter`.
    *   **Strings** compare exactly.
*   **`contains` on other types.** If the field is neither a string nor a list, or `value` isn't a string when the field is text, the check fails with the reason `wrong value`.
*   **"Nothing" in recorded values.** The recorder's `"(none)"` is not a special case here. It is turned into `null` where it is read from a recording, so a real agent that answers the text "(none)" is graded as text. Two places do this:
    *   `afr case`, when it drafts a case: only values taken from the run's decided attributes (`decided_fields`) and from a label's typed answer fields, at the top level. Free-text answers and agent output are left alone. Because `[]` and `null` are both "nothing", an empty label list recorded as `"(none)"` also matches an agent that answers `[]`.
    *   The agreement script.
*   **Field paths.** `field` is first looked up as one exact key, because recorded field names can contain dots. Only if that key is missing is it split on dots and walked through nested dicts. A path that runs into a value that isn't a dict counts as missing; it never crashes.
*   **Check results.** A result is `{field, key, expected, actual, passed, reason}`:
    *   `key` is `"<field>/<8 hex digits>"`, the first 8 hex digits of SHA-256 over the whole check (every key, including `type` and `field`) in canonical JSON;
    *   `reason` is `ok`, `wrong value` or `missing`.
*   **Validation.** These are found when the suite is loaded, and the command stops with exit code 2 before any case runs:
    *   an unknown type;
    *   a `field`, `value` or `values` key that is absent (`value: null` is valid, and is how "nothing" is checked);
    *   `values` that isn't a list;
    *   two checks in one case with the same key.

    So a broken check never reaches grading, and no two checks share a baseline entry.
*   **Per field.** A field passes in a case when every check on it passes.

### Case outcomes

Each case gets one outcome:
*   **skipped:** `unreviewed: true` without `--include-unreviewed`, or no checks. With `--include-unreviewed`, an unreviewed case that `grade` finds no line for is also skipped, with the note "no output: run it with --include-unreviewed". It is not stale, because `run` skipped it on purpose.
*   **stale:** no line for the case, or its `input_hash` doesn't match.
*   **error:** the line has an error.
*   **pass:** every check passed.
*   **fail:** any check failed.

Lines for cases that no longer exist are listed as orphaned and otherwise ignored.

### The baseline and the gate

`baseline.json` records each reviewed, graded case's checks:

```json
{
  "smoke-01": {"labels/3fa2c901": "pass", "type/91c04b7e": "fail"},
  "smoke-02": {"type/91c04b7e": "error"}
}
```

A case with outcome `error` records `error` for each of its checks. The file uses sorted keys, two-space indentation, `\n` line endings and a final newline, so its diffs are clean.

How each state of a case compares with the baseline, and what fails each command:

| State | Reported as | `run` fails? | `grade` fails? |
|---|---|---|---|
| A check's key got worse (pass, then fail, then error, best first) | regression | yes | yes |
| A check's key got better | improvement | no | yes |
| A case in the baseline has keys added, removed or changed (a check was edited) | changed checks | yes, until accepted | yes |
| A case not in the baseline | new case | no | yes |
| A case in the baseline whose file is gone | removed case | no | yes |
| A case in the baseline that is now skipped | skipped (in baseline) | no | no |
| A stale case (its keys aren't compared) | stale | yes | yes |

*   **Why the commands differ.** `run` asks "did the agent get worse?", so improvements and new cases are welcome news. `grade` re-grades outputs that were saved and haven't changed, so any difference can only come from the grading code or the cases. That is why it fails on any difference, including a grading bug that wrongly passes a check.
*   **`--accept`** writes the baseline from the current results, after printing the report as usual.
    *   Only reviewed, graded cases are written; skipped and unreviewed cases never enter the baseline.
    *   It refuses while any case is stale: it prints which cases, writes nothing, and exits 1.
    *   After writing, the command exits 0, because the new baseline is what the results are now compared with.
    *   Both `run` and `grade` take it.

### Exit codes

The only statement of the gate. The rest of this document refers here.

| Code | When |
|---|---|
| 0 | Nothing in the gate table fails the command. Also: `--accept` wrote the baseline, or there is no `baseline.json` and no case is stale. With no baseline, the report says to run `--accept`. |
| 1 | A row of the gate table fails the command, or `--accept` refused because of stale cases. |
| 2 | Usage errors: no such suite, a suite in both folders, a bad `suite.yaml`, a bad agent name, an unreadable or invalid case file, a bad check, a duplicate case id, a `.yml` file, a damaged `outputs.jsonl`, or a file that can't be written. |

### Report

The report goes through `render.console()`, which turns off Rich markup and highlighting. Recorded text, including agent output, goes through `render.text()`, so neither control characters nor Rich markup in an output can reach the terminal. The report contains:
*   a header with the suite, agent and version, model, git SHA and time;
*   case counts, such as `14/20 passed · 2 failed · 1 error · 0 stale · 3 skipped`, where the total is every case in the suite;
*   a per-field table with each field's `passed` and `graded` counts. A field is graded in every case with outcome `pass`, `fail` or `error`, and an error counts as failed;
*   the changes against the baseline, in the order of the gate table;
*   each failing check, as expected against actual;
*   a warning when the outputs' header names a different agent from `suite.yaml`.

**`--json`** prints one JSON object instead, with these keys:
*   `suite`;
*   `header`: the outputs header;
*   `counts`: per outcome;
*   `fields`: per field, `passed` and `graded`;
*   `cases`: each with `id`, `outcome`, `note` and `checks` (the check results);
*   `changes`: one list per row of the gate table, plus `orphaned`;
*   `exit_code`.

### The in-repo smoke suite

`src/flight_recorder/evals/example.py` holds a rule-based agent, `triage(input) -> {"type", "labels", "meta"}`. It reads an issue's `title` and `body`, ignoring case:

| Words in the title or body | Adds |
|---|---|
| "crash", "error", "exception" | type `bug`, label `bug` |
| "docs", "readme", "typo" | label `documentation` (type `docs` if no bug word) |
| "slow", "timeout" | label `performance` |

If no type rule matches, `type` is `None`. `labels` is the list of labels in the order the rules match. `meta` is `{"rules": <number of rules that matched>}`, which gives the dotted-path case something to read.

`evals/cases/smoke/` holds `suite.yaml` (agent `flight_recorder.evals.example:triage`, model `rules-v1`), the cases below, and `outputs.jsonl` and `baseline.json`. Those two files are generated once by `afr eval run --suite smoke --accept` and then committed.

| File | Input (title / body) | Checks | Outcome in the baseline |
|---|---|---|---|
| `smoke-01.yaml` | "App crash on start" / "" | `equals type bug`; `equals labels [bug]` | pass |
| `smoke-02.yaml` | "Fix typo in README" / "" | `equals type docs`; `contains labels documentation` | pass |
| `smoke-03.yaml` | "Error and slow page" / "" | `equals labels [performance, bug]` (a different order from the agent's) | pass |
| `smoke-04.yaml` | "Question about setup" / "" | `equals type null`; `equals labels null` (the agent answers `[]`) | pass |
| `smoke-05.yaml` | "Timeout talking to API" / "" | `one_of type [bug, performance]` (the agent answers `null`) | fail, on purpose |
| `smoke-06.yaml` | "Crash in docs build" / "" | `not_contains labels documentation` | fail, on purpose |
| `smoke-07.yaml` | "Slow docs site" / "" | `equals meta.rules 2` | pass |
| `smoke-08.yaml` | "New idea" / "" | `equals type feature`; `unreviewed: true` | skipped |

The two failures on purpose mean the baseline holds `fail` keys, so a grading change that wrongly passes them makes `grade` fail. The docs state that this agent exists to exercise the harness, and that its score says nothing about the triager.

### Agreement check with the triager's grader

`scripts/eval_agreement.py` checks the harness's grading code against a grader it shares no code with. It opens the store read-only with `cli.runs.open_store(readonly=True)`. It reads only:
*   runs in the four corpus batch groups (`real-mini`, `synthetic-nano`, `injected-mini`, `real-gemini`);
*   for each `(triager.batch.group, triager.case, triager.batch.repeat)`, the run with the highest `triager.batch.attempt`, as `docs/evals/corpus.md` describes;
*   runs whose `triager.grade` is `correct` or `wrong`. `failed` runs have no decision.

1.  **Consistency first.** It counts runs that carry `triager.accepted.*`. It checks that each run's `triager.grade` is `correct` exactly when every `triager.grade.<field>` is `ok`. Runs that fail this are listed as "mixed truth" and left out, because they would show disagreements that have nothing to do with the harness.
2.  **Build the suite.** For each remaining run, it writes a case and an output line in a new temporary folder outside the repo, deleted at the end.
    *   The case id is `r-` plus the first 12 hex digits of the run id. The input is `{"run": "<run id>"}`; no issue text is used, and its hash is what the line records.
    *   The output is the run's `triager.decided.*`.
    *   Each field gets an `equals` on `triager.expected.<field>`, or, when the run has accepted answers for the field, a `one_of` on the truth plus the accepted answers.
    *   Accepted answers are lists of strings; accepted labels are one string joined with `", "`, split back into lists.
    *   Every `"(none)"` becomes `null`, and a missing attribute is left missing. Both are "nothing", like `[]`.
3.  **Compare.** It calls the grading functions directly, without the CLI, and compares each field's result with `triager.grade.<field>`.

It prints:
*   the consistency counts;
*   agreement per field;
*   each disagreement, as run id, field, both values and both marks.

It always exits 0; its output is the evidence, saved in the PR with no issue text. A disagreement is either a harness bug, which gets fixed, or a rule that differs on purpose, which gets documented in `docs/evals/harness.md`.

**What it can and can't show.** The triager normalises values before writing them: it sorts labels and strips a leading `#` from strings. So the check shows that the two graders' logic agrees on the triager's normalised flat values. It can't test label order or `#` handling. Those meet raw agent output first at #16's first live run, where the adapter normalises them.

### CI

The existing `ci.yml` job gets one step after "Pytest", **"Eval harness (no agent calls)"**, which runs `uv run afr eval grade --suite smoke`. `grade` checks committed outputs and fails on any difference from the baseline. The step doesn't run the agent, so it can't catch an agent regressing; that is #19.

## Risks & Mitigations

| Risk | Mitigation or acceptance |
|---|---|
| A hung agent hangs the run (no timeout in-process). | Accepted for #15: suites are about 20–30 hand-run calls. Fixed with command agents. A worker-thread timeout can't kill the call, so it isn't worth adding. |
| LLM noise flags regressions that aren't real. With 20 checks that passed before, each right 97% of the time, about 46% of live runs show at least one. | Accepted for #15, where no noisy agent runs: the smoke agent always gives the same answer, and CI only grades. The owner reruns a live suite before believing a single regression. Confirmation reruns (Alternative P) come with the first noisy suite (#16 or #19), once its noise can be measured. |
| A grading bug passes checks it shouldn't. | `grade` fails on any difference, and the smoke suite has checks the agent fails on purpose. |
| An interrupted run loses the last good outputs. | Atomic replace at the end of the run. |
| A secret, or a key-tied fingerprint, reaches git in outputs. | Redaction, then `strip_fingerprints`. Non-local outputs are new LLM text going into a public repo, so they are reviewed in the PR like cases. Claude Code suites stay under the git-ignored `local/`. |
| The agent is arbitrary code running with the user's permissions. | Same as any imported Python. The docs say to run only agents you trust. Because `suite.yaml` is data, an agent must be a plain function or method (not a class, a partial or a Typer/click app such as `flight_recorder.cli:app`), and one defined in the standard library or builtins is refused (found in the pre-merge review: `agent: subprocess:run` ran a command from YAML alone). Any other plain function still gets through, afr's own included: `agent: flight_recorder.store.db:downgrade` with `input: {}` would drop every table in the configured store (found in the pre-merge fix review, shown with stand-ins). So a `suite.yaml` from someone else is code: read it before `afr eval run`. CI runs only `grade`, which imports nothing. Closing this needs an allowlist (an explicit agent marker), not more refusals. |
| Large suites: calls run one after another, and `outputs.jsonl` grows with the suite. | Accepted. Suites are tens of cases. A single case can't be rerun on its own; a changed input means rerunning the suite. |
| The agreement check's data was back-filled and partly re-graded (#14), so truth versions may be mixed. | The consistency step sets those runs apart before comparing. |
| Eval runs can't be found in the store. | Accepted until the linking issue (see Alternative K). |
| Two different checks on one field share 8 hex digits of hash. | About a 1 in 4 billion chance per pair. The validator then reports a duplicate key, and either check can be reworded. |

## Open Questions

*   **#15's "Done when"** must be edited to match this design before coding, with the new budget (about 220 + 90 lines) and estimate (3.5 hours).
*   **The triager's adapter (for #16):** the harness calls `module:function` with a case's `input` and expects a dict of fields. The triager probably needs a short adapter in its repo, which also normalises `#` and label order. #16's first live run, about 20 to 30 calls on the owner's key or the Gemini free tier, makes the triager suite's first outputs and baseline.
*   **#16 may shrink:** the triager's dataset already holds corrected truth and accepted answers, so its suite could be converted from that instead of drafted run by run. That's #16's decision.

## Alternatives Considered

### Alternative A: A fixed pass bar (95% of cases)

*   **Pros:** One number, easy to state. It suits suites that should always pass, such as red-team sets.
*   **Cons:**
    *   Suites built partly from wrong runs fail it from the first run. The triager was right on 81 of 200 runs after #14's corrections.
    *   It also trips on noise.
    *   A baseline gate is noisier still on live runs: with 20 checks at 97% each, about 46% of runs flip at least one. That noise is accepted for #15 (see Risks and Alternative P).
*   **Reason for Rejection:** A bar that is red from day one carries no signal. A baseline answers the question an eval is for: did this change make things better or worse? A per-suite bar may return for red-team suites (#18).

### Alternative B: Live agent runs in this issue's CI

*   **Pros:** CI would catch the agent regressing.
*   **Cons:** It needs a model key in CI. The plan allows only the Gemini free tier there, and #19 owns that job.
*   **Reason for Rejection:** No paid calls in CI. This issue's CI step is a harness test.

### Alternative C: The full spec in one issue

*   **Pros:** It covers everything #15 first listed: command agents with a timeout, tool checks, `judge`, and writing `eval_results`.
*   **Cons:** It would run far past both budgets: the issue's 2 hours, and this design's 3.5 hours with about 310 lines. Tool checks need the store and run linking. The core would no longer be explainable line by line.
*   **Reason for Rejection:** Portfolio first. Each item has a seam (the check registry, the outputs file) and goes to a later issue.

### Alternative D: One `run` command with a `--saved` option

*   **Pros:** One command fewer.
*   **Cons:** CI's step would be called "run" while running nothing, and one command would have to gate two ways.
*   **Reason for Rejection:** `run` plus `grade` makes each command's promise clear from its name.

### Alternative E: Strict equality everywhere

*   **Pros:** No hidden rules.
*   **Cons:** The triager's labels are a list where order carries no meaning, and agents say "nothing" as `null` or `[]`. Strict equality would mark right answers wrong unless every case added options.
*   **Reason for Rejection:** Lists of plain values compare as multisets, and `null`, missing and `[]` are one "nothing". A strict-order option can be added when a case needs one.

### Alternative F: Grading unreviewed cases, or refusing suites that hold them

*   **Pros:** One report with every case, or forced review.
*   **Cons:** An unreviewed case's expected answer is the agent's own earlier output, which inflates the score. Refusing blocks every `drafts` suite.
*   **Reason for Rejection:** Skipped and counted apart; `--include-unreviewed` grades them, and they never enter the baseline.

### Alternative G: Naming the agent only with `--agent`

*   **Pros:** No suite file.
*   **Cons:** Every invocation must repeat the agent, and a suite can be run against the wrong one.
*   **Reason for Rejection:** `suite.yaml` names it; `--agent` overrides it.

### Alternative H: Flat field names only

*   **Pros:** Simpler lookup.
*   **Cons:** Other agents return nested output, and `afr case` can write field names that contain dots.
*   **Reason for Rejection:** Dotted paths are kept, with the exact key tried first.

### Alternative I: A file per case, or a file per run with history

*   **Pros:** A file per case gives per-case diffs. A file per run keeps history without a database.
*   **Cons:** A file per case can mix outputs from different agent versions. A file per run grows the repo with each commit, and needs a rule for which file CI grades.
*   **Reason for Rejection:** One `outputs.jsonl` per suite, holding the latest run under one header.

### Alternative J: The smoke suite runs committed triager outputs, or the triager as a dev dependency

*   **Pros:** Real triager data.
*   **Cons:** Committed outputs can't be refreshed from this repo, and a git dependency ties the two repos' lock files.
*   **Reason for Rejection:** A rule-based agent in this repo runs anywhere with no key.

### Alternative K: Linking eval runs with a wrapper span, or by tagging the agent's root span

*   **Pros:** Eval runs could be found in the store, ready for tool checks.
*   **Cons:**
    *   A wrapper span becomes the trace's root. Ingest names the run and copies its attributes from the root (`ingest/mapping.py`), so every eval run would be called `afr.eval.case`, and would lose the `decided.*` attributes `afr label` and `afr case` read.
    *   Tagging the agent's own root needs setup inside each agent.
    *   Nothing in #15 reads the tag.
*   **Reason for Rejection:** Deferred to the tool-check issue, with no new dependency in #15.

### Alternative L: Files only, against also writing `eval_results`

*   **Pros:** Writing rows when a store is configured would follow PLAN.md key decision 7.
*   **Cons:**
    *   A second write path to test, when `grade` and CI have no store anyway.
    *   The table doesn't fit the harness's results one to one:
        *   `passed` is one boolean, while a case has five outcomes;
        *   `model` and `git_sha` are required, while the header allows them to be missing;
        *   `suite_run_id` is a UUID the header doesn't carry.
*   **Reason for Rejection:** Files only for #15. Key decision 7 and `docs/schema.md` are amended to say that results live in `outputs.jsonl` until #34 loads them. That loader decides the mapping, with the mismatches above listed for it in `docs/schema.md`.

### Alternative M: Decode "(none)" in the equality rules

*   **Pros:** No change to `afr case`.
*   **Cons:** It builds the recorder's encoding into a harness that should be agent-neutral, so a real agent's text "(none)" would equal null.
*   **Reason for Rejection:** Decoded where it is read from a recording, from recorded decisions and typed answers only. An empty list recorded as `"(none)"` becomes `null`, which equals `[]` under the "nothing" rule.

### Alternative N: Reuse or extract the triager's grader

*   **Pros:** It already grades per field, with accepted answers.
*   **Cons:** It is written for four triager fields and a truth dataset, and lives in another repo.
*   **Reason for Rejection:** The harness grades against case checks. The triager's grader serves as an independent check, which only means something because the two share no code.

### Alternative O: An existing library (promptfoo or DeepEval)

*   **Pros:** Existing assertion types, reports and result caching.
*   **Cons:** Building the harness is how eval design is learned end to end.
*   **Reason for Rejection:** Build first, compare in #20 (PLAN.md key decision 6).

### Alternative P: Confirmation reruns and a `--strict` flag in #15

*   **Pros:**
    *   Re-calling a newly failing case a couple of times, and failing only if it fails every time, separates noise from real regressions.
    *   A `--strict` flag would let any command fail on any difference.
*   **Cons:**
    *   No noisy agent runs in #15, so nothing would exercise the rerun path.
    *   Reruns add a flag, a `retries` field, a "flaky" category and a call-again loop, and leave open questions: whether a case or a check is re-called, and which result `--accept` keeps.
    *   `--strict` only means something on saved outputs, where it is simply how `grade` behaves.
*   **Reason for Rejection:** Deferred until there is noise to measure (#16 or #19). Adding `retries` to output lines later stays compatible. `grade` fails on any difference by default, with no flag.

--------------------------------------------------------------------------------

## Non-Functional Requirements

*   **Security and privacy:** see Risks & Mitigations.
    *   Case and suite names go through `check_id` and `suite_dir`.
    *   Agent output is printed through `render.text()`.
    *   The agreement script reads the store read-only, writes only to a temporary folder outside the repo, and saves no issue text.
*   **Cost:** `run` makes one call per case. `grade`, CI and the agreement check make no model calls.
*   **Rollout and rollback:** Everything is new except four things:
    *   one CLI registration;
    *   one CI step;
    *   small changes to `evals/cases.py` (`suite_dir`, `strip_fingerprints`, the reserved `suite` id, and decoding `"(none)"`);
    *   doc amendments.

    There is no database change and no new dependency. Rolling back means removing them.

## Additional Resources

*   `.review/15-eval-harness/`: the brainstorm and review reports for this design.
*   `docs/evals/error-analysis.md`: why results are kept per field, and the 81-of-200 count.
*   `docs/evals/corpus.md`: the triager attributes the agreement check reads, and how to pick corpus runs.
*   The triager's `src/issue_triager/grading.py` (issue-triage-agent repo): the grader the harness is checked against.

## Future Improvements

See Non-Goals. In short:
*   command agents with a timeout;
*   linking eval runs, and tool checks;
*   `judge` (#17);
*   confirmation reruns;
*   loading outputs into `eval_results` (#34).

--------------------------------------------------------------------------------

## Detailed Implementation Plan

One branch, `15-eval-harness`, and one PR, which closes #15. The work goes in six phases. Each phase ends with its tests passing and the full suite still green, and each phase's tests are written first and shown failing.

### Before coding

*   **Edit issue #15's "Done when"** to match this design, with the budget (about 220 + 90 lines) and the 3.5-hour estimate:
    *   two commands;
    *   a Python-function agent;
    *   four check types;
    *   the outputs file;
    *   a per-check baseline, where `run` fails on regressions and `grade` on any difference;
    *   unreviewed cases skipped;
    *   the agreement check;
    *   the CI step as a harness test.

    List what moved out: command agents, run linking and tool checks, `judge` (#17), confirmation reruns, and writing `eval_results` (#34).
*   **Edit issue #16** to say three things:
    *   a suite mixes right and wrong runs;
    *   the triager needs an adapter that normalises `#` and label order;
    *   converting the triager's dataset may replace drafting run by run.

### Module contracts

Plain dataclasses (frozen) for results; dicts for case and output data, as `evals/cases.py` already uses.

```text
evals/checks.py
  convert(value) -> Any                       # value conversion; raises ValueError("output not JSON: <type>")
  canonical(value) -> str                     # the Canonical JSON call
  lookup(output: dict, field: str) -> tuple[bool, Any]
  same(a, b) -> bool                          # equality, including "nothing"
  check_key(check: dict) -> str               # "<field>/<8 hex>"
  validate(checks: list) -> list[str]         # load-time problems
  CheckResult(field, key, expected, actual, passed: bool, reason: str)
  CHECKS: dict[str, Callable[[dict, dict | None], CheckResult]]
  run_checks(checks: list[dict], output: dict | None) -> list[CheckResult]

evals/grade.py
  input_hash(case: dict) -> str
  CaseResult(id, outcome, note: str | None, unreviewed: bool, checks: list[CheckResult], error: str | None)
  SuiteResult(cases: list[CaseResult], orphaned: list[str], fields: dict[str, tuple[int, int]])
  grade_suite(cases: list[dict], lines: dict[str, dict],
              *, include_unreviewed: bool) -> SuiteResult      # grading never reads the header
  Changes(regressions, improvements, changed, new, removed, skipped_in_baseline, stale)  # lists of ids or keys
  compare(result: SuiteResult, baseline: dict | None) -> Changes
  exit_code(changes: Changes, command: Literal["run", "grade"]) -> int
  baseline_from(result: SuiteResult) -> dict        # raises StaleError(list of ids) if any case is stale

evals/run.py
  Suite(name, folder: Path, agent: str | None, model: str | None, cases: list[dict])
  load_suite(root: Path, name: str, *, agent: str | None = None) -> Suite       # CaseError on any problem
  import_agent(spec: str) -> Callable[[dict], Any]                              # CaseError
  agent_version(spec: str) -> str | None
  git_sha(folder: Path) -> str | None
  run_suite(suite: Suite, *, redactor: Redactor, include_unreviewed: bool, now: datetime)
      -> tuple[dict, dict[str, dict]]                                           # header, lines by case
  read_outputs(folder: Path) -> tuple[dict | None, dict[str, dict]]             # CaseError if damaged
  write_outputs(folder: Path, header: dict, lines: dict[str, dict]) -> None
  read_baseline(folder: Path) -> dict | None
  write_baseline(folder: Path, baseline: dict) -> None
```

### Phase 1: checks

#### `src/flight_recorder/evals/checks.py` (Create)
*   **Changes:** everything listed for `checks.py` in "Module contracts", with the rules in "Checks", "Value conversion" and "Canonical JSON". It holds the value conversion so that the check hash and the input hash share it.
*   **Tests:** `tests/evals/test_checks.py`.
    *   **lookup:** an exact key with dots, a dotted walk, a path through a value that isn't a dict, missing.
    *   **same:**
        *   null, missing and `[]` as one "nothing";
        *   multisets, including a boolean list against an integer list;
        *   lists of dicts in order;
        *   `1` and `1.0`;
        *   exact strings;
        *   nested dicts.
    *   **Each check:** pass, fail and missing, plus `not_contains` on a missing field and `contains` on a number field.
    *   **validate:**
        *   an unknown type;
        *   an absent `value`;
        *   `value: null` accepted;
        *   `values` that isn't a list;
        *   a duplicate key.
    *   **check_key:** stable under key order.
    *   **convert:** a Pydantic model, a date, a set, a tuple, a non-string key, NaN.
*   **Checkpoint:** `uv run pytest tests/evals/test_checks.py`.

### Phase 2: grading and the gate

#### `src/flight_recorder/evals/grade.py` (Create)
*   **Changes:** everything listed for `grade.py` in "Module contracts", with the rules in "Case outcomes", "The baseline and the gate" and "Exit codes".
*   **Tests:** `tests/evals/test_grade.py`.
    *   Each outcome, including no checks, and unreviewed with no line.
    *   The input hash ignores `checks`, `expected` and `tags`.
    *   Orphaned lines.
    *   Per-field counts, where an error counts as failed.
    *   Each row of the gate table, for both commands.
    *   A regression on another field in a failing case.
    *   `baseline_from` refuses a stale case, writes `error` for each check of an error case, and leaves out skipped and unreviewed cases.
*   **Checkpoint:** `uv run pytest tests/evals/test_grade.py`.

### Phase 3: suites, the agent and the outputs file

#### `src/flight_recorder/evals/cases.py` (Modify)
*   **Changes:**
    *   `check_id` also refuses `suite`.
    *   A new `suite_dir(root, suite, *, local) -> Path`, with the same "stays inside root" guard as `case_path`.
    *   A new `strip_fingerprints(value)`, used by `kept()` and by `run_suite`.
    *   In `draft_case`, values from `decided_fields` and from `answer["fields"]` that equal `payloads.NONE_TEXT` at the top level become `None`.
*   **Tests:**
    *   `tests/test_cases.py`: `suite` in the `bad` ids list; `suite_dir` refuses a path outside root; `strip_fingerprints` leaves no `:<8 hex>]` suffix and adds no marker.
    *   `tests/test_case_cli.py` (database tests): a new recorded run whose root span has `triager.decided.duplicate_of: "(none)"` and `triager.decided.labels: "(none)"` is drafted with `null` for both. A label free-text answer of `"(none)"` stays text. The run is added beside the shared `TRIAGER` fixture, not by changing it.

#### `src/flight_recorder/evals/run.py` (Create)
*   **Changes:** everything listed for `run.py` in "Module contracts", with the rules in "Suite folder", "Outputs file" and "Calling the agent".
    *   `run_suite` takes the redactor as a parameter, and grades nothing itself.
    *   `git_sha` runs `git` with `GIT_*` variables removed.
    *   `write_outputs` and `write_baseline` write to a temporary file in the folder, then `os.replace`, with `newline="\n"`.
*   **Tests:** `tests/evals/test_run.py`, with a fixed `Redactor` as in `tests/test_case_cli.py`.
    *   **Suite folder errors:**
        *   no such suite;
        *   a suite in both folders;
        *   `.yml`;
        *   a mismatched or duplicate id;
        *   an unknown `suite.yaml` key;
        *   `input` that isn't a dict.
    *   **The agent:** import errors; an agent that raises, returns a non-dict, returns a Pydantic model, or changes its input.
    *   **Redaction:** an `anthropic_key` from `fake_secrets.secrets_by_kind()` echoed in output is written as `[REDACTED:anthropic_key]`, with no fingerprint and no `placeholders` key.
    *   **The header:**
        *   `agent_version` for the smoke agent is the installed `agent-flight-recorder` version;
        *   `git_sha` is clean, `-dirty`, and `null` in a non-git `tmp_path`, using the `GIT_*`-free environment from `tests/conftest.py`.
    *   **The outputs file:**
        *   line order and format;
        *   an interrupted run leaves the old `outputs.jsonl`;
        *   a damaged `outputs.jsonl` (no header, bad JSON, duplicate case) raises `CaseError`;
        *   a `local/` suite's files stay under `local/`.
*   **Checkpoint:** `uv run pytest tests/evals tests/test_cases.py tests/test_case_cli.py`.

### Phase 4: the CLI, the example agent and the smoke suite

#### `src/flight_recorder/evals/example.py` (Create)
*   **Changes:** the agent in "The in-repo smoke suite". Its docstring says it exists only to exercise the harness, and that its score says nothing about the triager.

#### `src/flight_recorder/cli/eval.py` (Create)
*   **Changes:** `eval_app` with `run` and `grade`. Each imports its library modules inside the command, so loading the CLI doesn't import them.
    *   **`run` options:** `--suite` (required), `--agent`, `--root` (default `cli.case.DEFAULT_ROOT`), `--include-unreviewed`, `--accept`, `--json`. It builds the redactor with `default_redactor()`, then calls `run_suite`, `write_outputs`, `grade_suite` on the written lines, and `compare`. It prints the report, handles `--accept`, and exits with `exit_code(..., "run")`.
    *   **`grade` options:** the same without `--agent`. It reads the outputs instead of running, and exits with `exit_code(..., "grade")`.
    *   **Errors:** a `CaseError`, or an `OSError` while writing, prints its message through `render.text()` and exits 2, the way `afr case` handles errors before the store.
    *   **Output:** the report and `--json` follow "Report".

#### `src/flight_recorder/cli/__init__.py` (Modify)
*   **Changes:** `from flight_recorder.cli.eval import eval_app` and `app.add_typer(eval_app, name="eval")`.

#### `evals/cases/smoke/` (Create)
*   **Changes:** create `suite.yaml` and `smoke-01.yaml` to `smoke-08.yaml`, exactly as in "The in-repo smoke suite", and commit them. Then run `uv run afr eval run --suite smoke --accept` on the clean tree, so the header's `git_sha` isn't dirty, and commit `outputs.jsonl` and `baseline.json`.

#### Tests
*   `tests/test_eval_cli.py`, using `clean_env` with `AFR_HOME` set to `tmp_path`, so no real key is read or made, on a copy of the smoke suite in `tmp_path`:
    *   `run`, then `grade`, both exit 0;
    *   editing a case's input makes `grade` exit 1 and report it stale;
    *   editing a check makes both commands report changed checks and exit 1 until `--accept`;
    *   making smoke-05's check pass makes `grade` exit 1 but `run` exit 0;
    *   a new case and a removed case;
    *   `--accept` writes the baseline and exits 0, and refuses with exit 1 while a case is stale;
    *   `--json` keys;
    *   unreviewed cases with and without `--include-unreviewed`;
    *   the agent-mismatch warning;
    *   exit 2 with escaped text for a case id holding a control character;
    *   with `AFR_DATABASE_URL` pointing at an unreachable port, both commands still work, so neither opens the store.
*   **Checkpoint:** `uv run pytest` (the full suite), and `uv run afr eval grade --suite smoke` exits 0.

### Phase 5: CI and the agreement check

#### `.github/workflows/ci.yml` (Modify)
*   **Changes:** after the "Pytest" step, a step named "Eval harness (no agent calls)" that runs `uv run afr eval grade --suite smoke`.

#### `scripts/eval_agreement.py` (Create)
*   **Changes:** the script described in "Agreement check with the triager's grader". It uses `open_store(readonly=True)` and calls `grade_suite` directly. It has no test file. Its grading step is the tested grading code, but its own case building (the filters, decoding, splitting, and choosing `equals` or `one_of`) is checked only by its output in the PR. That trade-off is accepted for a script run by hand.
*   **Checkpoint:** `uv run pyright scripts/eval_agreement.py` (pyright's configured paths don't include `scripts/`), then run the script on the owner's store and save its output.

### Phase 6: docs

#### `docs/evals/harness.md` (Create)
*   **Changes:** the design explained end to end, as the portfolio write-up:
    *   the agent contract;
    *   the suite folder;
    *   the outputs file;
    *   checks and equality;
    *   outcomes, the baseline and the gate table;
    *   why `run` and `grade` gate differently;
    *   the smoke suite, and that its score says nothing about the triager;
    *   the agreement check and its result;
    *   the limits in Risks & Mitigations.

#### `docs/cli.md` (Modify)
*   **Changes:** an "`afr eval run` and `afr eval grade`" section covering options, the gate table, exit codes and an example report. The `afr case` section links to it, and notes that "(none)" from recorded decisions is now written as `null`.

#### `docs/PLAN.md` (Modify)
*   **Changes:**
    *   Update the #15 row (line 172) to read "Eval harness from scratch, for any agent", with a 3.5-hour estimate.
    *   Update step 5 "Run the suite" (line 73) to say that cases are graded by generic checks now, with the judge later.
    *   Update the `eval_results` row (line 83) to say the table is unused until #34.
    *   Key decision 7 (line 130) adds: "Results are files in git (`outputs.jsonl`) until #34 loads them into the database."

#### `docs/schema.md` (Modify)
*   **Changes:** the `eval_results` lines (9, 99–100) say the table is unused until #34 loads output files, and list the mismatches that loader must decide: five outcomes against one boolean, nullable `model` and `git_sha`, and no `suite_run_id` in the header.

#### `README.md` (Modify)
*   **Changes:** a line in the command list: "To run an agent against eval cases: `uv run afr eval run --suite smoke`. See docs/evals/harness.md."

### Verification Plan

*   **Lint and types:** `uv run ruff check`, `uv run ruff format --check`, `uv run pyright`, `uv run pyright scripts/eval_agreement.py`, and `uv lock --check` (no dependency change).
*   **Tests:** `uv run pytest` with `AFR_REQUIRE_DB=1`, phase by phase as above.
*   **Smoke end to end,** on a copy of the repo with no `.env` and `AFR_HOME` set to a temporary folder:
    *   `afr eval run --suite smoke` exits 0, and only the header line of `outputs.jsonl` changes;
    *   `afr eval grade --suite smoke` exits 0;
    *   editing one case's input makes `grade` exit 1, reporting the case stale.
*   **Agreement check:** the script's output on the owner's store, read-only, saved as PR evidence. Each disagreement is fixed, or documented in `docs/evals/harness.md`.
*   **CI:** the new step runs on the PR and passes.
