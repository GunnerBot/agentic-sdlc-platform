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
ASDLC_LINEAR_SPEC_PLANNER_ENABLED=true
ASDLC_LINEAR_PLAN_APPROVAL_REQUIRED=true
ASDLC_MODEL_PROVIDER=openai
ASDLC_OPENAI_API_KEY=<openai_api_key>
ASDLC_OPENAI_DEFAULT_MODEL=gpt-5.4-mini
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

To run the platform with the official self-hosted Hermes gateway instead of the local compatibility
service, set `ASDLC_HERMES_API_KEY` in ignored `.env.local` and start the Hermes overlay:

```bash
ASDLC_HERMES_HTTP_ENABLED=true
ASDLC_HERMES_API_MODE=openai_compatible
ASDLC_HERMES_API_KEY=<local_hermes_gateway_token>
ASDLC_HERMES_MODEL=hermes-agent
ASDLC_HERMES_INFERENCE_PROVIDER=custom
ASDLC_HERMES_INFERENCE_MODEL=gpt-5.4-mini
ASDLC_HERMES_TIMEOUT_SECONDS=120
make compose-real-hermes-up
```

The Hermes overlay runs `nousresearch/hermes-agent gateway run`, enables the OpenAI-compatible API
server on port `8642`, and wires the platform to `POST /v1/responses`. The overlay patches
Hermes' mounted `/opt/data/config.yaml` at startup so the runtime uses the configured
OpenAI-compatible endpoint/model through Hermes' `custom` provider.

LLM observability is persisted in task/session metadata as `llm_observability`. Request-side token
counts are estimated by `ASDLC_OBSERVABILITY_CHARS_PER_TOKEN`; cost estimates use
`ASDLC_OBSERVABILITY_INPUT_COST_PER_MILLION_USD` and
`ASDLC_OBSERVABILITY_OUTPUT_COST_PER_MILLION_USD`. Defaults track the public GPT-5.4 mini text-token
rate at the time this was added and should be adjusted if the runtime model changes. Per-task
totals are exposed at `GET /tasks/{task_id}/llm-observability`.

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

Linear spec ingestion can read linked Notion pages and Google Docs when configured. Put these in
ignored local config:

```bash
ASDLC_NOTION_HTTP_ENABLED=true
ASDLC_NOTION_API_KEY=<notion_internal_integration_secret>
ASDLC_GOOGLE_DOCS_HTTP_ENABLED=true
ASDLC_GOOGLE_DOCS_BEARER_TOKEN=<optional_google_oauth_token>
```

Notion links require the integration to be granted access to the page. Google Docs links are fetched
through the text export endpoint; private docs require a bearer token with access. Hydrated document
text is treated as an additional spec text source for repo detection, planning, Hermes context, and
DAG node metadata.

Linear spec ingestion can also hydrate Figma design links when configured:

```bash
ASDLC_FIGMA_HTTP_ENABLED=true
ASDLC_FIGMA_API_KEY=<figma_personal_access_token>
```

Figma links in Linear descriptions, comments, and attachments are fetched through the Figma files
API. The fetched file or node summary is treated as additional spec text, while the original Figma
URL remains tracked as a design asset for audit and DAG metadata.

Linear image attachments can be hydrated through an explicit OpenAI vision summarization path:

```bash
ASDLC_DESIGN_IMAGE_HYDRATION_ENABLED=true
ASDLC_DESIGN_IMAGE_SUMMARY_PROVIDER=openai
ASDLC_DESIGN_IMAGE_SUMMARY_MODEL=gpt-5.4-mini
ASDLC_DESIGN_IMAGE_MAX_BYTES=5000000
```

When enabled, image bytes are fetched only long enough to summarize them and are not persisted. The
stored context contains the generated summary plus metadata such as content type, byte count, and
summary model.

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

Linear assignment can ingest markdown specs directly from the issue description and text/markdown
attachments. When the spec names registered repos, the platform resolves the repo scope before
creating Multica work:

- Single registered repo: the task is bound to that repo even without a `repo:*` label.
- Multiple registered repos: the platform creates a `linear-spec` DAG with one ready node per repo.
- Design inputs: image attachments and Figma links are summarized into Hermes, Multica, DAG node,
  and audit metadata. Binary/image data is not persisted or stored in the repository.
- If the webhook payload is partial, the Linear adapter hydrates the full issue context before
  parsing the spec. If the repo scope is still missing or unregistered, the task is blocked and the
  bot asks for a registered repo in Linear; a follow-up comment naming one registered repo resumes
  the task.
- Linked Notion pages and Google Docs in the Linear description, comments, or text attachments are
  fetched as additional spec text when the matching document adapter is enabled.
- Figma links in the Linear description, comments, or attachments are fetched as additional design
  spec text when the Figma adapter is enabled.
- Linear image attachments are fetched and summarized as additional design spec text when image
  hydration is enabled.
- When `ASDLC_LINEAR_SPEC_PLANNER_ENABLED=true`, hydrated specs are planned through the configured
  model provider. The planner can create multiple repo-scoped DAG nodes, including multiple nodes
  for one repo, and invalid model plans fall back to the deterministic one-node-per-repo DAG.
- When `ASDLC_LINEAR_PLAN_APPROVAL_REQUIRED=true`, the platform persists the planned DAG and waits
  for `/approve-plan <LINEAR-ID>` in the Linear thread before queueing runnable DAG nodes. Queued
  node payloads include an RTK-only terminal policy, GraphStore-first repo-context policy, and
  GitHub write-disabled marker until write-scoped GitHub App access is approved.

Conversation sync can poll Multica-backed sessions and mirror new agent comments back to the
originating channel. The real Docker overlay enables it by default:

```bash
ASDLC_CONVERSATION_SYNC_ENABLED=true
ASDLC_CONVERSATION_SYNC_INTERVAL_SECONDS=15
ASDLC_CONVERSATION_SYNC_BATCH_SIZE=50
```

Linear replies use the configured Linear adapter. Slack replies use `ASDLC_SLACK_BOT_TOKEN` and the
stored Slack thread ID. Telegram replies use `ASDLC_TELEGRAM_BOT_TOKEN` and the stored chat ID.

## Delivery Plan

See `docs/IMPLEMENTATION_BACKLOG.md`.

## Development Discipline

Implementation is test-first. Unit tests and Schemathesis contract tests are part of the default
quality gate. See `docs/TDD_WORKFLOW.md`.

Agent/runtime instructions are in `AGENTS.md`, `CLAUDE.md`, and `.agent/`.
