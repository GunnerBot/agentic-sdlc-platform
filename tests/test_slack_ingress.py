import hmac
import json
import time
from hashlib import sha256

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


def signed_slack_headers(body: bytes, secret: str) -> dict[str, str]:
    timestamp = str(int(time.time()))
    base = b"v0:" + timestamp.encode("utf-8") + b":" + body
    digest = hmac.new(secret.encode("utf-8"), base, sha256).hexdigest()
    return {
        "X-Slack-Request-Timestamp": timestamp,
        "X-Slack-Signature": f"v0={digest}",
        "Content-Type": "application/json",
    }


def test_slack_url_verification_returns_challenge() -> None:
    body = json.dumps(
        {
            "type": "url_verification",
            "challenge": "challenge-token",
        }
    ).encode("utf-8")
    client = TestClient(create_app(Settings(slack_signing_secret="secret")))

    response = client.post(
        "/channels/slack/events",
        content=body,
        headers=signed_slack_headers(body, "secret"),
    )

    assert response.status_code == 200
    assert response.json() == {"challenge": "challenge-token"}


def test_slack_ingress_rejects_invalid_signature_when_secret_configured() -> None:
    body = b'{"type":"event_callback","event":{"type":"app_mention","text":"hello"}}'
    client = TestClient(create_app(Settings(slack_signing_secret="secret")))

    response = client.post(
        "/channels/slack/events",
        content=body,
        headers={
            "X-Slack-Request-Timestamp": str(int(time.time())),
            "X-Slack-Signature": "v0=bad",
            "Content-Type": "application/json",
        },
    )

    assert response.status_code == 401


def test_slack_app_mention_routes_to_hermes() -> None:
    body = json.dumps(
        {
            "type": "event_callback",
            "event": {
                "type": "app_mention",
                "channel": "C123",
                "user": "U123",
                "text": "<@BOT> How does FEFO allocation work?",
            },
        }
    ).encode("utf-8")
    hermes_session = FakeHermesSession()
    client = TestClient(
        create_app(
            Settings(slack_signing_secret="secret"),
            hermes_session=hermes_session,
        )
    )

    response = client.post(
        "/channels/slack/events",
        content=body,
        headers=signed_slack_headers(body, "secret"),
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
            provider="slack",
            channel="C123",
            sender_id="U123",
            text="How does FEFO allocation work?",
            repo=None,
        )
    ]
