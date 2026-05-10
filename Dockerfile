FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_HTTP_TIMEOUT=120

WORKDIR /app

COPY --from=ghcr.io/astral-sh/uv:0.5.31 /uv /uvx /bin/

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates git \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml uv.lock README.md ./
COPY src ./src
COPY alembic.ini ./
COPY migrations ./migrations

RUN uv sync --no-dev --frozen

EXPOSE 8080

CMD ["sh", "-c", "uv run --no-dev alembic upgrade head && uv run --no-dev agentic-sdlc-platform"]
