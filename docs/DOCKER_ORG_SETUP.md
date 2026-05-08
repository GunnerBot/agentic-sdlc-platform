# Docker Compose Organization Setup

This guide is the fastest path for a company to self-host Agentic SDLC Platform for one
organization or workspace. It assumes Docker Compose first; the same environment contract can later
be moved to EC2, ECS, Kubernetes, or another production runtime.

## What The Company Gets

- Linear-driven planning and implementation workflows.
- Slack and Telegram task/status operations.
- GitHub App based repository access with read and write permissions from day one.
- Multica/Hermes runtime integration for agent execution.
- Graphify-backed repository context retrieval.
- LLM usage and estimated cost observability.
- Local Postgres persistence through Docker Compose.

## Required Accounts And Credentials

Create these before starting the platform:

- GitHub App installed into the company organization.
- OpenAI service account API key.
- Linear workspace webhook secret.
- Optional Slack app credentials.
- Optional Telegram bot token.
- Optional Multica workspace/API key for real runtime execution.
- Optional Hermes gateway API key for the official Hermes gateway.
- Optional Notion, Google Docs, Figma, and image-summary credentials for richer spec hydration.

Use a service account or bot-owned credential wherever possible. Do not use a personal developer
token for long-running company automation.

## GitHub App Setup

Use one GitHub App for both reads and writes. The platform enforces write guardrails internally,
so there is no separate read-only app or PAT flow.

Minimum repository permissions:

- Metadata: read.
- Contents: read and write.
- Pull requests: read and write.
- Issues: read and write.
- Checks: read.

Recommended app settings:

- Webhook URL: `https://<your-public-platform-host>/webhooks/github`
- Webhook secret: generate a strong random value and store it only in `.env.local`.
- Repository access: let the installing user choose selected repositories or all repositories.
- Private key: download the GitHub App private key and store it under `secrets/github-app.pem`.

GitHub controls repository selection during installation. The platform imports only repositories
granted to the GitHub App installation.

## Clone And Configure

```bash
git clone https://github.com/GunnerBot/agentic-sdlc-platform.git
cd agentic-sdlc-platform
cp .env.example .env.local
mkdir -p secrets
```

Put the GitHub App private key here:

```text
secrets/github-app.pem
```

`secrets/`, `.env.local`, `*.pem`, and generated Graphify data are gitignored. Never commit real
keys, tokens, installation IDs, repo-local Graphify indexes, or generated runtime artifacts.

## Minimal `.env.local`

Start with this shape and replace placeholders:

```bash
ASDLC_ENVIRONMENT=local
ASDLC_ALLOW_UNSIGNED_WEBHOOKS=false

ASDLC_GITHUB_APP_READ_ONLY_ENABLED=true
ASDLC_GITHUB_APP_SLUG=<github_app_slug>
ASDLC_GITHUB_APP_WRITE_ENABLED_DEFAULT=true
ASDLC_GITHUB_APP_ID=<github_app_id>
ASDLC_GITHUB_APP_INSTALLATION_ID=<initial_installation_id_if_known>
ASDLC_GITHUB_APP_PRIVATE_KEY_PATH=secrets/github-app.pem
ASDLC_GITHUB_WEBHOOK_SECRET=<github_webhook_secret>

ASDLC_VENDOR_HTTP_ENABLED=true
ASDLC_MODEL_PROVIDER=openai
ASDLC_OPENAI_API_KEY=<openai_service_account_key>
ASDLC_OPENAI_ROUTER_MODEL=gpt-5-nano
ASDLC_OPENAI_SUMMARY_MODEL=gpt-5-nano
ASDLC_OPENAI_PLANNER_MODEL=gpt-5-mini
ASDLC_OPENAI_WRITE_MODEL=gpt-5-mini
ASDLC_OPENAI_PLANNER_ESCALATION_MODEL=gpt-5
ASDLC_OPENAI_WRITE_ESCALATION_MODEL=gpt-5
ASDLC_OPENAI_PREMIUM_ESCALATION_MODEL=gpt-5.5

ASDLC_LINEAR_SIGNING_SECRET=<linear_webhook_secret>
ASDLC_LINEAR_SPEC_PLANNER_ENABLED=true
ASDLC_LINEAR_PLAN_APPROVAL_REQUIRED=true

ASDLC_API_AUTH_ENABLED=true
ASDLC_API_AUTH_KEYS=<comma_separated_operator_api_keys>
ASDLC_API_RATE_LIMIT_ENABLED=true
ASDLC_API_RATE_LIMIT_REQUESTS_PER_MINUTE=120
```

For local smoke testing, the default Compose file includes a compatible dev Multica/Hermes service.
For real Multica, add:

```bash
ASDLC_MULTICA_HTTP_ENABLED=true
ASDLC_MULTICA_BASE_URL=http://127.0.0.1:18080
ASDLC_MULTICA_API_KEY=<multica_api_key>
ASDLC_MULTICA_WORKSPACE_ID=<multica_workspace_id>
ASDLC_MULTICA_DEFAULT_RUNTIME_PROVIDER=codex
```

For the official Hermes gateway, add:

```bash
ASDLC_HERMES_HTTP_ENABLED=true
ASDLC_HERMES_API_MODE=openai_compatible
ASDLC_HERMES_API_KEY=<local_hermes_gateway_token>
ASDLC_HERMES_MODEL=hermes-agent
ASDLC_HERMES_INFERENCE_PROVIDER=custom
ASDLC_HERMES_INFERENCE_MODEL=gpt-5-mini
ASDLC_HERMES_TIMEOUT_SECONDS=120
```

## Start Docker Compose

For the fastest local company trial with compatible dev runtime services:

```bash
docker compose --env-file .env.local up -d --build
```

For real host-local Multica services:

```bash
make compose-real-up
```

For the official self-hosted Hermes gateway overlay:

```bash
make compose-real-hermes-up
```

Run database migrations:

```bash
docker compose --env-file .env.local exec agentic-sdlc-platform uv run alembic upgrade head
```

Health checks:

```bash
curl http://localhost:8080/healthz
curl http://localhost:8080/readyz
curl http://localhost:8080/ops/status
```

`/ops/status` reports missing optional credentials and production guardrails.

## Install And Sync GitHub Repositories

Get the GitHub App install URL for a workspace:

```bash
curl "http://localhost:8080/repos/github-app/install-url?workspace_id=company-acme"
```

Install the app in GitHub and choose the repositories. After GitHub gives an installation ID, sync:

```bash
curl -X POST http://localhost:8080/repos/github-app/sync \
  -H "Content-Type: application/json" \
  -d '{"workspace_id":"company-acme","installation_id":"123456"}'
```

The platform stores the workspace installation and imports the granted repositories with:

- read enabled.
- write enabled.
- branch prefix restricted to `agent/dag/`.
- no direct default-branch push.
- plan approval required before queued implementation work.
- no auto-merge by default.

Index only the repositories you want to onboard for context:

```bash
curl -X POST http://localhost:8080/repos/index \
  -H "Content-Type: application/json" \
  -d '{"repos":["atlas-tech-inc/keychain-os-erp","atlas-tech-inc/webapp-monorepo"]}'
```

`POST /repos/index-all` remains available for deliberate bulk indexing, but it is not the default
onboarding path for large installations.

## Repo Context And Graphify

Selected indexing clones repositories into the Docker-managed `/repo-cache` volume using the
GitHub App installation token. Graphify then indexes the cached checkout and writes generated graph
data to `/graphify-data`. No host repository mount is required.

Local checkout paths are still supported as a developer override when you explicitly register repo
metadata such as:

```json
{
  "local_path": "/repos/erp-service"
}
```

Those paths are runtime metadata only. Cached checkouts and Graphify indexes are generated runtime
data and should never be committed.

## Connect Linear

Create a Linear webhook that points to:

```text
https://<your-public-platform-host>/webhooks/linear
```

Use the same webhook secret in `ASDLC_LINEAR_SIGNING_SECRET`.

Typical workflow:

1. A user creates or assigns a Linear issue.
2. The issue contains a spec, markdown, repo names, links, attachments, or design references.
3. The platform hydrates available context.
4. The planner creates a DAG of ordered implementation nodes.
5. If approval is required, the bot waits for `/approve-plan <LINEAR-ID>` in the Linear thread.
6. Runnable nodes are sent to Multica/Hermes.
7. PRs are created on `agent/dag/<dag_id>/<node_key>` branches.
8. Status and agent comments sync back to Linear.

## Connect Slack And Telegram

Slack and Telegram are optional but useful for intake and status.

Slack supports thread-aware ticket creation through:

```text
/create-ticket
```

When used inside a Slack thread, the platform uses the thread context to create the Linear ticket.
Follow-up task/status commands are routed back to the same persisted session where possible.

Telegram follows the same task/session model after the bot token and allowed chat mapping are
configured.

## Production Hardening Before Real Company Rollout

Before exposing this to a whole company, enable:

- API auth with `ASDLC_API_AUTH_ENABLED=true`.
- API rate limiting with `ASDLC_API_RATE_LIMIT_ENABLED=true`.
- Signed GitHub and Linear webhooks.
- HTTPS public ingress.
- Postgres backups.
- Secret storage outside the repository.
- Log and metrics collection.
- Cost budgets and model retry limits.
- GitHub write policy review for branch prefixes and PR behavior.

## What Moves To EC2 Later

The EC2 version should preserve the same contract:

- `.env.local` becomes EC2 instance/user data, SSM Parameter Store, Secrets Manager, or ECS secrets.
- Docker Compose can run on a single EC2 instance for the first deployment.
- Public HTTPS should terminate through ALB, nginx, Caddy, or another reverse proxy.
- Postgres should move to RDS for production.
- Graphify data should use an attached volume or durable object storage strategy.
- Webhook URLs should point at the public HTTPS host.

The Docker Compose setup is the fastest route to validate the organization flow. EC2 should be a
deployment packaging change, not a different product architecture.
