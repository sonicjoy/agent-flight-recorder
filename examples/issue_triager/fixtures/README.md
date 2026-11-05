# Triage fixtures

| File | What | Edit by |
|---|---|---|
| `issues.json` | This repo's GitHub issues with their real triage (area labels, `stretch`, milestone week, duplicate-of) | Re-running `uv run python scripts/export_issues.py` (read-only, needs `gh` logged in) |
| `synthetic.yaml` | About 60 hand-written cases: paraphrases of real issues, and tricky ones (near-duplicates, ambiguous area, cross-cutting, injection, out of scope) | Hand |

Load them with `issue_triager.fixtures.load_cases()`. Each case has a `truth` the agent is graded against; give the agent only `case.agent_input()` (id, title, body), never the target's labels or milestone.

Report scores separately for `source: real` and `source: synthetic`.

Duplicate vs regression (both link an earlier issue, never both at once):

- **Duplicate** asks for the same work again, done or not. Close it and link the original. Marked by a closing comment that starts `Duplicate of #N`, GitHub's own convention.
- **Regression** reports that work a closed, completed issue delivered has broken again. Keep it open as a bug. Marked by a line `Regression of #N` in the body or a comment.
- A bug in behaviour that never worked, or in unfinished work, is neither.
