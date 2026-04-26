from fastapi.testclient import TestClient

from agentic_sdlc_platform.app import create_app
from agentic_sdlc_platform.core.config import Settings


def test_healthz_returns_ok() -> None:
    client = TestClient(create_app(Settings()))

    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_readyz_returns_ready() -> None:
    client = TestClient(create_app(Settings()))

    response = client.get("/readyz")

    assert response.status_code == 200
    assert response.json() == {"status": "ready"}
