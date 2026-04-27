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

Model-backed planning uses `ASDLC_MODEL_PROVIDER`. For OpenAI, set:

```bash
ASDLC_VENDOR_HTTP_ENABLED=true
ASDLC_MODEL_PROVIDER=openai
ASDLC_OPENAI_API_KEY=<openai_api_key>
ASDLC_OPENAI_DEFAULT_MODEL=gpt-5.5
ASDLC_OPENAI_FALLBACK_MODEL=gpt-5.4-mini
```

GitHub App read-only repo discovery uses `ASDLC_GITHUB_APP_*` settings. Put the App ID,
installation ID, and `ASDLC_GITHUB_APP_PRIVATE_KEY_PATH` in ignored `.env.local`, then store the
private key in an ignored local file such as `secrets/github-app.pem`; do not commit keys. Write
operations for automatic branch push and PR creation remain intentionally disabled until a separate
write-scoped GitHub App policy is approved.

For private repo checkouts, enable the git credential helper in ignored local config and install the
targeted helper:

```bash
ASDLC_GITHUB_APP_GIT_CREDENTIAL_ENABLED=true
ASDLC_GITHUB_APP_GIT_CREDENTIAL_ALLOWED_OWNERS=atlas-tech-inc
make github-app-git-credential-configure
```

The helper mints short-lived GitHub App installation tokens at clone/fetch time; do not commit
tokens or authenticated clone URLs.

Docker Compose includes a local dev service that implements the Hermes and Multica HTTP contracts
with non-production tokens. This is only for local smoke testing. To run the container against real
host-local services, use the real overlay:

```bash
make compose-real-up
```

The real overlay reads secrets from ignored `.env.local`, keeps Postgres inside Compose, and points
the app container at host-local services with `host.docker.internal` because `127.0.0.1` inside a
container means the container itself.

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

Graph-backed repo questions use the provider-neutral `GraphStore` port. Graphify is the first real
adapter. By default it runs the official `graphify` CLI, so repo metadata must include either
`graph_path`, `graphify_graph_path`, `local_path`, `repo_path`, or `workspace_path`:

```bash
ASDLC_VENDOR_HTTP_ENABLED=true
ASDLC_GRAPHIFY_MODE=cli
ASDLC_GRAPHIFY_COMMAND=graphify
```

CLI query mode runs `graphify query <question> --graph <graphify-out/graph.json>`. CLI index mode
runs `graphify update <repo_path>`. A compatible self-hosted HTTP wrapper can be used
with `ASDLC_GRAPHIFY_MODE=http` and `ASDLC_GRAPHIFY_BASE_URL`; it must expose `POST /api/index` and
`POST /api/query` using the internal GraphStore request/response shape.

Graphify output is generated local index data and must not be committed. The repository ignores
`graphify-out/` and `.graphify/`; store graph paths in ignored local config or repository metadata,
not as checked-in artifacts.

In the real Docker overlay, host repos are mounted read-only at `/repos` and generated Graphify data
is written to the Docker-managed `/graphify-data` volume. Example repo metadata for the local
`keychain-os-erp` checkout:

```json
{
  "local_path": "/repos/keychain-os-erp"
}
```

Indexing copies that read-only repo into `/graphify-data/keychain-os-erp/` and creates
`/graphify-data/keychain-os-erp/graphify-out/graph.json`. Nothing is written back to the host repo
checkout, and nothing under `graphify-out/` is committed.

## Delivery Plan

See `docs/IMPLEMENTATION_BACKLOG.md`.

## Development Discipline

Implementation is test-first. Unit tests and Schemathesis contract tests are part of the default
quality gate. See `docs/TDD_WORKFLOW.md`.

Agent/runtime instructions are in `AGENTS.md`, `CLAUDE.md`, and `.agent/`.
