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


def test_ops_status_reports_missing_enabled_integrations() -> None:
    client = TestClient(
        create_app(
            Settings(
                notion_http_enabled=True,
                google_docs_http_enabled=True,
                figma_http_enabled=True,
                environment="production",
            )
        )
    )

    response = client.get("/ops/status")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "degraded"
    assert body["integrations"]["notion"] == {
        "enabled": True,
        "configured": False,
        "ready": False,
        "missing": ["api_key"],
    }
    assert body["integrations"]["google_docs"]["missing"] == ["bearer_token"]
    assert body["integrations"]["figma"]["missing"] == ["api_key"]
    assert body["webhook_security"]["unsigned_allowed"] is False
    assert body["webhook_security"]["linear"] == {
        "configured": False,
        "required": True,
        "ready": False,
    }
    assert {alert["code"] for alert in body["alerts"]} >= {
        "notion_not_ready",
        "google_docs_not_ready",
        "figma_not_ready",
        "api_auth_disabled",
        "rate_limit_disabled",
        "linear_webhook_secret_missing",
        "github_webhook_secret_missing",
        "slack_signing_secret_missing",
        "telegram_secret_missing",
    }


def test_api_auth_can_protect_non_exempt_routes() -> None:
    client = TestClient(
        create_app(Settings(api_auth_enabled=True, api_auth_keys="secret"))
    )

    assert client.get("/healthz").status_code == 200
    assert client.get("/ops/status").status_code == 401
    assert client.get("/ops/status", headers={"X-API-Key": "secret"}).status_code == 200
    assert (
        client.get("/ops/status", headers={"Authorization": "Bearer secret"}).status_code
        == 200
    )


def test_api_auth_fails_closed_when_enabled_without_keys() -> None:
    client = TestClient(create_app(Settings(api_auth_enabled=True)))

    response = client.get("/ops/status")

    assert response.status_code == 503
    assert response.json()["detail"] == "API auth is enabled but no API keys are configured"


def test_rate_limit_can_protect_non_exempt_routes() -> None:
    client = TestClient(
        create_app(
            Settings(
                api_rate_limit_enabled=True,
                api_rate_limit_requests_per_minute=1,
            )
        )
    )

    first = client.get("/ops/status")
    second = client.get("/ops/status")

    assert client.get("/healthz").status_code == 200
    assert first.status_code == 200
    assert second.status_code == 429
    assert second.headers["Retry-After"]
