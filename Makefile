.PHONY: sync format format-check lint lint-fix typecheck security test contract agent-gates precommit prepush install-hooks quality run migrate github-app-git-credential-configure compose-dev-up compose-dev-down compose-dev-logs compose-real-up compose-real-hermes-up compose-real-down compose-real-logs

sync:
	uv sync

format:
	uv run ruff format .

format-check:
	uv run ruff format --check .

lint:
	uv run ruff check .

lint-fix:
	uv run ruff check --fix .

typecheck:
	uv run mypy

security:
	uv run bandit -c pyproject.toml -r src scripts -q

test:
	uv run pytest tests --ignore=tests/contracts

contract:
	uv run pytest tests/contracts

agent-gates:
	uv run python scripts/enforce_agent_quality_gates.py

precommit: format-check lint typecheck security agent-gates

prepush: precommit test contract

install-hooks:
	git config core.hooksPath .git-hooks
	chmod +x .git-hooks/pre-commit .git-hooks/pre-push

quality: prepush

run:
	uv run agentic-sdlc-platform

migrate:
	uv run alembic upgrade head

github-app-git-credential-configure:
	git config --global --unset-all credential.https://github.com.helper || true
	git config --global --add credential.https://github.com.helper "!uv --directory $(CURDIR) run agentic-sdlc-github-app-credential"
	git config --global --add credential.https://github.com.helper "!/opt/homebrew/bin/gh auth git-credential"
	git config --global credential.https://github.com.useHttpPath true

compose-dev-up:
	docker compose --env-file .env.local --profile dev up -d --build

compose-dev-down:
	docker compose --env-file .env.local --profile dev down

compose-dev-logs:
	docker compose --env-file .env.local --profile dev logs -f agentic-sdlc-platform dev-agent-services

compose-real-up:
	docker compose --env-file .env.local -f docker-compose.yml -f docker-compose.real.yml up -d --build

compose-real-hermes-up:
	docker compose --env-file .env.local -f docker-compose.yml -f docker-compose.real.yml -f docker-compose.hermes.yml up -d --build

compose-real-down:
	docker compose --env-file .env.local -f docker-compose.yml -f docker-compose.real.yml down

compose-real-logs:
	docker compose --env-file .env.local -f docker-compose.yml -f docker-compose.real.yml logs -f agentic-sdlc-platform
