from fastapi.testclient import TestClient

from agentic_sdlc_platform.app import create_app
from agentic_sdlc_platform.core.config import Settings
from agentic_sdlc_platform.ports.hermes_session import HermesSessionRequest, HermesSessionResponse


class FakeHermesSession:
    def __init__(self) -> None:
        self.requests: list[HermesSessionRequest] = []

    async def ask(self, request: HermesSessionRequest) -> HermesSessionResponse:
        self.requests.append(request)
        return HermesSessionResponse(session_id="session-1", message_id="message-1")


def test_telegram_ingress_rejects_invalid_secret_token_when_configured() -> None:
    client = TestClient(create_app(Settings(telegram_secret_token="secret")))

    response = client.post(
        "/channels/telegram/webhook",
        json={"message": {"chat": {"id": 42}, "from": {"id": 7}, "text": "hello"}},
        headers={"X-Telegram-Bot-Api-Secret-Token": "bad"},
    )

    assert response.status_code == 401


def test_telegram_message_routes_to_hermes() -> None:
    hermes_session = FakeHermesSession()
    client = TestClient(
        create_app(
            Settings(telegram_secret_token="secret"),
            hermes_session=hermes_session,
        )
    )

    response = client.post(
        "/channels/telegram/webhook",
        json={
            "message": {
                "chat": {"id": -1001234567890},
                "from": {"id": 7},
                "text": "How does FEFO allocation work?",
            }
        },
        headers={"X-Telegram-Bot-Api-Secret-Token": "secret"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "route": "hermes_direct",
        "session_id": "session-1",
        "message_id": "message-1",
    }
    assert hermes_session.requests == [
        HermesSessionRequest(
            provider="telegram",
            channel="-1001234567890",
            sender_id="7",
            text="How does FEFO allocation work?",
            repo=None,
        )
    ]


def test_telegram_implementation_command_routes_to_multica_task_without_hermes() -> None:
    client = TestClient(create_app(Settings()))

    response = client.post(
        "/channels/telegram/webhook",
        json={"message": {"chat": {"id": 42}, "from": {"id": 7}, "text": "/implement OS-1284"}},
    )

    assert response.status_code == 200
    assert response.json()["route"] == "multica_task"
