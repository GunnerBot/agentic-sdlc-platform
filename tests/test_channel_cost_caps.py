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


def test_channel_cost_cap_blocks_requests_after_budget_is_exhausted() -> None:
    hermes_session = FakeHermesSession()
    client = TestClient(
        create_app(
            Settings(
                channel_cost_cap_usd=0.50,
                channel_default_request_cost_usd=0.25,
            ),
            hermes_session=hermes_session,
        )
    )

    payload = {
        "provider": "slack",
        "channel": "C123",
        "sender_id": "U123",
        "text": "How does FEFO allocation work?",
    }

    first = client.post("/channels/messages", json=payload)
    second = client.post("/channels/messages", json=payload)
    third = client.post("/channels/messages", json=payload)

    assert first.status_code == 202
    assert second.status_code == 202
    assert third.status_code == 429
    assert third.json()["detail"] == "Channel cost cap exceeded"
    assert len(hermes_session.requests) == 2


def test_channel_cost_cap_is_isolated_per_channel() -> None:
    hermes_session = FakeHermesSession()
    client = TestClient(
        create_app(
            Settings(
                channel_cost_cap_usd=0.25,
                channel_default_request_cost_usd=0.25,
            ),
            hermes_session=hermes_session,
        )
    )

    first = client.post(
        "/channels/messages",
        json={
            "provider": "slack",
            "channel": "C123",
            "sender_id": "U123",
            "text": "How does FEFO allocation work?",
        },
    )
    second = client.post(
        "/channels/messages",
        json={
            "provider": "slack",
            "channel": "C999",
            "sender_id": "U123",
            "text": "How does FEFO allocation work?",
        },
    )

    assert first.status_code == 202
    assert second.status_code == 202
    assert len(hermes_session.requests) == 2
