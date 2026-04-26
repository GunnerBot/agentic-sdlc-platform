# Agentic SDLC Platform

FastAPI + `uv` scaffold for the composable Agentic SDLC Platform. The service is the glue layer
between Linear, GitHub, Multica, Hermes, model routing, and deploy automation.

## Initial Scope

- Health/readiness endpoints
- Linear and GitHub webhook ingress
- HMAC signature validation primitives
- Channel routing placeholder
- DAG decomposition placeholder
- Auto-merge gate placeholder
- Cost router placeholder
- Deploy hook placeholder

## Local Development

```bash
uv sync
make quality
make migrate
uv run agentic-sdlc-platform
```

## Environment

Copy `.env.example` to `.env` and fill only the integrations you are testing locally.

## Delivery Plan

See `docs/IMPLEMENTATION_BACKLOG.md`.

## Development Discipline

Implementation is test-first. Unit tests and Schemathesis contract tests are part of the default
quality gate. See `docs/TDD_WORKFLOW.md`.

Agent/runtime instructions are in `AGENTS.md`, `CLAUDE.md`, and `.agent/`.
