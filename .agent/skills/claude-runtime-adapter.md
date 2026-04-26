# Skill: Claude Runtime Adapter

Use when adding or changing Claude integration.

Rules:

- Keep Claude-specific code in `src/agentic_sdlc_platform/adapters/`.
- Keep provider-neutral request/response types in `ports/`.
- Require explicit configuration before real network calls.
- Add timeout, retry, and audit behavior before enabling production use.
- Never expose raw API keys to logs, exceptions, or API responses.
- Tests must cover disabled, misconfigured, and configured paths.
