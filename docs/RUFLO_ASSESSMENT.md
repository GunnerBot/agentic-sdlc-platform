# Ruflo Assessment

Reviewed on 2026-04-27: https://github.com/ruvnet/ruflo

## Decision

Do not integrate Ruflo as a runtime dependency right now.

Ruflo is an agent orchestration toolkit centered on Claude Code/Codex CLI hooks,
MCP tools, swarm coordination, memory, and workflow templates. This service is a
server-side SDLC control plane with durable webhooks, channel ingress, repo
registry, session storage, Graphify indexing, Hermes chat sessions, Multica task
orchestration, and contract-tested FastAPI APIs. Pulling Ruflo in directly would
overlap with orchestration responsibilities we already own and would add a large
Node/CLI/MCP surface to a Python service.

## Useful Ideas To Adopt

- Workflow templates: feature, bugfix, security, refactor pipelines can map to
  DAG templates in this service.
- Anti-drift checkpoints: keep a coordinator-owned task state, checkpoint after
  each phase, and require verification before moving to PR.
- Memory-before-work: search repo/session memory before planning or executing.
- Role routing: use explicit roles such as planner, explorer, coder, tester,
  reviewer, and security reviewer.
- Cost routing: simple deterministic operations should avoid model calls.

## Already Covered Here

- Durable webhook ingestion and idempotency.
- Linear, Slack, and Telegram ingress.
- Human override commands.
- Repo registry and multi-repo indexing hooks.
- Graphify seam for repo questions.
- Hermes session persistence and resume path.
- Multica task orchestration seam.
- TDD plus Schemathesis contract tests.

## Backlog Items Derived From Ruflo

1. Add DAG templates for bugfix, feature, refactor, and security work.
2. Add task checkpoints with required verification gates before PR creation.
3. Add a memory retrieval step before task planning.
4. Store per-agent role outputs in session events for auditability.
5. Add a lightweight policy router for deterministic versus model-backed work.
