# TDD Workflow

Every implementation phase starts with tests and contracts before production code.

## Phase Gate

1. Define the behavior in `docs/IMPLEMENTATION_BACKLOG.md`.
2. Add or update unit tests for the behavior.
3. Add or update Schemathesis contract coverage when API shape changes.
4. Run the focused tests and confirm the new behavior fails when implementation is absent.
5. Implement the smallest code path that makes the tests pass.
6. Run full quality gates before commit.

## Commands

```bash
make test
make contract
make lint
make quality
```

## Test Layout

- `tests/test_*.py`: unit and service-level tests.
- `tests/contracts/`: OpenAPI contract tests powered by Schemathesis.

## Contract Testing Rules

- Every public endpoint must be represented in the generated OpenAPI schema.
- Endpoint behavior and OpenAPI response declarations must stay aligned.
- Required headers and request fields should be required in the schema, not only in code.
- Contract tests run in-process against the ASGI app to avoid network flakiness.
