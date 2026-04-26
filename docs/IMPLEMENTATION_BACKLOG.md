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

## Phase 1: Webhook Bridge

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
- Add Telegram/Discord adapter contracts
- Route ad-hoc Q&A to Hermes direct sessions
- Route implementation requests to Multica lifecycle tasks
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
