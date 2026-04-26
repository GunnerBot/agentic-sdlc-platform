import hmac
from hashlib import sha256

from fastapi.testclient import TestClient

from agentic_sdlc_platform.app import create_app
from agentic_sdlc_platform.core.config import Settings


class FakeEvent:
    id = "event-1"


class FakeTask:
    id = "task-1"


class FakeWriteResult:
    event = FakeEvent()
    created = True


class FakeRepository:
    async def record_inbound_event(self, **kwargs):
        return FakeWriteResult()

    async def create_task_from_event(self, **kwargs):
        return FakeTask()

    async def record_audit_event(self, **kwargs):
        return None


def build_client(settings: Settings | None = None) -> TestClient:
    return TestClient(create_app(settings or Settings(), repository=FakeRepository()))


def test_linear_webhook_accepts_payload_when_secret_not_configured() -> None:
    client = build_client()

    response = client.post(
        "/webhooks/linear",
        content=b'{"type":"Issue"}',
        headers={"Linear-Delivery": "delivery-1"},
    )

    assert response.status_code == 202
    assert response.json() == {
        "accepted": True,
        "source": "linear",
        "task_id": None,
        "delivery_id": "delivery-1",
        "duplicate": False,
    }


def test_github_webhook_schema_requires_event_header() -> None:
    client = build_client()

    response = client.post("/webhooks/github", content=b"{}")

    assert response.status_code == 422


def test_github_webhook_validates_signature_when_secret_configured() -> None:
    payload = b'{"action":"opened"}'
    digest = hmac.new(b"secret", payload, sha256).hexdigest()
    client = build_client(Settings(github_webhook_secret="secret"))

    response = client.post(
        "/webhooks/github",
        content=payload,
        headers={
            "X-GitHub-Event": "pull_request",
            "X-GitHub-Delivery": "delivery-1",
            "X-Hub-Signature-256": f"sha256={digest}",
        },
    )

    assert response.status_code == 202
    assert response.json() == {
        "accepted": True,
        "source": "github:pull_request",
        "task_id": None,
        "delivery_id": "delivery-1",
        "duplicate": False,
    }
