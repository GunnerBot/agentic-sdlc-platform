import uvicorn

from agentic_sdlc_platform.core.config import get_settings


def main() -> None:
    settings = get_settings()
    uvicorn.run(
        "agentic_sdlc_platform.app:create_app",
        factory=True,
        host=settings.host,
        port=settings.port,
        reload=settings.reload,
    )


if __name__ == "__main__":
    main()
