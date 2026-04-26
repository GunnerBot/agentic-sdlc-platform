from fastapi import FastAPI

from agentic_sdlc_platform.api.health import router as health_router
from agentic_sdlc_platform.api.webhooks import router as webhook_router
from agentic_sdlc_platform.core.config import Settings, get_settings
from agentic_sdlc_platform.core.dependencies import (
    build_graph_store,
    build_model_provider,
    build_repository,
    build_task_orchestrator,
)
from agentic_sdlc_platform.persistence.repository import PersistenceRepository
from agentic_sdlc_platform.ports.task_orchestrator import TaskOrchestratorPort


def create_app(
    settings: Settings | None = None,
    repository: PersistenceRepository | None = None,
    task_orchestrator: TaskOrchestratorPort | None = None,
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
    app.include_router(health_router)
    app.include_router(webhook_router, prefix="/webhooks")
    return app
