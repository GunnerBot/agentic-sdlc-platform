from fastapi.testclient import TestClient

from agentic_sdlc_platform.app import create_app
from agentic_sdlc_platform.core.config import Settings
from agentic_sdlc_platform.ports.hermes_session import HermesSessionRequest, HermesSessionResponse


class FakeHermesSession:
    def __init__(self) -> None:
        self.requests: list[HermesSessionRequest] = []

    async def ask(self, request: HermesSessionRequest) -> HermesSessionResponse:
        self.requests.append(request)
        return HermesSessionResponse(
            session_id="session-1",
            message_id="message-1",
            answer="FEFO allocates oldest expiring lots first.",
        )


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
        "session_id": None,
        "message_id": None,
        "task_id": None,
        "command": None,
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


def test_channel_ingress_invokes_hermes_for_direct_questions_when_configured() -> None:
    hermes_session = FakeHermesSession()
    client = TestClient(create_app(Settings(), hermes_session=hermes_session))

    response = client.post(
        "/channels/messages",
        json={
            "provider": "slack",
            "channel": "C123",
            "sender_id": "U123",
            "text": "How does FEFO allocation work?",
            "repo": "keychain-os-erp",
        },
    )

    assert response.status_code == 202
    assert response.json() == {
        "accepted": True,
        "provider": "slack",
        "channel": "C123",
        "route": "hermes_direct",
        "session_id": "session-1",
        "message_id": "message-1",
        "task_id": None,
        "command": None,
    }
    assert hermes_session.requests == [
        HermesSessionRequest(
            provider="slack",
            channel="C123",
            sender_id="U123",
            text="How does FEFO allocation work?",
            repo="keychain-os-erp",
        )
    ]


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
