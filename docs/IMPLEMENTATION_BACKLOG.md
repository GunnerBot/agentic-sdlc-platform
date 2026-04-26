# Implementation Backlog

This backlog follows the Agentic SDLC Platform design spec and keeps delivery in small vertical slices.

## Phase 0: Local Scaffold

- FastAPI service scaffold with `uv`
- Health and readiness endpoints
- Linear and GitHub webhook receivers
- Signature validation primitives
- Glue module placeholders for channel routing, DAG decomposition, auto-merge, cost routing, and deploy hooks
- Unit tests, Dockerfile, Compose baseline

## Phase 0.5: TDD And Contract Gates

- TDD workflow documentation
- Makefile quality gates
- CI quality workflow
- Schemathesis OpenAPI contract tests
- Contract test coverage for existing health and webhook endpoints
- Repo-native `AGENTS.md`, `CLAUDE.md`, `.agent/` protocols, skills, and agent role guidance

## Phase 0.6: Claude And Graphify Seams

- Provider-neutral model provider port
- Provider-neutral graph store port
- Claude model provider adapter seam
- Graphify graph store adapter seam
- Disabled and configured-path tests for both adapters

## Phase 1: Webhook Bridge

- Async SQLAlchemy persistence
- Alembic migrations
- Local Postgres service in Docker Compose
- `InboundEvent`, `Task`, and `AuditEvent` control-plane tables
- Validate Linear webhook signatures
- Validate GitHub webhook signatures
- Normalize incoming events into an internal task event model
- Add idempotency keys for retried webhooks
- Persist inbound event audit records

## Phase 2: Multica Adapter

- Add Multica client
- Create task API integration
- Map Linear issue events to Multica tasks
- Map GitHub PR events to Multica task updates
- Add retries, timeouts, and structured error handling

## Phase 3: Channel Router

- Add Slack ingress adapter
- Add Telegram ingress adapter
- Route ad-hoc Q&A to Hermes direct sessions
- Route implementation requests to Multica lifecycle tasks
- Add channel auth and repo mapping for Slack channels and Telegram chats
- Add human override commands: `/pause`, `/resume`, `/takeover`, `/context`, `/reject`
- Enforce per-channel cost caps

## Phase 4: DAG Decomposer

- Define subtask DAG schema
- Call planner model through the model router
- Persist DAG nodes and dependencies
- Support dependency unblocking after PR merge

## Phase 5: Model And Cost Router

- Load model routing YAML
- Enforce forbidden models and cross-family critic rules
- Track cost by task, repo, role, model, and channel
- Add per-ticket and per-channel budget caps

## Phase 6: Auto-Merge And Deploy Hooks

- Consume GitHub check-suite and PR review events
- Require CI green, critic approval, and human approval
- Restrict auto-merge to `agent-staging` until production hardening
- Trigger deploy hooks and integration test polling

## Phase 7: Hardening

- Postgres persistence
- Audit logs and replay
- Credential vault integration
- Deterministic guard hooks for protected branches and deploy commands
- Observability dashboards

## Phase 8: Deferred Channels

- Add Discord ingress adapter after Slack and Telegram are production-ready
- Validate Discord signatures
- Route Discord messages through the same channel auth, repo mapping, and Hermes/Multica paths
