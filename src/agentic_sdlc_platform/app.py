from fastapi import FastAPI

from agentic_sdlc_platform.api.channels import router as channel_router
from agentic_sdlc_platform.api.health import router as health_router
from agentic_sdlc_platform.api.slack import router as slack_router
from agentic_sdlc_platform.api.telegram import router as telegram_router
from agentic_sdlc_platform.api.webhooks import router as webhook_router
from agentic_sdlc_platform.core.config import Settings, get_settings
from agentic_sdlc_platform.core.dependencies import (
    build_graph_store,
    build_hermes_session,
    build_model_provider,
    build_repository,
    build_task_orchestrator,
)
from agentic_sdlc_platform.persistence.repository import PersistenceRepository
from agentic_sdlc_platform.ports.hermes_session import HermesSessionPort
from agentic_sdlc_platform.ports.task_orchestrator import TaskOrchestratorPort


def create_app(
    settings: Settings | None = None,
    repository: PersistenceRepository | None = None,
    task_orchestrator: TaskOrchestratorPort | None = None,
    hermes_session: HermesSessionPort | None = None,
) -> FastAPI:
    resolved_settings = settings or get_settings()
    app = FastAPI(
        title=resolved_settings.service_name,
        version=resolved_settings.version,
        docs_url="/docs" if resolved_settings.docs_enabled else None,
        redoc_url="/redoc" if resolved_settings.docs_enabled else None,
    )

    app.state.settings = resolved_settings
    app.state.model_provider = build_model_provider(resolved_settings)
    app.state.graph_store = build_graph_store(resolved_settings)
    app.state.repository = repository or build_repository(resolved_settings)
    app.state.task_orchestrator = task_orchestrator or build_task_orchestrator(resolved_settings)
    app.state.hermes_session = hermes_session or build_hermes_session(resolved_settings)
    app.include_router(health_router)
    app.include_router(channel_router, prefix="/channels")
    app.include_router(slack_router, prefix="/channels/slack")
    app.include_router(telegram_router, prefix="/channels/telegram")
    app.include_router(webhook_router, prefix="/webhooks")
    return app
