# Agentic SDLC Platform Agent Guide

This repo is built by agents and for agents. Follow this guide before editing code.

## Required Workflow

1. Read `CLAUDE.md`, `docs/TDD_WORKFLOW.md`, and `docs/IMPLEMENTATION_BACKLOG.md`.
2. Classify the task into a backlog phase.
3. Write or update tests first.
4. Update Schemathesis contract tests before changing public API behavior.
5. Implement the smallest code path that satisfies the tests.
6. Run `make quality`.
7. Commit as `GunnerBot <GunnerBot@users.noreply.github.com>`.

## Agent Usage

Use specialist agents when work spans independent concerns:

- `planner`: phase decomposition, sequencing, risk review.
- `architect`: ports/adapters, integration boundaries, vendor lock-in review.
- `tdd-guide`: scenario and test coverage before implementation.
- `security-reviewer`: webhook validation, tokens, GitHub actions, deploy hooks.
- `build-error-resolver`: dependency, CI, and quality-gate failures.

Keep write ownership disjoint when parallelizing implementation.

## Skills

Repo-native skills live in `.agent/skills/` and protocols live in `.agent/protocols/`.
Use them as operating instructions for Hermes, Claude, Codex, or any compatible runtime.
