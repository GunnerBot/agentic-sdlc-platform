from fastapi.testclient import TestClient

from agentic_sdlc_platform.app import create_app
from agentic_sdlc_platform.core.config import Settings


def test_channel_ingress_routes_questions_to_hermes_direct() -> None:
    client = TestClient(create_app(Settings()))

    response = client.post(
        "/channels/messages",
        json={
            "provider": "slack",
            "channel": "C123",
            "sender_id": "U123",
            "text": "How does FEFO allocation work?",
        },
    )

    assert response.status_code == 202
    assert response.json() == {
        "accepted": True,
        "provider": "slack",
        "channel": "C123",
        "route": "hermes_direct",
    }


def test_channel_ingress_routes_implementation_requests_to_multica_task() -> None:
    client = TestClient(create_app(Settings()))

    response = client.post(
        "/channels/messages",
        json={
            "provider": "telegram",
            "channel": "-1001234567890",
            "sender_id": "42",
            "text": "/implement OS-1284",
        },
    )

    assert response.status_code == 202
    assert response.json()["route"] == "multica_task"


def test_channel_ingress_requires_supported_provider() -> None:
    client = TestClient(create_app(Settings()))

    response = client.post(
        "/channels/messages",
        json={
            "provider": "email",
            "channel": "inbox",
            "sender_id": "user",
            "text": "hello",
        },
    )

    assert response.status_code == 422
