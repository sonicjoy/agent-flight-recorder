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

## Roadmap

- [ ] Recorder core: trace schema, ingest API, redaction, pgvector search, replay CLI
- [ ] Evals and guardrails: eval harness, golden cases, LLM-as-judge, red-team set, CI
- [ ] Analyst agent, MCP server, Docker + Terraform deployment to Cloud Run
- [ ] Replay UI and dashboard
