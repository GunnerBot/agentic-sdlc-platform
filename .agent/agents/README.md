# Agent Roles

Recommended roles for this repo:

- `planner`: decomposes specs into small, dependency-aware phases.
- `architect`: reviews ports/adapters, security boundaries, and vendor coupling.
- `tdd-guide`: writes scenarios and tests before implementation.
- `security-reviewer`: reviews webhooks, secrets, GitHub permissions, deploy actions, and audit trails.
- `build-error-resolver`: handles failing quality gates.

Agents should report changed files and quality commands they ran.
