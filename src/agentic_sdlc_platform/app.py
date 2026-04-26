from fastapi import FastAPI

from agentic_sdlc_platform.api.health import router as health_router
from agentic_sdlc_platform.api.webhooks import router as webhook_router
from agentic_sdlc_platform.core.config import Settings, get_settings


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved_settings = settings or get_settings()
    app = FastAPI(
        title=resolved_settings.service_name,
        version=resolved_settings.version,
        docs_url="/docs" if resolved_settings.docs_enabled else None,
        redoc_url="/redoc" if resolved_settings.docs_enabled else None,
    )

    app.state.settings = resolved_settings
    app.include_router(health_router)
    app.include_router(webhook_router, prefix="/webhooks")
    return app
