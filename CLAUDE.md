# CLAUDE.md - Agentic SDLC Platform

FastAPI + `uv` service that composes external agent infrastructure into an internal SDLC control
plane.

## Architecture

- `api/`: HTTP ingress. Keep controllers thin.
- `core/`: configuration and shared infrastructure.
- `glue/`: platform orchestration primitives.
- `ports/`: runtime-agnostic interfaces.
- `adapters/`: vendor/tool adapters such as Claude and Graphify.
- `models/`: API response and request models.

## Design Rules

- Prefer ports/adapters over direct vendor coupling.
- Do not log tokens, webhook secrets, prompts with credentials, or raw request bodies.
- Webhook behavior must be idempotent before it mutates external systems.
- Auto-merge remains restricted to `agent-staging` until explicitly lifted.
- Contract tests must describe every public endpoint.
- Every phase starts with tests.

## Commands

```bash
uv sync
make lint
make test
make contract
make quality
make run
```

## Integration Posture

Claude and Graphify adapters are currently deterministic seams. Do not wire real transports until
the concrete API, auth model, timeout policy, and audit records are specified and tested.
