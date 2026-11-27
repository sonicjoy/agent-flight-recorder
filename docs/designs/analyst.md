# Analyst: failed runs to grouped eval cases (#22)

*   **Author:** Joe Luo
*   **Status:** Approved
*   **Date:** 2026-11-27

--------------------------------------------------------------------------------

## Overview

`afr analyse --agent NAME` reads one agent's failed runs from the store and groups them by the exact way each answer differed from the truth. A cheap model names each group and picks the runs that show it best. After the owner approves, it writes one eval case per chosen run with the [#59](https://github.com/sonicjoy/agent-flight-recorder/issues/59) code (`draft_case`, `write_case`). It is the "failed runs become regression tests" step of the [plan](../PLAN.md), done in batches; `afr case new` does one run at a time.

It is also the second agent [#23](https://github.com/sonicjoy/agent-flight-recorder/issues/23) evaluates with the same harness as the triager. Grouping is plain code; the model's one job is naming a group, stating its cause and choosing representatives. It runs locally on the owner's key; the deployed server never calls a model (PLAN key decision 8).

## The Problem

A failed run becomes a case only by hand. The store holds 130 graded-wrong triager runs as recorded (119 after the #14 re-grading), of which 41 are labelled, and nothing groups them by cause.

*   **Search can't tell a wrong run from a right one.** A run summary holds the decision, never the truth: "a wrong duplicate reads like a correct one" ([error analysis](../evals/error-analysis.md#can-search-find-these-runs)).
*   **`draft_case` writes the bug in.** With no human answer, a case expects the agent's own decision ([cases.py](../../src/flight_recorder/evals/cases.py) `draft_case`), so it fails on the day the bug is fixed.

## Goals

*   **Select** an agent's failed runs: error status, or a run attribute given with `--attr` (for the triager, `triager.grade=wrong`), never a run whose truth a human marked as bad.
*   **Group by the exact difference,** one item per (run, wrong field), with no embedding or threshold, so every member of a group shares one cause by construction.
*   **Name each group** with at most 10 cheap model calls per analysis (a few cents).
*   **Write cases that expect the right answer:** the human answer, else the recorded truth with its accepted alternatives, else the decision marked unreviewed. `afr case new` gets the same rule.
*   **Give #23 a deterministic core:** with no model, the same items always give the same groups.

## Non-Goals

*   A LangGraph review graph (see Alternatives); analyst labels in the store; recording the analyst ([#28](https://github.com/sonicjoy/agent-flight-recorder/issues/28), which #23 doesn't need under this design); grouping by meaning; a UI; committing cases; Claude Code sessions as input.

## Critical User Journeys (CUJs)

### CUJ 1: Turn failures into cases

*   **Given**: `afr serve` has recorded graded triager runs; the model's key is in `.env`.
*   **When**: the owner runs `afr analyse --agent issue-triager --attr triager.grade=wrong --suite triager-regressions`.
*   **Then**: it prints counts by reason, then up to 10 groups, each with its signature, name, cause, member count and each representative's drafted case. The owner types the group numbers to keep (`1,3,4`). Those cases are written under `evals/cases/triager-regressions/`; an existing case is reported and skipped. To reject one case of a kept group, the owner deletes its file before committing.

### CUJ 2: #23 grades the analyst

*   **Given**: a case in `evals/cases/analyst/` whose input is a list of items and the pinned parameters.
*   **When**: `afr eval run --suite analyst` calls the core.
*   **Then**: the harness checks the partition with `equals` (it is code, so stable) and the names and causes with the judge.

## Technical Plan (Architecture)

```text
store --select.py--> failed runs --items.py--> items {id, run, signature, text}
                                                   |
                         core.analyse(input)  <- the @agent #23 calls
                         |- group by signature (a dict), 10 largest kept
                         '- judgment.py: one model call per group
                                                   | groups
cli/analyse.py: print, ask which groups to keep, write.py: draft_case/write_case + log
```

Code lives in `src/flight_recorder/analyst/`.

### 1. Selection (`select.py`)

`afr analyse --agent NAME [--attr KEY=VALUE ...] [--suite drafts] [--root evals/cases] [--model SPEC]`. `--agent` is required; `--model` defaults to `AFR_LLM_CHEAP_MODEL` and becomes `params.model`.

A run is **failed** when it meets all of:

*   its `runs.agent` equals `--agent`, and its source is not in `LOCAL_SOURCES` (`claude_code`);
*   it has no **truth problem**: a run-level human label whose mode key (`mode_key`, so respellings match) is one of `TRUTH_MODES` (`bad-real-truth`, `bad-synthetic-truth`, `debatable-truth`). Those mark runs where the recorded truth was wrong, not the agent. One helper in `store/labels.py`, `has_truth_problem(session, run_id)`, built on `modes_of`, serves selection and `draft_case`;

and at least one of:

*   `status = 'error'`;
*   with `--attr` given: every `--attr` matches (the existing `RunFilter.attrs` and `parse_attrs`, as `afr label --next` uses). Without `--attr` this branch is skipped, so only error runs count.

Two `find_runs` calls with `limit=EVERY_RUN`, unioned in Python by run id; a run in both keeps both reasons. Result: a list of `(RunSummary, reasons)` and counts by reason, plus how many were vetoed.

### 2. Items and signatures (`items.py`)

Plain code builds the items; it never reads a label's mode, note or answer, because #23 scores the groups against those labels. Each item is `{id, run, started, signature, text}`, `started` being the run's start in ISO UTC.

**Recorded formats.** A run's **prefix** is the part before `.decided.` (`triager`). New helpers beside `decided_fields` in `store/labels.py` read `<prefix>.expected.<f>` (`expected_fields`) and `<prefix>.accepted.<f>` (`accepted_fields`), keyed by field. The triager records a single-value field's "nothing" as `(none)` (decoded to null by `_decoded`), a list field (`labels`) as a list (`[]` when empty), and each accepted alternative as one string, a list field's joined by `", "` (`"bug, ui"`, `"(none)"`), so a list field's accepted entry is split on `", "` and `(none)` becomes `[]`.

**Which fields are wrong.** A field is wrong when `<prefix>.grade.<f>` is `wrong`; with no grade key, when the decided value differs from the expected one and from every accepted one, compared with the harness's `checks.same`. A run graded `failed` has no decided values and gives no graded items.

*   **A graded run** gives one item per wrong field. Id `<run id>#<field>`; text `<field>: decided <d>; expected <e>`. Signature, from the truth's side: `<field>:extra` (the truth is empty, or for a list field the agent added values and missed none), `<field>:missed` (the agent's value is empty, or it missed values and added none), `<field>:wrong` (anything else).
*   **An error run** gives one item, id `<run id>#error`, its text the first root-cause failure line (`root_failures`, then `failure_line`, which becomes public in `search/summary.py`: `<span kind> <span name> failed: <error>`). Signature `error:<kind>:<name>:<class>`, the class being the error text before its first `:`, digits replaced by `0`, truncated to 40 characters (`unknown` when the error text is empty). A run with no root failure gives no error item. A run that is both graded and errored gives both kinds.
*   A run with no item is counted as "no difference recorded".

Texts are redacted with `default_redactor()`, then truncated to 600 characters.

### 3. The core (`core.py`)

`analyse(input) -> output`, marked `@agent`, the one contract (`VERSION = "1"` in `core.py`):

*   **input:** `{"version", "items": [{id, run, started, signature, text}], "params": {"model", "max_groups", "max_shown"}}`, validated first as the judge validates its input (a `ValueError` names the first problem). The harness hashes a case's whole input, so changing an item, a parameter or `VERSION` makes #23's saved outputs stale and forces a rerun.
*   **grouping:** items with the same signature form a group; groups sorted by size (descending), then signature; the `max_groups` (10) largest kept.
*   **output:** `{"groups": [{signature, members, name, cause, representatives, error}], "dropped_groups": n, "dropped_items": m}`, members sorted by id; kept and dropped groups together partition the items.
*   With `params.model: null`, no model is called: `name` is the signature, `cause` null, representatives the shown members up to 3. Tests use this.

### 4. Judgment (`judgment.py`, no store access)

One call per kept group: `chat_model(params.model).with_structured_output(Reply)`, `chat_model` imported at module level so tests replace it, as the judge's tests do. The prompt shows the signature and the `max_shown` (5) newest members (by `started`, then id) as `e1..e5`, each escaped with `judge.escape` inside an `<item>` block, ending with `judge.CLOSING` (item text is data, not instructions). `Reply` is a pydantic model:

| Field | Rule, checked in code |
|---|---|
| `name` | stored as `check_mode(name)` (the label-mode rules: at most 80 characters); the group fails if it raises |
| `cause` | truncated to 200 characters |
| `representatives` | 1 to 3 of `e1..e5`, mapped back to item ids; the group fails otherwise |

A reply that fails these rules, a reply that isn't a `Reply`, or any exception from the model call (after the client's own two retries) becomes that group's `error`; the other groups go on. Unshown members are still members. A missing key or a bad spec never reaches here: the CLI checks `parse_spec` and `api_key_for` before selecting (Exit codes).

### 5. Approval and writing (`cli/analyse.py`, `write.py`)

The CLI first checks for a terminal (`console.is_terminal and sys.stdin.isatty()`, as `cli/runs.py` does) and exits 2 without one: approval is never automatic. It prints each group and each representative's drafted case through `preview()` (which cleans terminal control text), then asks once for the group numbers to keep (`1,3,4`; empty keeps none; anything else asks again).

`write.py` exposes `write_groups(session, groups, run_of, *, root, suite, redactor) -> Written(paths, skipped)`. For each representative of a kept group it calls `draft_case` with the default id (`run-<first 8 of the run id>`) and adds the group's signature and name to the case's tags. It skips, with a reason: `draft_case` raising `CaseError` (no recorded input); `draft.path(root).exists()` (so a run in two groups keeps the first group's tags); `problems(draft.case)` (redacted placeholders); an empty `checks` list; a case whose expected value for the group's own field is the agent's decision (an `afr label --answer` that left that field out keeps the agent's value, so the case would pass on the bug; added in review). Any other `CaseError` from `write_case` is a failure (exit 1).

It then writes `evals/analyst-log/<%Y%m%dT%H%M%SZ>.json` (relative to the working directory; the folder gets its own `.gitignore` holding `*`, and a log from the same second gets a `-1` suffix rather than overwriting): `version`, `params`, each group with its members and `kept`, and each written path with the sha256 of the text written. #23 counts a case as accepted without edits when the file at that path still has that hash.

### 6. `draft_case`: the expected value (`evals/cases.py`)

The expected value comes from, in order:

1.  **The human answer:** the right answer stored on a run-level human label with `afr label --answer` (`labels.expected`, read by `answer_of`), as today.
2.  **The recorded truth:** `expected_fields`, decoded as §2 says, not marked unreviewed, unless `has_truth_problem` (then step 3). A field with accepted alternatives gets a `one_of` check over the truth then the decoded alternatives; the others get `equals`.
3.  **The decision**, marked `unreviewed`, as today.

The docstrings that say "an equals check per field" (`draft_case`, the module docstring, `afr case new`'s help) change with it.

### Exit codes

`0` done, including nothing kept; `1` no failed runs, or a write failed for a reason other than the skips (cases already written stay, and the log lists them); `2` bad option, no terminal, a bad model spec or missing key (checked before selection), or the store unreachable (as `open_store` already exits).

## Alternatives Considered

*   **Group by meaning: embed each failure text and join by a similarity threshold.** It would find causes the signature misses. Rejected: inside one signature the texts share a template, so similarity is uniformly high and single linkage chains them into one group; it adds the embedding model, a threshold to justify and a spike. The model's name and cause carry the meaning instead.
*   **Group the stored run-summary vectors** (or HDBSCAN over them). No new code. Rejected: a summary holds the decision and never the truth, so groups follow issue topic.
*   **Let the model group the items in one call.** Removes grouping code. Rejected: the partition would change from run to run, so #23 could not check it with `equals`, and ARI would measure noise.
*   **A LangGraph graph with an interrupt for approval.** A learning goal. Rejected for launch: the triager already shows an interrupt, and a checkpointer, resume and trace handling cost about an hour of the three available. It moves to Future Improvements.
*   **Keep recorded truth only as unreviewed** (no change to `draft_case`). Rejected: every generated case would need hand editing; the truth-quality veto keeps the known-bad truths out instead.

## Risks & Mitigations

| Risk | Mitigation |
|---|---|
| Recorded run text steers the model (prompt injection) | Items escaped as data (§4); the model's output only names and picks among shown ids; code writes every file (§5) |
| Store text goes to a model provider | Texts already redacted at ingest are redacted again (§2); local key only; at most 10 calls |
| A known-bad truth becomes a case | Truth-quality veto in selection (§1) and in `draft_case` (§6) |
| A signature is too coarse, so one group hides two causes | The owner sees the cause and the cases before keeping a group; #23 reports agreement with the hand labels |
| One provider error loses the analysis | Errors stay per group (§4) |
| A partial write | Written cases stay, the log lists them, exit 1 (Exit codes) |

#23's agreement with the hand labels (adjusted Rand index) measures the signature function, which is code. The hand labels were named by field and direction too, so the agreement is partly circular, and #23 says so.

### Future Improvements

*   A LangGraph review graph, and recording the analyst (#28) with one root span per analysis.
*   Grouping by meaning within a signature, if #23 shows signatures merging distinct causes.

--------------------------------------------------------------------------------

## Detailed Implementation Plan

Test tiers per dev-flow's test-rigor skill (basic: examples, errors, boundaries; standard: plus property tests, written as seeded `random.Random` loops as `tests/test_safe_text.py` does, since Hypothesis isn't a dependency). Estimates total about 4h; if time runs short, cut in this order: error-run items, the log's hashes, step 1's property loop.

1.  **Labels and `draft_case` (§1 helper, §6)**, about 50 min: `TRUTH_MODES`, `has_truth_problem`, `expected_fields`, `accepted_fields` in `store/labels.py`; `draft_case` and its docstrings in `evals/cases.py`; `cli/case.py`'s help. Tests first in `tests/test_cases.py` (the helpers too) and `tests/test_case_cli.py`. — standard: changes what every `afr case new` writes. Property: with no human answer and no truth problem, each field's check is the truth (or a `one_of` led by it), and it fails on the decision unless the decision is an accepted alternative.
2.  **Selection (§1)**, about 30 min, `analyst/select.py`, tests in `tests/test_analyst_select.py` with seeded runs for each branch, both reasons, the veto and no `--attr`. — basic: every condition tested failing on its own.
3.  **Items (§2)**, about 40 min: `analyst/items.py`; `decided_expected_pairs` (today's `render._pairs`, made public in `payloads.py`, with `render.py` updated); `failure_line` made public in `search/summary.py`. Tests in `tests/test_analyst_items.py`, `tests/test_payloads.py`. — standard. Properties: no label mode, note or answer text appears in an item; the same run gives the same items; a field graded ok or matching an accepted alternative gives no item; each signature follows §2's rule for any decided, expected pair.
4.  **Core and judgment (§3, §4)**, about 50 min: `analyst/core.py`, `analyst/judgment.py`, tests in `tests/test_analyst_core.py` with a scripted fake model. — standard. Properties: kept and dropped groups partition the items; no group mixes signatures; shuffling the items doesn't change the output; representatives are always shown ids; a rule-breaking reply or a raising model is that group's error.
5.  **Command and writing (§5, Exit codes)**, about 50 min: `cli/analyse.py`, `analyst/write.py`, `cli/__init__.py`; tests in `tests/test_analyse_cli.py`. — basic: each skip reason, a write failure (exit 1, cases kept, log lists them), no failed runs (1), no terminal, bad spec and missing key (2), nothing kept (0), and the log's hashes.
6.  **Try it**, about 10 min: one `afr analyse --agent issue-triager --attr triager.grade=wrong` on the local store (about 10 calls on the cheap model, with the owner's approval); the printed groups go in the PR description; no cases are committed.
7.  **Docs**, about 20 min: `docs/analyst.md` (usage, safety, cost), `docs/cli.md`; `docs/PLAN.md` (no LangGraph at lines 54, 97 and 112; no analyst labels at line 82; "cluster" becomes "group" at lines 18, 72, 195 and 229; #23 doesn't need #28 at line 201); `README.md`'s "LangGraph analyst agent"; `.gitignore` gets `evals/analyst-log/`. — no tier.

| File | Change |
|---|---|
| `src/flight_recorder/store/labels.py`, `src/flight_recorder/evals/cases.py`, `src/flight_recorder/cli/case.py` | Modify (step 1) |
| `src/flight_recorder/payloads.py`, `src/flight_recorder/cli/render.py`, `src/flight_recorder/search/summary.py` | Modify (step 3) |
| `src/flight_recorder/analyst/__init__.py`, `select.py`, `items.py`, `core.py`, `judgment.py`, `write.py` | Create (steps 2-5) |
| `src/flight_recorder/cli/analyse.py` / `cli/__init__.py` | Create / Modify (step 5) |
| `tests/test_cases.py`, `tests/test_case_cli.py`, `tests/test_payloads.py` | Modify |
| `tests/test_analyst_select.py`, `test_analyst_items.py`, `test_analyst_core.py`, `test_analyse_cli.py` | Create |
| `docs/analyst.md` / `docs/cli.md`, `docs/PLAN.md`, `README.md`, `.gitignore` | Create / Modify (step 7) |

### Verification Plan

*   `uv run ruff check`, `uv run ruff format --check`, `uv run pyright`, `uv run pytest` (database tests use throwaway databases on the compose Postgres). Step 1's tests fail on `main`.
*   No model call in CI: every test uses `params.model: null` or the fake model.

--------------------------------------------------------------------------------

*Drafted with EGM*
