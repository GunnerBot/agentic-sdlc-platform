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

Copy `.env.example` to `.env` and use ignored `.env.local` overrides for machine-local secrets.
Set `ASDLC_CHANNEL_MAPPING_PATH=config/channel-mapping.example.toml` to enforce channel
allow-lists and repo mapping for Slack and Telegram.

GitHub App read-only repo discovery uses `ASDLC_GITHUB_APP_*` settings. Put the App ID,
installation ID, and `ASDLC_GITHUB_APP_PRIVATE_KEY_PATH` in ignored `.env.local`, then store the
private key in an ignored local file such as `secrets/github-app.pem`; do not commit keys. Write
operations for automatic branch push and PR creation remain intentionally disabled until a separate
write-scoped GitHub App policy is approved.

## Delivery Plan

See `docs/IMPLEMENTATION_BACKLOG.md`.

## Development Discipline

Implementation is test-first. Unit tests and Schemathesis contract tests are part of the default
quality gate. See `docs/TDD_WORKFLOW.md`.

Agent/runtime instructions are in `AGENTS.md`, `CLAUDE.md`, and `.agent/`.
