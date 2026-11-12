# Looking at recorded runs

Three commands read the run store (`AFR_DATABASE_URL`, default: compose.yaml's Postgres). They only read: each one opens a read-only transaction.

```bash
uv run afr runs list                        # the newest 20 runs
uv run afr runs show last                   # the newest finished run's span tree
uv run afr replay 7af8a849                  # step through a run, one span at a time
```

## `afr runs list`

Runs, newest first: id (first 8 characters), start time, agent, name, status, duration, tokens in/out and cost. On a narrow terminal the agent, tokens, duration and start time are dropped, in that order, so the id and cost always show whole and the name takes what's left.

| Option | |
|---|---|
| `--source` | `otel` or `claude_code` |
| `--agent` | The agent or service name, exactly (`claude-code`, `issue-triager`...) |
| `--status` | `ok`, `error` or `unset` for finished runs; `running` or `unfinished` for runs with no end yet |
| `--since`, `--until` | Start time: `30m`, `2h`, `3d` or `1w` ago; a date (`2026-11-10`, local midnight); or a local date and time (`2026-11-10T14:30`). `--since` is inclusive, `--until` isn't |
| `--limit` | How many runs (default 20, at most 1000) |

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

Steps through the run's spans in time order. Each step shows its time since the run started, where it is in the tree (`session › turn › Bash`, the nearest 6 levels), the span itself and its input and output. Input and output are shown as indented `key: value` lines, with multi-line strings (prompts, file contents) as real lines rather than `\n` escapes. Long content is cut so a step fits on one screen in a terminal, or at 40 lines or 4,000 characters when piped; `--full` shows it all. A span's content is read only when its step is shown.

- **In a terminal:** Enter, `n`, space, `j` or → for the next step; `p`, `b`, `k` or ← to go back; `q`, Esc or Ctrl+C to quit.
- **Piped or redirected:** one line per step, so a long run doesn't flood the reader; `--content` adds inputs and outputs.
- `--kind llm` steps through only the model calls (or any other kind).

Spans that start at the same moment go parents first, then by id. A placeholder parent (a turn whose event arrived late) starts with its earliest child.

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
- **Invisible characters**: zero-width spaces, byte-order marks, soft hyphens, line and paragraph separators, private-use characters and Unicode tag characters. Tags spell out text a person can't see but a model reads. All of them are shown as escapes, such as `\u200b` or `\U000e0041`.
- **Instructions aimed at an agent.** If an agent runs these commands, what it reads is data, not instructions. That's also why replay prints no content when piped unless you ask with `--content`.

If Postgres isn't running, the commands say so within a few seconds; if the schema is behind, they ask you to run `afr db upgrade`. A time too far back or ahead (`99999999w`, or before 1970 on Windows) is a usage error. Piping into `head` stops quietly.
