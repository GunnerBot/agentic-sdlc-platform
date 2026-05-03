# Self-Hosted Bootstrap Runbook

This runbook captures the repeatable setup sequence for a private organization install. It is
organization-neutral: real workspace names, repository names, installation IDs, API keys, and
runtime artifacts belong in ignored local config, secret stores, Docker volumes, or the platform
database.

## 1. Prepare The Public Platform Repo

Use the public repository only for generic platform code:

- FastAPI service code.
- Provider ports and adapters.
- Migrations.
- Docker and Compose files.
- Example config templates.
- Tests with fictional organizations and repositories.
- Generic setup documentation.

Do not commit:

- Real organization names.
- Real repository names.
- GitHub App installation IDs.
- API keys.
- Private keys.
- Local checkout paths.
- Graphify indexes.
- Generated task artifacts.

## 2. Stop Any Existing Local Runtime

Stop existing containers before a clean bootstrap:

```bash
docker compose --env-file .env.local \
  -f docker-compose.yml \
  -f docker-compose.real.yml \
  -f docker-compose.hermes.yml \
  down --remove-orphans
```

This removes containers and the Compose network. It intentionally preserves Docker volumes unless
you explicitly choose to reset persisted database/index state.

## 3. Configure Ignored Runtime Secrets

Copy the example file and edit only ignored local config:

```bash
cp .env.example .env.local
mkdir -p secrets
```

Required for a real organization install:

```bash
ASDLC_GITHUB_APP_SLUG=<github_app_slug>
ASDLC_GITHUB_APP_ID=<github_app_id>
ASDLC_GITHUB_APP_INSTALLATION_ID=<optional_default_installation_id>
ASDLC_GITHUB_APP_PRIVATE_KEY_PATH=secrets/github-app.pem
ASDLC_GITHUB_WEBHOOK_SECRET=<github_webhook_secret>

ASDLC_OPENAI_API_KEY=<service_account_api_key>

ASDLC_LINEAR_SIGNING_SECRET=<linear_webhook_secret>
```

Optional real runtime services:

```bash
ASDLC_MULTICA_HTTP_ENABLED=true
ASDLC_MULTICA_BASE_URL=<multica_url>
ASDLC_MULTICA_API_KEY=<multica_api_key>
ASDLC_MULTICA_WORKSPACE_ID=<multica_workspace_id>

ASDLC_HERMES_HTTP_ENABLED=true
ASDLC_HERMES_API_KEY=<hermes_gateway_key>
```

## 4. Start Docker

For the fastest local smoke setup with compatible dev runtime services:

```bash
docker compose --env-file .env.local up -d --build
```

For real host-local Multica plus platform container:

```bash
make compose-real-up
```

For the official self-hosted Hermes gateway overlay:

```bash
make compose-real-hermes-up
```

Then run migrations:

```bash
docker compose --env-file .env.local exec agentic-sdlc-platform \
  uv run alembic upgrade head
```

## 5. Verify Runtime Health

```bash
curl http://localhost:8080/erp/health
curl http://localhost:8080/ops/status
```

`/ops/status` should be treated as the operator checklist. It reports missing optional credentials,
auth/rate-limit state, and runtime readiness.

## 6. Connect The GitHub App

Get an install URL for the workspace:

```bash
curl "http://localhost:8080/repos/github-app/install-url?workspace_id=<workspace_id>"
```

Install the app in GitHub and choose selected repositories or all repositories. Then sync the
installation:

```bash
curl -X POST http://localhost:8080/repos/github-app/sync \
  -H "Content-Type: application/json" \
  -d '{"workspace_id":"<workspace_id>","installation_id":"<installation_id>"}'
```

This stores only the repositories granted by GitHub for that installation.

## 7. Register Local Checkout Aliases When Needed

For local Docker trials, repository aliases and local paths are runtime data. Register them through
the API or a private seed process, not committed source:

```bash
curl -X POST http://localhost:8080/repos \
  -H "Content-Type: application/json" \
  -d '{
    "name": "erp-service",
    "provider": "github",
    "clone_url": "https://github.com/acme-corp/erp-service.git",
    "default_branch": "main",
    "metadata": {
      "local_path": "/repos/erp-service",
      "repo_path": "/repos/erp-service"
    }
  }'
```

## 8. Index Repositories

```bash
curl -X POST http://localhost:8080/repos/erp-service/index
```

or:

```bash
curl -X POST http://localhost:8080/repos/index-all
```

Generated Graphify data belongs in Docker-managed volumes or ignored local paths.

## 9. Connect Linear And Channels

Configure provider webhooks to the public platform URL:

```text
https://<platform-host>/webhooks/linear
https://<platform-host>/webhooks/github
```

Slack and Telegram are optional channel adapters. Their app tokens, signing secrets, channel maps,
and allow-lists are local runtime config.

## 10. Run A Smoke Flow

Minimal checks:

```bash
curl http://localhost:8080/repos
curl http://localhost:8080/tasks
```

For a full Linear flow:

1. Create or assign a Linear issue to the bot.
2. Include a registered repository name in the spec.
3. Confirm the platform creates an internal task and hydrated spec artifact.
4. Confirm plan approval is requested when enabled.
5. Reply `/approve-plan <ISSUE-ID>`.
6. Confirm ready DAG nodes are queued to the orchestrator.
7. Confirm task/session status is visible from Linear or channel status commands.

## 11. Commit Policy

Commit only generic platform changes from the public project identity. Keep organization-specific
runtime setup private and outside the repository.
