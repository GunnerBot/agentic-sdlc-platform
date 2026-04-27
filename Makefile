.PHONY: sync lint test contract quality run migrate compose-real-up compose-real-down compose-real-logs

sync:
	uv sync

lint:
	uv run ruff check .

test:
	uv run pytest tests --ignore=tests/contracts

contract:
	uv run pytest tests/contracts

quality: lint test contract

run:
	uv run agentic-sdlc-platform

migrate:
	uv run alembic upgrade head

compose-real-up:
	docker compose --env-file .env.local -f docker-compose.yml -f docker-compose.real.yml up -d --build

compose-real-down:
	docker compose --env-file .env.local -f docker-compose.yml -f docker-compose.real.yml down

compose-real-logs:
	docker compose --env-file .env.local -f docker-compose.yml -f docker-compose.real.yml logs -f agentic-sdlc-platform
