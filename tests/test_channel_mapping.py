import hmac
import json
import time
from hashlib import sha256

from fastapi.testclient import TestClient

from agentic_sdlc_platform.app import create_app
from agentic_sdlc_platform.core.config import Settings
from agentic_sdlc_platform.ports.graph_store import GraphQuery, GraphQueryResult
from agentic_sdlc_platform.ports.hermes_session import HermesSessionRequest, HermesSessionResponse


class FakeRepo:
    name = "erp-service"
    metadata_json = {}
    default_branch = "main"


class FakeRepository:
    async def get_repo_by_name(self, name: str):
        return FakeRepo() if name == "erp-service" else None


class FakeGraphStore:
    def __init__(self) -> None:
        self.queries: list[GraphQuery] = []

    async def query(self, request: GraphQuery) -> GraphQueryResult:
        self.queries.append(request)
        return GraphQueryResult(
            provider="graphify",
            answer="Graph answer.",
            references=["src/allocation.py"],
        )


class FakeHermesSession:
    def __init__(self) -> None:
        self.requests: list[HermesSessionRequest] = []

    async def ask(self, request: HermesSessionRequest) -> HermesSessionResponse:
        self.requests.append(request)
        return HermesSessionResponse(session_id="session-1", message_id="message-1")


def write_mapping(tmp_path) -> str:
    mapping_path = tmp_path / "channel-mapping.toml"
    mapping_path.write_text(
        """
[[channels]]
provider = "slack"
channel = "C123"
repo = "erp-service"
allowed_senders = ["U123"]

[[channels]]
provider = "telegram"
channel = "-1001234567890"
repo = "erp-service"
allowed_senders = ["7"]
""".strip(),
        encoding="utf-8",
    )
    return str(mapping_path)


def signed_slack_headers(body: bytes, secret: str) -> dict[str, str]:
    timestamp = str(int(time.time()))
    base = b"v0:" + timestamp.encode("utf-8") + b":" + body
    digest = hmac.new(secret.encode("utf-8"), base, sha256).hexdigest()
    return {
        "X-Slack-Request-Timestamp": timestamp,
        "X-Slack-Signature": f"v0={digest}",
        "Content-Type": "application/json",
    }


def test_generic_channel_ingress_applies_mapped_repo(tmp_path) -> None:
    hermes_session = FakeHermesSession()
    client = TestClient(
        create_app(
            Settings(
                channel_mapping_path=write_mapping(tmp_path),
                vendor_http_enabled=False,
            ),
            hermes_session=hermes_session,
        )
    )

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
    assert hermes_session.requests[0].repo == "erp-service"


def test_generic_channel_ingress_uses_graph_for_mapped_repo_when_enabled(tmp_path) -> None:
    hermes_session = FakeHermesSession()
    graph_store = FakeGraphStore()
    client = TestClient(
        create_app(
            Settings(
                channel_mapping_path=write_mapping(tmp_path),
                vendor_http_enabled=True,
            ),
            repository=FakeRepository(),
            graph_store=graph_store,
            hermes_session=hermes_session,
        )
    )

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
    assert response.json()["route"] == "graph_repo_query"
    assert response.json()["answer"] == "Graph answer."
    assert hermes_session.requests == []
    assert graph_store.queries == [
        GraphQuery(
            repo="erp-service",
            question="How does FEFO allocation work?",
            metadata={"default_branch": "main"},
        )
    ]


def test_generic_channel_ingress_rejects_unknown_mapped_channel(tmp_path) -> None:
    client = TestClient(create_app(Settings(channel_mapping_path=write_mapping(tmp_path))))

    response = client.post(
        "/channels/messages",
        json={
            "provider": "slack",
            "channel": "UNKNOWN",
            "sender_id": "U123",
            "text": "How does FEFO allocation work?",
        },
    )

    assert response.status_code == 403


def test_slack_ingress_rejects_disallowed_sender_from_mapped_channel(tmp_path) -> None:
    body = json.dumps(
        {
            "type": "event_callback",
            "event": {
                "type": "app_mention",
                "channel": "C123",
                "user": "U999",
                "text": "<@BOT> How does FEFO allocation work?",
            },
        }
    ).encode("utf-8")
    client = TestClient(
        create_app(
            Settings(
                slack_signing_secret="secret",
                channel_mapping_path=write_mapping(tmp_path),
            )
        )
    )

    response = client.post(
        "/channels/slack/events",
        content=body,
        headers=signed_slack_headers(body, "secret"),
    )

    assert response.status_code == 403


def test_telegram_ingress_applies_mapped_repo(tmp_path) -> None:
    hermes_session = FakeHermesSession()
    client = TestClient(
        create_app(
            Settings(
                telegram_secret_token="secret",
                channel_mapping_path=write_mapping(tmp_path),
                vendor_http_enabled=False,
            ),
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
    assert hermes_session.requests[0].repo == "erp-service"
