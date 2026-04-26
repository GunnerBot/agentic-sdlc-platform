# TDD + Contract First Protocol

Use this protocol for every implementation phase.

1. State the behavior and affected endpoints.
2. Write unit tests for service behavior.
3. Write or update Schemathesis/OpenAPI contract coverage for public API behavior.
4. Confirm the new tests fail for missing behavior when practical.
5. Implement behind ports/adapters.
6. Run `make quality`.
7. Record any discovered contract drift in the commit summary or PR body.

Never weaken a contract test to hide a mismatch. Either fix the schema or fix the endpoint.
