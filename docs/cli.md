# Looking at recorded runs

These commands use the run store (`AFR_DATABASE_URL`, default: compose.yaml's Postgres). `runs list`, `runs show` and `replay` only read: each one opens a read-only transaction. `label` and `labels` write labels, and nothing else ([Labelling runs](#labelling-runs)).

```bash
uv run afr runs list                        # the newest 20 runs
uv run afr runs show last                   # the newest finished run's span tree
uv run afr replay 7af8a849                  # step through a run, one span at a time
```

## `afr runs list`

Runs, newest first: id (first 8 characters), start time, agent, name, status, duration, tokens in/out and cost. On a narrow terminal the agent, tokens, duration and start time are dropped, in that order, so from about 50 columns up the id and cost show whole and the name takes what's left.

| Option | |
|---|---|
| `--source` | `otel` or `claude_code` |
| `--agent` | The agent or service name, exactly (`claude-code`, `issue-triager`...) |
| `--status` | `ok`, `error` or `unset` for finished runs; `running` or `unfinished` for runs with no end yet |
| `--since`, `--until` | Start time: `30m`, `2h`, `3d` or `1w` ago; a date (`2026-11-10`, local midnight); or a local date and time (`2026-11-10T14:30`). `--since` is inclusive, `--until` isn't |
| `--limit` | How many runs (default 20, at most 1000) |
| `--attr KEY=VALUE` | A run attribute equals the value, compared as text (`--attr triager.grade=wrong`). Different keys must all match; repeat a key for any of several values (`--attr triager.batch.group=real-mini --attr triager.batch.group=real-gemini`) |

**Running or unfinished:** a run with no end recorded is `running` if it, or any of its spans, changed in the last 30 minutes, else `unfinished`. (Spans count because writing one doesn't always touch the run's own row.) A run whose end never arrives stays unfinished, rather than looking as if it's still going:
- a Claude Code session whose SessionEnd was lost (the terminal was killed, or the event was spooled and never sent);
- an OTel trace whose root span never arrived (only the root says when a run ended).

## `afr runs show <run>`

The run's totals, then its span tree. Each span shows its kind, name, model, status, duration, tokens (with cache reads) and cost. Error and rejected spans show their error underneath.

```
spans
└── step issue_triager  ok  8.4s
    ├── step load_issue  ok  0ms
    ├── step triage  ok  6.9s
    │   └── agent triage_agent  ok  6.9s
    │       ├── step model  ok  2.5s
    │       │   └── llm ChatOpenAI  openai:gpt-5.4-mini  ok  2.5s  1.0k/60 tok  $0.0010
    │       ├── step tools  ok  1ms
    │       │   └── tool search_issues  ok  0ms
    ...
```

| Option | |
|---|---|
| `--kind` | Only spans of this kind (`agent`, `llm`, `tool`, `step`, `other`), with their parents |
| `--errors` | Only error and rejected spans, with their parents |
| `--depth N` | Show N levels; deeper spans are summarised. The tree never draws more than 40 levels |
| `--all` | Don't fold long lists |

**Long lists are folded.** A long Claude Code session can have hundreds of spans under one turn. A span with more than 20 children shows the first 5, the last 5, and any child with an error or rejection at or below it. The rest become one line, such as `… 212 more (150 llm, 62 tool)  3.4M/12k tok  $12.30`, so no tokens or cost go missing.

## `afr replay <run>`

Steps through the run's spans in time order. Each step shows its time since the run started, where it is in the tree (`session › turn › Bash`, the nearest 6 levels), the span itself and its input and output. Input and output are shown as indented `key: value` lines. A multi-line string (a prompt, a file) is a block under `|`, as real lines rather than `\n` escapes. A string that could pass for something else, such as `"5"`, `"null"`, `""`, `"- x"` or anything with `: ` in it, is quoted, and so is such a key. Long content is cut so a step fits on one screen in a terminal, or at 40 lines or 4,000 characters when piped; `--full` shows it all. A span's content is read only when its step is shown.

- **In a terminal:** Enter, `n`, space, `j` or → for the next step; `p`, `b`, `k` or ← to go back; `q`, Esc or Ctrl+C to quit.
- **Piped or redirected:** one line per step, so a long run doesn't flood the reader; `--content` adds inputs and outputs.
- `--kind llm` steps through only the model calls (or any other kind).

Spans that start at the same moment go parents first, then by id. A placeholder parent (a turn whose event arrived late) starts with its earliest child.

## Labelling runs

Hand error analysis (#14): read runs and name what went wrong, before building any metric. Labels go in the `labels` table with source `human` ([schema.md](schema.md#labels)).

```bash
uv run afr label 7af8a849                     # show the run, then ask for a failure mode and a note
uv run afr label 7af8a849 --mode false-duplicate --note "#12 is related, not the same"
uv run afr label --next --attr triager.grade=wrong --sample 50 --by triager.batch.group
uv run afr labels list                        # modes, most common first, with an example run
```

### `afr label <run>`

Shows the run so it can be judged from the screen, top to bottom, then asks for a **failure mode** and a **note**:

1. **`agent vs truth`.** An attribute `<prefix>.decided.<field>` that has a matching `<prefix>.expected.<field>` is one row, with both sides named: `triager labels: agent evals, recorder  ≠  truth demo-agent`. The heading says where the truth came from when the run has `<prefix>.case_source`, and a synthetic case stands out: `agent vs truth · truth from a synthetic case (s65)`. Any agent can use the convention; the triager records it on its root span.
2. **`task`.** What the agent was asked: the first model call's last human message (LangChain's chat prompt), or else the entry span's input.
3. **`steps`.** The model and tool calls in time order; a run with more than 12 shows its first 8 and last 4, so the final decision stays on screen. A model call shows its tool calls with their arguments, such as the decision and its rationale, or its text. A tool call shows its input and result; a list of records, such as search hits, is shown as `#id title (labels; milestone)`. An empty result says `(no results)`. Other shapes are shown as their text.
4. **`problems`.** The error spans, with their parents.
5. **`attributes`.** The rest, dimmed. SDK attributes (`telemetry.*`, `service.instance.*`, `openinference.*`) are left out.
6. **`labels`.** The run's labels so far, each with its right answer if one was given.

All recorded text was redacted at ingest, and control characters are shown as escapes.
- **The mode:** type a number to reuse a mode (they're listed most used first, with how many runs have each), or type a new name: up to 80 printable characters, with a letter or digit, not a number and not `s` or `q`. Answer `q` to stop without labelling.
- **One mode, whatever its spelling:** a mode is stored as its words joined with `-`. Words are split at ASCII punctuation and spaces, and only A–Z is lowercased, so `Tool Timeout` and `tool_timeout` are `tool-timeout`. Word order doesn't count either: `timeout-tool` is the same mode, and takes the spelling the mode already has. Any other character is part of a word as typed: `x²` and `x` are different modes, and non-ASCII letters keep their case (`ΟΔΟΣ` isn't `οδος`).
- **The note** is optional, and redacted before it's stored, like everything else a person or model may copy from a run.
- **Several modes:** after each mode and note it asks for another; press Enter when there are no more. Each is saved as it's answered, so `q` (or the end of piped input) here keeps those already given, and stops `--next`. A mode the run already has is refused and asked again: a run (or one span) has each mode once, which the database enforces even with two people labelling at once ([schema.md](schema.md#labels)).
- **The right answer** (optional, #59): after the last mode it asks once what the agent should have done, so `afr case` can turn the run into an eval case for an agent with no truth dataset. A run with `<prefix>.decided.<field>` attributes shows the agent's decision and takes the fields that were wrong as `field=value; field=value` (a list field takes comma-separated values, `(none)` for empty); the fields not named keep the agent's value, and each value takes the type of the agent's (a number, `true`/`false`). A field is named as after `.decided.`, dots included. A run without takes free text. Press Enter to skip; `q` stops like after a mode. It's redacted like the note and stored on the run's first new label. A run keeps one answer, the last one given. Merging or removing its label moves it to another of the run's labels; only removing them all loses it. Not asked for a span. A script that pipes answers in needs one more line (empty) per run.

| Option | |
|---|---|
| `--mode`, `--note` | Skip the questions (for scripts, or an agent driving the CLI); one mode per call, and a mode the run already has adds nothing. `--note` alone is the first mode's note |
| `--answer` | With `--mode`: the right answer, in the same form as asked. A mode the run already has gets the answer on its label. Without it, `--mode` asks nothing |
| `--span` | Label one of the run's spans instead, by an id prefix; a span of another run is refused |
| `--author` | Who's labelling. Default: the OS user name. It stays in the local store |

### `afr label --next`

Walks a sample of runs, showing each run that has no human label yet and asking for its modes and notes. Answer `s` to skip a run or `q` to stop; run it again to carry on where you left off. It starts with how many are done (`12 of 50 labelled`).

| Option | |
|---|---|
| `--attr`, `--agent` | Which runs: the same filters as `runs list` |
| `--sample N` | Label only N of them (default: all) |
| `--by KEY` | Sample each value of this attribute in proportion to its share (largest remainder), e.g. one batch group in five gets a fifth of the sample |
| `--seed` | The sample's seed (default 14). The sample is the same every time for the same runs, filters and seed. Each run's place comes from a hash of the seed and its id, so a run recorded later costs the sample at most one pick |

The #13 corpus's sample ([evals/corpus.md](evals/corpus.md)):

```bash
uv run afr label --next --attr triager.grade=wrong \
  --attr triager.batch.group=real-mini --attr triager.batch.group=synthetic-nano \
  --attr triager.batch.group=injected-mini --attr triager.batch.group=real-gemini \
  --sample 50 --by triager.batch.group
```

### `afr labels`

| Command | |
|---|---|
| `afr labels list` | Each mode with its runs, labels and first labelled run. `--source judge` or `analyst` for model labels (judge labels come with #28; analyst #22) |
| `afr labels rename OLD NEW` | Renames a mode on human labels (OLD in any spelling), or merges it into an existing one. A run (or span) that had both keeps one label, and the merged label's note joins it, tagged (below), so no note is lost. NEW with the same words as OLD respells it. For open coding: name freely at first, then settle the names |
| `afr labels remove <run>` | Deletes the run's human labels; `--mode` only those with that mode, in any spelling |

**Merged notes.** A note that came from another label is tagged with that label's time, author and mode, from the label alone:

```
kept note; [afr-note v1 at=2026-11-13T10:01:00Z by=ann mode=a] note a; [afr-note v1 at=2026-11-13T10:02:00Z by=bob mode=b] note b
```

The tagged fragments are always kept sorted (by time first), so merging in any order, or in a chain (`a` into `b`, then `b` into `c`), gives the same note. `v1` is the tag's format version; values are percent-encoded for `%`, space, `;`, `[` and `]`. Only afr writes tags: `[afr-note v` typed into a note is stored as `\[afr-note v`, so a tag's `by=` can't be typed. A tag's `mode=` is the label's mode when it was merged.

## `afr case new`

Turns a recorded run into an eval case for any agent (#59), in the format the harness reads (#15):

```bash
uv run afr label 7af8a849                          # name what went wrong, and the right answer
uv run afr case new --from-run 7af8a849 --suite smoke
```

It writes `evals/cases/<suite>/<case-id>.yaml`:

```yaml
id: run-7af8a849
from_run: 4bf92f3577b34da6a3ce929d0e0e4736   # the run's trace or session id
tags: [extra-label]                           # the run's failure modes
input:
  prompt: "Triage this issue. ..."            # the entry span's input, else the first model call's human message; a dict input as it is
expected:
  labels: [evals]
  milestone: "Week 3"
checks:
  - {type: equals, field: labels, value: [evals]}
  - {type: equals, field: milestone, value: "Week 3"}
```

- **`expected`** is the run's right answer from its labels (a run keeps one, the last given), by field, or `answer: <text>`. With none, it's the run's recorded truth (`<prefix>.expected.<field>`), unless you labelled the run with a truth-quality mode (`bad-real-truth`, `bad-synthetic-truth`, `debatable-truth`). With neither, it's what the agent did and the case says `unreviewed: true`: fix it, then delete the line. What the agent did is its decided fields, else its output as recorded: the entry span's output, a Claude Code session's last reply, or for a LangChain run whose entry span recorded none, its structured output as fields (a tool call no tool ran), else what its last model call said, in full.
- **Types:** `expected` follows the attributes. The recorder's "nothing", the text `(none)` (span attributes can't hold null), is written as `null` when it comes from a decided field or a typed answer's field; free text keeps it. `null` matches an agent's `null`, a missing field or `[]` in [`afr eval`](#afr-eval-run-and-afr-eval-grade).
- **`checks`** is one `equals` per expected field, to edit by hand: free text rarely matches exactly. A field whose recorded truth has accepted alternatives (`<prefix>.accepted.<field>`, a list field's joined by `, `) gets a `one_of` over the truth and those alternatives instead.
- It prints the whole case first (control characters escaped), then asks `write it?`. Redaction catches secret shapes, not everything personal (home paths, names, hostnames, passwords written in prose), so read it before it goes into git.

**Safety.** Cases go into git, and the repo will be public. The rules are in `flight_recorder.evals.cases`, which the analyst (#22) uses too:

- **Redacted values** (`[REDACTED:kind:fingerprint]`) anywhere in the case, `expected` included, are listed and nothing is written. The case is redacted again first, with today's patterns, so a secret a run recorded before a pattern existed is caught here too. `--fake` swaps each for a fixed fake of its kind (`fake-anthropic-key`): on its own, neither the redactor nor gitleaks takes it for a secret. After a secret's name (`password=fake-secret-assignment`) the redactor still redacts it, as it would any value there. `--keep-placeholders` keeps them as `[REDACTED:kind]`, fingerprint stripped, and marks the case `placeholders: true`.
- **A Claude Code session** goes to `<root>/local/<suite>/`: its prompts and replies are your own work. `local/` holds a `.gitignore` of its own (`*`), written with the first case, so git ignores it wherever the cases folder is; `evals/cases/local/` is in the repo's `.gitignore` too. Move a case out only after rewriting it by hand.
- **Ids:** the suite and case id are 1 to 64 of `a-z`, `0-9`, `_` and `-`, starting with a letter or digit; `local`, `suite` (the suite's settings file) and Windows device names (`con`, `nul`, `com1`, ...) are refused. The file must resolve inside the cases folder, links included, and an existing case is never overwritten.
- YAML is written with `safe_dump`; read cases with `safe_load`.

| Option | |
|---|---|
| `--from-run` | The run, as for `runs show` |
| `--suite` | The suite folder (default `drafts`, for cases waiting for review) |
| `--id` | The case id (default `run-` and the first 8 characters of the run's id) |
| `--fake`, `--keep-placeholders` | What to do with redacted values (above) |
| `--root` | The cases folder (default `evals/cases`) |
| `--yes` | Write without asking |

## `afr analyse`

Groups an agent's failed runs by exactly how they went wrong and writes an eval case for each representative run of the groups you keep. See [analyst.md](analyst.md).

```bash
uv run afr analyse --agent issue-triager --attr triager.grade=wrong
```

`--agent` is required. `--attr KEY=VALUE` (repeatable, as in `afr label --next`) adds runs matching every attribute to the error runs; `--suite` (default `drafts`) and `--root` (default `evals/cases`) say where cases go; `--model` names the model that names groups (default `AFR_LLM_CHEAP_MODEL`). It needs a terminal: it asks which groups to keep and never keeps any by itself. Exit codes: 0 done, including nothing kept; 1 no failed runs, failed runs with no difference to group, a case write failed (cases already written stay, and the log lists them), or the log couldn't be written; 2 a bad option, no terminal, a bad model spec or missing key, or the store unreachable.

## `afr eval run` and `afr eval grade`

Runs an agent against a suite of cases and grades it against a baseline (#15). How it works, and why: [docs/evals/harness.md](evals/harness.md).

```bash
uv run afr eval run --suite smoke             # call the agent once per case, save outputs.jsonl, grade
uv run afr eval grade --suite smoke           # grade the saved outputs, calling nothing (what CI runs)
uv run afr eval run --suite smoke --accept    # keep these results as the new baseline.json
```

```text
suite smoke · agent flight_recorder.evals.example:triage 0.1.0 · model rules-v1 · git 6c1ac98 · ran 2026-11-17T14:01:12Z
5/8 passed · 2 failed · 0 error · 0 stale · 1 skipped
field       passed  graded
labels      4       5
meta.rules  1       1
type        3       4
smoke-05 type/acacb19c: expected ["bug", "performance"], got null (wrong value)
smoke-06 labels/f7099305: expected "documentation", got ["bug", "documentation"] (wrong value)
smoke-08 (unreviewed): skipped: unreviewed
```

The suite is `<root>/<suite>/` or `<root>/local/<suite>/`: a `suite.yaml` naming the agent (`agent: module:function`, a function marked with `@agent` from `flight_recorder.evals`; and an optional `model:` label) and one `<id>.yaml` per case. `run` writes `outputs.jsonl` there; `--accept` writes `baseline.json`. Neither command opens the store. The report lists changes against the baseline, each failing check as expected against actual, and agent output escaped like any recorded text.

How each change against the baseline gates each command:

| What changed | Reported as | `run` fails? | `grade` fails? |
|---|---|---|---|
| A check got worse: pass to fail or error, or fail to error | regression | yes | yes |
| A check got better: error to fail or pass, or fail to pass | improvement | no | yes |
| A case's checks were edited | changed checks | yes, until accepted | yes |
| A case not in the baseline | new case | no | yes |
| A case in the baseline whose file is gone | removed case | no | yes |
| A case in the baseline now skipped | skipped (in baseline) | no | no |
| A case whose input changed since its output was saved, or with no output | stale | yes | yes |

**Exit codes:** 0 when nothing above fails the command, when `--accept` wrote the baseline, or with no baseline yet and nothing stale (the report says to run `--accept`). 1 when a row fails the command, or `--accept` refused because a case is stale. 2 for usage errors: no such suite, a suite in both folders, a bad `suite.yaml`, agent or case file, a `.yml` file, a damaged `outputs.jsonl`, or a file that can't be written.

| Option | |
|---|---|
| `--suite` | The suite folder (required) |
| `--agent` | `module:function`, instead of `suite.yaml`'s agent (`run` only) |
| `--root` | The cases folder (default `evals/cases`) |
| `--include-unreviewed` | Grade cases marked `unreviewed: true` too; they never enter the baseline |
| `--accept` | Write `baseline.json` from these results (refused while a case is stale) |
| `--json` | Print one JSON object: `suite`, `header`, `counts`, `fields`, `cases`, `changes`, `exit_code` |

## `afr eval judge-suite` and `afr eval agreement`

The LLM judge is an agent under test (#17, [docs/designs/llm-judge.md](designs/llm-judge.md)): a judge suite has one case per item to judge, and the owner's hand grade as its only check. `judge-suite` writes that suite; `afr eval run` runs the judge; `agreement` measures how it agrees with the hand grades. Neither command calls a model or opens the store.

```bash
uv run afr eval judge-suite --items evals/judges/triager-comment/items.yaml \
    --rubric evals/judges/triager-comment/rubric.yaml --suite comment-judge
uv run afr eval run --suite comment-judge         # needs the key for the rubric's model
uv run afr eval agreement --suite comment-judge --field grade
```

`judge-suite` reads an items file (`items:`, a list of `{id, tag, item, grade}` with optional `targets`, `author` and `note`) and a rubric (`model`, `instructions`, `criteria`, optional `examples`). It writes `suite.yaml` and one case per item, deletes the folder's other case files, leaves `outputs.jsonl` alone, and never writes `baseline.json`: a judge suite never gates.

| Option (`judge-suite`) | |
|---|---|
| `--items` | The items file (required) |
| `--rubric` | The rubric file (required) |
| `--suite` | The suite folder to write (required) |
| `--root` | The cases folder (default `evals/cases`) |

**Exit codes (`judge-suite`):** 0 after writing. 1 when items are invalid (a bad or repeated id, an unknown key, a tag or grade not allowed, a malformed item, an unknown `targets`), listing every one; nothing is written. 2 for a missing or malformed file, no items, a rubric the judge would refuse (including a model spec with an unknown provider), a folder that holds another suite (its `suite.yaml` names another agent, or it has cases but no `suite.yaml`), or the suite also existing under `local/`.

`agreement` grades the saved outputs as `grade` does, then prints one row for all counted cases (`all`) and one per case tag (`tag <name>`; tags that aren't text are ignored): `n`, percent agreement, Cohen's kappa (`n/a` when nothing is counted or both raters used one and the same class), the catch rate (hand-graded failures the judge also failed), the pass rate, and the four counts. Then it lists every case left out and why: stale, error, skipped, no single `equals` check on the field, or a value not `pass` or `fail`.

| Option (`agreement`) | |
|---|---|
| `--suite` | The judge suite (required) |
| `--field` | The judge's graded field, `grade` (required) |
| `--root` | The cases folder (default `evals/cases`) |

**Exit codes (`agreement`):** 0 when no case was stale or errored (other left-out cases are listed but don't change it). 1 when a case was stale or errored: the numbers are partial, and printed anyway. 2 for usage errors, and for no `outputs.jsonl` at all, unlike `grade`, which reports every case stale: there is nothing to measure.

## Choosing a run

| You type | It means |
|---|---|
| `last` | The newest *finished* run. With hooks installed, the newest run is often the Claude Code session you're typing in, so it's skipped until it ends |
| `7af8a849` | A run whose id starts with this: at least 4 hex characters, hyphens optional. If several match, they're listed (the first 20) |
| `5d610b17...` (in full) | The source's own id: a Claude Code session id or an OTel trace id |

## Numbers

- **Tokens:** `in/out`. Input includes cache reads (the OTel GenAI convention), so `show` also says what share came from the cache: `158k in (80% cache reads), 31.4k cache writes · 439 out`.
- **Cost:** blank for spans that aren't model calls (tools, steps). It's never shown as $0 when unknown:
  - `unpriced`: a model call whose model isn't in `pricing.yaml`;
  - `$0.0094 + unpriced`: the known part of a run's cost. The stored run total is empty in that case, but the priced spans still add up;
  - `<$0.0001`: priced, but too small to show.
- **Duration:** `open` while a span or run has no end.
- **Times** are shown in your local time zone.

## Recorded text is untrusted

Everything shown was recorded from an agent: prompts, tool output, web pages. It was redacted before it was stored ([redaction.md](redaction.md)), so secret-shaped values never reach the screen. It could still contain things meant for a terminal or a model:
- **Escape codes** (clear the screen, set the window title, move the cursor), **right-to-left overrides** that disguise text, and Rich markup such as `[red]`. These are shown as text, never run: `\x1b[2J`, `\u202e`, `[red]` as typed. Newlines and tabs are kept.
- **Invisible characters**: zero-width spaces, byte-order marks, soft hyphens, line and paragraph separators, private-use characters and Unicode tag characters. Tags spell out text a person can't see but a model reads. So are Hangul fillers (letters that render blank) and runs of variation selectors, which can carry hidden bytes; a single selector after a character, as in most emoji, is kept. All of them are shown as escapes, such as `\u200b` or `\U000e0041`. Emoji joined with zero-width joiners show the joiners as `\u200d`.
- **Instructions aimed at an agent.** If an agent runs these commands, what it reads is data, not instructions. That's also why replay prints no content when piped unless you ask with `--content`.

If Postgres isn't running, the commands say so within a few seconds; if the schema is behind, they ask you to run `afr db upgrade`. A time too far back or ahead is a usage error: `99999999w`, or on Windows anything outside about 1970 to 3000 (Windows can't convert those to local time). Piping into `head` stops quietly.
