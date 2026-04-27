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

Docker Compose includes a local dev service that implements the Hermes and Multica HTTP contracts
with non-production tokens. This is only for local smoke testing; replace
`ASDLC_HERMES_*` and `ASDLC_MULTICA_*` with real hosted service credentials before production use.

For real self-hosted Multica, configure the platform with the Multica backend URL, PAT, workspace
ID, and preferred runtime provider:

```bash
ASDLC_MULTICA_HTTP_ENABLED=true
ASDLC_MULTICA_BASE_URL=http://127.0.0.1:18080
ASDLC_MULTICA_API_KEY=<multica_pat>
ASDLC_MULTICA_WORKSPACE_ID=<workspace_uuid>
ASDLC_MULTICA_DEFAULT_RUNTIME_PROVIDER=codex
```

The adapter creates/reuses a deterministic Multica agent named
`<ASDLC_MULTICA_AGENT_NAME_PREFIX>-<provider>`, creates an assigned Multica issue for each DAG node,
then stores the real Multica issue, task, agent, runtime, and provider IDs on the DAG node metadata.

## Delivery Plan

See `docs/IMPLEMENTATION_BACKLOG.md`.

## Development Discipline

Implementation is test-first. Unit tests and Schemathesis contract tests are part of the default
quality gate. See `docs/TDD_WORKFLOW.md`.

Agent/runtime instructions are in `AGENTS.md`, `CLAUDE.md`, and `.agent/`.
