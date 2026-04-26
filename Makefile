.PHONY: sync lint test contract quality run migrate

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
