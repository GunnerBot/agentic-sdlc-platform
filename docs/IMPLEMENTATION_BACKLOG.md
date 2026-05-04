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
- Real Graphify graph store adapter through official CLI mode, with optional compatible HTTP mode
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
- Expose task and agent session status APIs for triggered work and channel conversations
- Add repository registry APIs for multi-repo ownership, defaults, and provider metadata
- Add repo indexing job APIs backed by the Graphify graph store seam
- Add repo Q&A API backed by Graphify queries for codebase questions
- Add bulk repo indexing API for all active registered repositories
- Route repo-scoped Slack, Telegram, and generic channel questions to Graphify
- Persist channel/Linear conversation sessions and route followups back to the same Hermes or Multica task
- Sync Multica issue comments back into persisted task sessions and originating Linear threads
- Enforce deterministic repo context policy: prefer Graphify or code-review graph retrieval before broad source scans, then verify against narrow source reads

## Phase 4: DAG Decomposer

- Define subtask DAG schema
- Call planner model through the model router
- Persist DAG nodes and dependencies
- Support dependency unblocking after PR merge
- Add GitHub App read-only installation integration for repo discovery/import
- Keep GitHub write enablement explicitly pending until a narrowly scoped GitHub App write policy is approved:
  branch create, commit push, PR create/comment, and checks read on allowlisted repositories only
- Enforce runtime execution policy in Multica/Hermes payloads: all terminal commands must be prefixed with `rtk`
- Include repo context payloads from Graphify or a compatible code-review graph in every repo-backed node execution

## Phase 4.5: PRD/TRD Readiness And Test-First Enforcement

- Hydrate Linear descriptions, comments, and attachments for PRD, TRD, design, and acceptance
  links before planning. Supported sources should include inline markdown, Notion, Google Docs,
  Figma, image attachments, and plain URLs.
- Persist PRD/TRD/design artifacts separately from implementation snapshots so every DAG node can
  reference the exact product and technical context it was planned against.
- Add a readiness gate before write execution:
  repo scope resolved, PRD/spec complete enough, TRD or technical plan generated, TDD/test plan
  generated, DAG/PR split generated, and human approval recorded.
- Enforce `ASDLC_LINEAR_PLAN_APPROVAL_REQUIRED=true` for write-capable execution modes so Multica or
  Hermes cannot start code-writing work until `/approve-plan <ISSUE-ID>` is recorded.
- Add an execution policy field such as `readiness_state` to every Multica/Hermes payload. Write
  execution must require `readiness_state=approved_for_implementation`.
- Add a test-first guard for functional code changes:
  the first write pass for each DAG node must create or update tests before production code edits.
- Add a changed-file-to-test coverage gate:
  every changed production file must map to at least one changed or pre-existing relevant unit,
  integration, or regression test in the same PR.
- Add an API contract gate:
  every new or modified FastAPI endpoint, OpenAPI schema, webhook payload contract, or response
  model must include Schemathesis/OpenAPI contract coverage in the same PR.
- Add PR completion gates:
  do not mark a DAG node completed, fixed, or ready to merge until unit tests, focused integration
  tests, contract tests, and configured smoke checks pass.
- Persist test evidence on the DAG node and PR metadata:
  test files changed, production files changed, contract suites run, command outputs, CI URLs,
  and any explicit human override.
- Add local and CI hooks that can reject unsafe agent output:
  missing plan approval, missing tests for changed production files, endpoint changes without
  contract tests, failing tests, direct default-branch writes, or PR body missing
  `dag/<dag_id>/<node_key>`.
- Keep tests and implementation in the same PR for each DAG node. Do not split required unit tests
  or contract tests into follow-up PRs.

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
