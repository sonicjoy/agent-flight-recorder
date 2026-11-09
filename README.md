# Agent Flight Recorder

An open-source tool that records every AI agent run, replays it as a timeline, finds failure patterns, and turns failed runs into eval cases.

> Status: early development.

## What it does

- **Record**: capture runs from Claude Code hooks and OpenTelemetry traces through a FastAPI ingest API, with secrets and PII redacted before storage.
- **Store**: runs, spans, tool calls, tokens and cost in Postgres, with pgvector embeddings for searching runs by meaning.
- **Replay**: a CLI and a React timeline UI to step through any run.
- **Analyse**: a LangGraph analyst agent that clusters failed runs by cause and drafts eval cases.
- **Guard**: an eval harness and red-team set that run in CI so regressions fail the build.
- **Query**: an MCP server so agents can search past runs and ask "why did run X fail?"

## Development

Requires [uv](https://docs.astral.sh/uv/), Python 3.12+ and Docker (for the run store's Postgres).

```bash
uv sync                          # create .venv and install dependencies
uv run pre-commit install        # run checks on every commit
cp .env.example .env             # then fill in your keys
docker compose up -d             # Postgres 16 + pgvector on 127.0.0.1:5433
uv run afr db upgrade            # create the tables
uv run afr --help
```

The schema and how traces map onto it: [docs/schema.md](docs/schema.md).

Checks (the same ones CI runs on every pull request):

```bash
uv run pre-commit run --all-files   # everything, including gitleaks
uv run ruff check && uv run ruff format --check
uv run pyright
uv run pytest                       # database tests skip if Postgres isn't running
```

## Roadmap

See the [project plan](docs/PLAN.md) and [milestones](https://github.com/sonicjoy/agent-flight-recorder/milestones).

- [x] Demo agent: LangGraph issue triager, in its own repo: [issue-triage-agent](https://github.com/sonicjoy/issue-triage-agent)
- [ ] Recorder core: trace schema, ingest API, redaction, pgvector search, replay CLI
- [ ] Evals and guardrails: eval harness, golden cases, LLM-as-judge, red-team set, CI
- [ ] Analyst agent, MCP server, Docker + Terraform deployment to Cloud Run
- [ ] Replay UI and launch
