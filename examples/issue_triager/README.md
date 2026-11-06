# Issue triager (demo agent)

The agent whose runs Agent Flight Recorder records. It triages this repo's own issues: area labels, `stretch`, milestone week, and a possible duplicate or regression, then drafts a comment. It's deliberately plain, so it makes real mistakes worth recording.

```bash
uv run triager --issue 19 --model google_genai:gemini-3.5-flash-lite
uv run triager --issue s53 --model openai:gpt-5.4-nano --inject timeout,bad-data --rate 0.5 --seed 7
```

`--issue` takes a real issue number or a synthetic case id (`s01`-`s65`, see [fixtures](src/issue_triager/fixtures/README.md)). After a run it prints the decision next to the ground truth, which the agent never sees.

## Graph

```
load_issue -> triage (agent loop) -> validate --invalid--> triage (once) / fail
                                         | ok
                                         v
                                 draft_comment -> human_review (interrupt) -> apply | rejected
```

| Step | What |
|---|---|
| `triage` | LangChain `create_agent` with two tools and structured output (`TriageDecision`). The model decides when and what to search; at most 8 tool calls |
| Tools | `search_issues` (keyword BM25 over real issues) and `get_issue`. They read the fixtures only, and never show the issue being triaged |
| `validate` | Code rules: known areas, week 1-5 or null, stretch needs a week, linked issues exist, only done issues can regress. One retry with the broken rules as feedback |
| `draft_comment` | Cheap model; duplicates and regressions are worded as suggestions |
| `human_review` | LangGraph `interrupt`: y/n prompt, or `--yes` |
| `apply` | Dry run: writes the decision, comment, tool steps, injected failures and token usage to `runs/` (git-ignored). Nothing goes to GitHub |

## Options

| Option | Default | |
|---|---|---|
| `--model` | `AFR_LLM_MODEL` | `<provider>:<model>` for both steps |
| `--comment-model` | `--model`, else `AFR_LLM_CHEAP_MODEL` | |
| `--cheap` | off | Triage with `AFR_LLM_CHEAP_MODEL` (smaller models fail more) |
| `--inject` | none | Tool failures: `timeout`, `bad-data`, `empty`, `noise` |
| `--rate`, `--seed` | 0.3, random | Chance per tool call; seed for repeatable runs |
| `--yes` | off | Approve without asking (batch runs) |
| `--runs-dir` | `runs` | Where `apply` writes |
