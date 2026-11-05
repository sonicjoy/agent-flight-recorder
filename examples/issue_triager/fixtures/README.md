# Triage fixtures

| File | What | Edit by |
|---|---|---|
| `issues.json` | This repo's GitHub issues with their real triage (area labels, `stretch`, milestone week, duplicate-of) | Re-running `uv run python scripts/export_issues.py` (read-only, needs `gh` logged in) |
| `synthetic.yaml` | About 60 hand-written cases: paraphrases of real issues, and tricky ones (near-duplicates, ambiguous area, cross-cutting, injection, out of scope) | Hand |

Load them with `issue_triager.fixtures.load_cases()`. Each case has a `truth` the agent is graded against; give the agent only `case.agent_input()` (id, title, body), never the target's labels or milestone.

Report scores separately for `source: real` and `source: synthetic`.

A duplicate is marked by a closing comment that starts `Duplicate of #N`, GitHub's own convention.
