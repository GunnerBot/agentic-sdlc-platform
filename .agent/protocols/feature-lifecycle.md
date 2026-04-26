# Feature Lifecycle

Each feature should move through these artifacts:

1. `0.idea.md`: trigger, source ticket, rough problem.
2. `1.prd.md`: user-visible behavior and constraints.
3. `2.architecture.md`: ports, adapters, persistence, security boundaries.
4. `3.tasks.md`: implementation slices with ownership.
5. `4.tests.md`: unit, integration, and contract scenarios.
6. `5.implementation.md`: code changes and migration notes.
7. `6.review.md`: findings, risk, and rollout notes.

Store active artifacts in `.agent/work/doing/<ticket-or-slug>/` and completed artifacts in
`.agent/work/done/<ticket-or-slug>/`. These work folders are intentionally ignored.
