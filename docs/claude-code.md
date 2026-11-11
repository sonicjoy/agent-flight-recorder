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
| Async | Every event except SessionEnd runs as an `async` hook, so Claude Code doesn't wait for it. SessionEnd runs once, at exit, with a 0.5 s send timeout |
| Fails open | The hook always exits 0, so it never blocks anything. It prints nothing; an async hook's output would reach the model. Errors go to `~/.afr/hook.log`, without content |
| Recorder down | Events are kept in `~/.afr/spool.jsonl` (capped at 20 MB) and sent, oldest first, with the next event that gets through |
| Cost per event | About 260 ms of process time in the background (measured on Windows), or about 0.7 s for SessionEnd when the recorder is down |

## What is recorded

```
Claude Code session (agent)            SessionStart ... SessionEnd
  turn (step)                          your prompt in, Claude's last reply out
    Read / Bash / Edit ... (tool)      input and output; failures are errors
    claude-opus-5-5 (llm)              one per model response: tokens, cache, cost
    Explore (agent)                    a subagent
      Grep ... (tool), ... (llm)
```

| Run field | From |
|---|---|
| `source`, `external_id`, `agent` | `claude_code`, the session id, `claude-code` |
| `name` | The first line of your first prompt (80 characters) |
| `attributes` | `claude_code.cwd`, `claude_code.permission_mode`, `claude_code.source` (startup, resume...), `claude_code.reason` (why it ended) |
| `status`, `ended_at` | Set by SessionEnd |

A turn that ends in an API error (StopFailure) is marked `error`.

**Content is redacted twice and capped.** The hook redacts before anything is sent or spooled, and the recorder redacts again before storing ([redaction.md](redaction.md)). Each string is capped at 32 KB, since a tool can return a whole file. Both sides use the key in `~/.afr/redaction.key`, so their placeholders match.

## Tokens and cost

Hooks don't carry token counts; the session transcript does. At each turn end (Stop, SubagentStop and SessionEnd), the hook reads the transcript lines written since last time and sends one entry per model response:
- **Tokens:** input, output, cache reads, and 5-minute and 1-hour cache writes.
- **Fast mode,** if used.
- **Repeated lines** are counted once: a response can span several transcript lines.

The recorder stores each response as an `llm` span and prices it at Claude's API rates, cache included ([llm-providers.md](llm-providers.md)). On a subscription, that's the API-equivalent cost, not what you paid.

**Cache-aware pricing matters a lot here.** In a real 744-response session, cache reads were 98% of 226 million input tokens. That's about $104 at cache rates, against about $905 if every input token were priced as fresh input.

**The transcript format is internal to Claude Code and can change.** The reader skips lines it doesn't recognise, and never fails the hook; if a format change breaks it, tokens stop being recorded while everything else carries on. Messages Claude Code writes itself, such as a login error (model `<synthetic>`), aren't counted as model calls.

## Settings

The hook runs in other projects' folders, so it reads only environment variables, never a `.env` file:

| Variable | Default |
|---|---|
| `AFR_HOME` | `~/.afr`: the key, spool, transcript positions and log |
| `AFR_HOOK_URL` | `http://127.0.0.1:4318/v1/claude-code/events` |
| `AFR_REDACTION_KEY` | The key file in `AFR_HOME`. Set it in the environment, not in the recorder's `.env`, or the hook won't see it |

## Checked against Claude Code 2.1.321

A headless session in this repo sent real events, which the recorder mapped as above: the run was named from the prompt, the working directory and permission mode were stored, and the turn was marked `error`. The CLI wasn't logged in, so that turn ended in an API error and fired `StopFailure`. That run is how the `<synthetic>` messages were found. The reader was also checked on a real 22 MB transcript: 744 responses in 0.11 s, and 20 ms when nothing was new.
