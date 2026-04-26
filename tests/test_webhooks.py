import hmac
from hashlib import sha256

from fastapi.testclient import TestClient

from agentic_sdlc_platform.app import create_app
from agentic_sdlc_platform.core.config import Settings


def test_linear_webhook_accepts_payload_when_secret_not_configured() -> None:
    client = TestClient(create_app(Settings()))

    response = client.post("/webhooks/linear", content=b'{"type":"Issue"}')

    assert response.status_code == 202
    assert response.json() == {"accepted": True, "source": "linear", "task_id": None}


def test_github_webhook_requires_event_header() -> None:
    client = TestClient(create_app(Settings()))

    response = client.post("/webhooks/github", content=b"{}")

    assert response.status_code == 400


def test_github_webhook_validates_signature_when_secret_configured() -> None:
    payload = b'{"action":"opened"}'
    digest = hmac.new(b"secret", payload, sha256).hexdigest()
    client = TestClient(create_app(Settings(github_webhook_secret="secret")))

    response = client.post(
        "/webhooks/github",
        content=payload,
        headers={
            "X-GitHub-Event": "pull_request",
            "X-Hub-Signature-256": f"sha256={digest}",
        },
    )

    assert response.status_code == 202
    assert response.json() == {
        "accepted": True,
        "source": "github:pull_request",
        "task_id": None,
    }
