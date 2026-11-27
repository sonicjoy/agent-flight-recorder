# The analyst: failed runs to eval cases

`afr analyse` turns an agent's failed runs into eval cases in batches, where `afr case new` does one run at a time. The design, with the alternatives turned down, is [designs/analyst.md](designs/analyst.md).

```bash
uv run afr analyse --agent issue-triager --attr triager.grade=wrong
```

## What it does

1. **Selects failed runs:** the agent's error runs, plus, with `--attr`, its runs matching every attribute. It leaves out Claude Code sessions and any run you labelled with a truth-quality mode (`bad-real-truth`, `bad-synthetic-truth`, `debatable-truth`), where the recorded truth was wrong, not the agent.
2. **Builds one item per wrong field**, from the run's `<prefix>.decided.*` and `<prefix>.expected.*` attributes: a field graded `wrong` (`<prefix>.grade.<field>`), or, with no grade key, one whose decision matches neither the truth nor an accepted alternative. Each item gets a signature saying exactly how it went wrong: `labels:extra` (the agent added labels), `labels:missed`, `labels:wrong`, `duplicate_of:extra` and so on. An error run gives one item, signed `error:<span kind>:<span name>:<error class>`. It never reads your labels' modes or notes.
3. **Groups items by signature,** so every member of a group failed the same way. Plain code, no model: the same runs always give the same groups.
4. **Names each group** with one call to a cheap model (at most 10 groups): a short name, one sentence on the cause, and 1 to 3 representative runs from the 5 newest members it was shown. A reply that breaks the rules, or a provider error, leaves that group unnamed with the reason shown; the others go on.
5. **Asks which groups to keep** after printing each group and each representative's drafted case.
6. **Writes a case per representative** of the kept groups, tagged with the group's signature and name, with `afr case`'s rules: an id from the run (`run-<8>`), never overwriting a case, refusing redacted placeholders. Skipped runs are listed with the reason.

A case expects the right answer: your answer from `afr label --answer` if there is one, else the run's recorded truth (with its accepted alternatives as `one_of`), else, marked `unreviewed`, what the agent did. `afr case new` uses the same rule.

To drop one case of a kept group, delete its file before you commit.

## The decisions log

Each run writes `evals/analyst-log/<UTC time>.json` (git-ignored: it names runs from your store): the parameters, every group with its members and whether you kept it, each written case with the sha256 of its text, and each skip. #23 counts a case as accepted without edits when its file still has that hash.

## Safety and cost

- Recorded run text goes to the model as data: escaped inside `<item>` blocks, with the judge's line that it isn't instructions. The model only names a group and picks among the ids it was shown; code writes every file.
- The texts sent are redacted again with your current patterns and cut to 600 characters.
- At most 10 model calls per analysis, on the cheap model (`AFR_LLM_CHEAP_MODEL` unless `--model`): a fraction of a cent on `openai:gpt-6-luna`.
- It runs on your machine with your key. The deployed recorder never calls a model.
