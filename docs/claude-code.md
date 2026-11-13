# Recording Claude Code sessions

Claude Code hooks send each session to the recorder as a run: your prompts, the tool calls, any subagents, and the model responses with their tokens and cost.

```bash
uv run afr serve                                   # the recorder, on 127.0.0.1:4318
uv run afr hook install --project path/to/project  # this project's sessions only
```

Then use Claude Code in that project as usual. To stop recording, run `uv run afr hook uninstall --project path/to/project`.

## Only where you choose

`afr hook install` writes to the project's `.claude/settings.local.json`:
- **Project-level:** only sessions started in that folder are recorded.
- **Personal:** Claude Code doesn't commit it, and the command holds this machine's path to Python.
- **Other hooks are kept.** Install again to update ours; uninstall removes only ours.

It refuses to install in your home folder, which would amount to user-level settings. **Don't add these hooks to user-level settings (`~/.claude/settings.json`), especially on a work machine:** every session on the machine would be recorded, including work you mustn't copy anywhere.

## It never gets in your way

| | |
|---|---|
| Async | Every event except SessionEnd runs as an `async` hook, so Claude Code doesn't wait for it. SessionEnd runs once, at exit: it sends only its own event (no backlog), with a 0.5 s send timeout, well inside the 1.5 s Claude Code allows SessionEnd hooks. It stays synchronous because under `claude -p` Claude Code kills async hooks still running at exit, and the run would then never be closed |
| Fails open | The hook always exits 0, so it never blocks anything. It prints nothing; an async hook's output would reach the model. Errors go to `~/.afr/hook.log`, without content |
| Recorder down | Events are kept in `~/.afr/spool.jsonl` and sent, oldest first, by the next async event that gets through. A backlog goes in chunks of at most 50 events, 4 MB and an estimated 2 s of the recorder's time (30 ms per event, 8 ms per model response or decision, measured on Windows and rounded up), so each chunk finishes within the 3 s timeout and under the recorder's 10 MB request limit. `tests/ingest/test_limits.py` checks these limits against each other and times a full chunk against the real app |
| Refused | If the recorder refuses the events themselves (400, 413 or 422), they're dropped and logged, not retried for ever. Anything else, such as a 404 from an older recorder, a 421 from a host name in `AFR_HOOK_URL`, a 5xx or no answer, keeps them. The recorder answers 503 when the database can't take writes at that moment (unreachable, a lock, a deadlock, a timeout; see [ingest.md](ingest.md)), so those events are sent again. It also answers 503, and logs why, for an error no event should cause, such as a bug or a database behind on its migrations: the events wait in the spool, not lost, until it is fixed. Only an event the recorder finds wrong in itself is rejected and dropped, with the reason in `hook.log`. One hook sends at most 10 chunks; a bigger backlog carries on with the next hook, so progress is never lost |
| Capped | The spool keeps at most 20 MB, dropping its oldest events, also when SessionEnd is the event that doesn't fit; anything dropped is logged |
| Crash-safe | A hook killed mid-send leaves its batch claimed; the next hook sends it once it's two minutes old. A turn end's transcript position is saved only once all its parts are sent or spooled, and only as far as they reach, so a killed hook's token usage is read again later. Transcript paths aren't sent: the recorder doesn't need them |
| Isolated | The hook runs as `python -I -m flight_recorder.hook`. Claude Code runs hooks in the project folder, and without `-I` a file such as `json.py` or `hmac.py` there would replace the standard module and run, as you, on every event, outside Claude Code's approvals |
| Cost per event | About 260 ms of process time in the background (measured on Windows), or about 0.7 s for SessionEnd when the recorder is down |

## What is recorded

```
Claude Code session (agent)            SessionStart ... SessionEnd; your first prompt, the last reply
  turn (step)                          your prompt in, Claude's last reply out
    command: /review (other)           a slash command, with its arguments and the prompt it became
    claude-opus-5-5 (llm)              one per model response, from request sent to last block:
                                       what it said, its thinking, the tools it called, tokens, cost
    Read / Bash / Edit ... (tool)      input and output; failures are `error`, refused calls `rejected`
      permission: Bash (step)          waiting for your approval, until the tool started
    Explore (agent)                    a subagent: SubagentStart ... SubagentStop
      Grep ... (tool), ... (llm)
    notification: idle_prompt (other)  what Claude Code told you
  compaction (auto) (step)             the context was compacted: instructions in, summary out
```

The trace is meant to tell the whole story of a session: what you asked, what Claude said and decided at each step, what ran and for how long, where it waited for you, and when its context was compacted.

- **Model responses** come from the transcript, which records when each request was sent (`api-request` lines, linked by `requestRef`), so an llm span covers the model's whole time, not just its streaming. `first_output_at` and `stop_reason` are in its attributes.
- **Waiting for permission:** a PermissionRequest carries no tool_use_id, so it's matched to the latest call of that tool, with the same input, that was still running at the request. If the request arrives before the tool's start, it waits under the turn, and the tool's end moves it under the tool. The wait ends when the tool started: PostToolUse's time minus its `duration_ms`, but never before the wait began.
- **Who allowed or refused each call:** hooks don't say, so the hook reads it from the transcript at each turn end: every tool result records the decision and who made it. It's stored on the tool span (`attributes.permission`: `by` is "you", "auto mode", "a permission rule", "the permission mode", "a safety check" or, for anything else, "Claude Code"; plus the raw `source`, `reason_type` and `reason_code`). A refused call, and its wait, get the span status `rejected` (spans only; runs keep `unset`/`ok`/`error`), with `error` saying who refused it and keeping any detail (the auto-mode reason from PermissionDenied, or your feedback). An auto-mode denial (PermissionDenied) is `rejected` too. A rejected call isn't a failure: it's left out of `status = 'error'` queries.
- **Read twice or in parts:** a response split across two turn-end reads keeps its request time, its earliest output and everything it said. Request times are kept between reads, since a queued prompt's request line can be read a turn early. The session keeps its earliest prompt and latest reply, however late events arrive.
- **No loose ends:** if a span's turn or subagent hasn't arrived (its event was lost or is late), a placeholder parent is created under the session, and the real event fills it in later. A subagent with no `agent_type` is named `subagent <first 8 characters of its id>`.
- **Compaction:** PostCompact closes the latest compaction still open.
- **Two clocks:** each hook process stamps its own time, about 100 ms late, while decisions and model responses carry the transcript's time. When a span's end would fall before its start (a call refused at once, or a fast tool whose PostToolUse was stamped first), it ends when it started: no span ends before it starts, so no event is refused for it.

| Run field | From |
|---|---|
| `source`, `external_id`, `agent` | `claude_code`, the session id, `claude-code` |
| `name` | The first line of your first prompt (80 characters) |
| `attributes` | `claude_code.cwd`, `claude_code.permission_mode`, `claude_code.source` (startup, resume...), `claude_code.reason` (why it ended) |
| `status`, `ended_at` | Set by SessionEnd |

A turn that ends in an API error (StopFailure) is marked `error`.

**Content is redacted twice and capped.** The hook redacts before anything is sent or spooled, and the recorder redacts again before storing ([redaction.md](redaction.md)). Each string is capped at 32 KB, since a tool can return a whole file. Both sides use the key in `~/.afr/redaction.key`, so their placeholders match. The hook sends the counts of what it redacted with each event, and the recorder stores them on that event's span; without them, Claude Code runs would show almost no redactions, since the server finds nothing left to replace. On Linux and macOS, `~/.afr` and the spool are owner-only (0700 and 0600).

## Tokens and cost

Hooks don't carry token counts; the session transcript does. At each turn end (Stop, SubagentStop and SessionEnd), the hook reads the transcript lines written since last time and sends one entry per model response, and the permission decisions (together, "items"):
- **Tokens:** input, output, cache reads, and 5-minute and 1-hour cache writes.
- **Fast mode,** if used.
- **Repeated lines** are counted once: a response can span several transcript lines.

**A turn end goes in parts.** Each part is the same hook event with at most 100 items and 2 MB; the recorder merges them, so a long turn can't outgrow the timeout or the 10 MB request limit, and nothing is cut or refused whole. A response too big for any part (its tool calls' inputs over 2 MB) keeps its tokens, and its output is replaced by a note saying so. One hook reads at most 1,000 items; the position is saved there and the next turn end carries on. That matters for a session started before the hooks were installed: its first turn end reads from the beginning of its transcript, a page at a time. SessionEnd sends its own event first, alone, so the run is closed within its 0.5 s, and leaves the responses it read to the next async event. What is still dropped is logged in `hook.log`: an output too big to send, items past what one read takes, events the recorder rejected (with its reasons, which are field and type names, never content) and items the recorder cut. The recorder takes at most 1,000 items per event, and its reply's `dropped_items` counts any past that.

The recorder stores each response as an `llm` span and prices it at Claude's API rates, cache included ([llm-providers.md](llm-providers.md)). On a subscription, that's the API-equivalent cost, not what you paid.

**Cache-aware pricing matters a lot here.** In a real 744-response session, cache reads were 98% of 226 million input tokens. That's about $104 at cache rates, against about $905 if every input token were priced as fresh input.

**The transcript format is internal to Claude Code and can change.** The reader skips lines it doesn't recognise, and never fails the hook; if a format change breaks it, tokens stop being recorded while everything else carries on. Messages Claude Code writes itself, such as a login error (model `<synthetic>`), aren't counted as model calls.

## Settings

The hook runs in other projects' folders, so it reads only environment variables, never a `.env` file:

| Variable | Default |
|---|---|
| `AFR_HOME` | `~/.afr`: the key, spool, transcript positions and log |
| `AFR_HOOK_URL` | `http://127.0.0.1:4318/v1/claude-code/events`. A loopback address bypasses any system proxy |
| `AFR_REDACTION_KEY` | The key file in `AFR_HOME`. Set it, and `AFR_HOME`, in the environment, not in the recorder's `.env`: the server reads `.env` but the hook doesn't, and the two would then use different keys |

## Checked against Claude Code 2.1.321

A headless session in this repo sent real events, which the recorder mapped as above: the run was named from the prompt, the working directory and permission mode were stored, and the turn was marked `error`. The CLI wasn't logged in, so that turn ended in an API error and fired `StopFailure`. That run is how the `<synthetic>` messages were found. The reader was also checked on a real 22 MB transcript: 744 responses in 0.11 s, and 20 ms when nothing was new.
