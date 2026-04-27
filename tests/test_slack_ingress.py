import hmac
import json
import time
from hashlib import sha256

import httpx
from fastapi.testclient import TestClient

from agentic_sdlc_platform.adapters.slack import SlackClient
from agentic_sdlc_platform.app import create_app
from agentic_sdlc_platform.core.config import Settings
from agentic_sdlc_platform.glue.ticket_command import TicketThreadContext
from agentic_sdlc_platform.ports.graph_store import GraphQuery, GraphQueryResult
from agentic_sdlc_platform.ports.hermes_session import HermesSessionRequest, HermesSessionResponse
from agentic_sdlc_platform.ports.issue_tracker import IssueCreateRequest, IssueCreateResponse


class FakeRepo:
    name = "keychain-os-erp"
    metadata_json = {}
    default_branch = "main"


class FakeHermesSession:
    def __init__(self) -> None:
        self.requests: list[HermesSessionRequest] = []

    async def ask(self, request: HermesSessionRequest) -> HermesSessionResponse:
        self.requests.append(request)
        return HermesSessionResponse(session_id="session-1", message_id="message-1")


class FakeGraphStore:
    def __init__(self) -> None:
        self.queries: list[GraphQuery] = []

    async def query(self, request: GraphQuery) -> GraphQueryResult:
        self.queries.append(request)
        return GraphQueryResult(
            provider="graphify",
            answer="Allocation lives in inventory/allocation.py.",
            references=["inventory/allocation.py"],
        )


class FakeIssueTracker:
    def __init__(self) -> None:
        self.created: list[IssueCreateRequest] = []

    async def create_issue(self, request: IssueCreateRequest) -> IssueCreateResponse:
        self.created.append(request)
        return IssueCreateResponse(
            issue_id="issue-id-1",
            external_id="OS-1284",
            url="https://linear.app/keychain/issue/OS-1284",
        )


class FakeRepository:
    async def get_repo_by_name(self, name: str):
        return FakeRepo() if name == "keychain-os-erp" else None


async def test_slack_client_fetches_thread_context() -> None:
    captured_request: httpx.Request | None = None

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal captured_request
        captured_request = request
        return httpx.Response(
            status_code=200,
            json={
                "ok": True,
                "messages": [
                    {
                        "user": "U111",
                        "text": "FEFO allocation picks the wrong lot in staging.",
                    },
                    {
                        "user": "U222",
                        "text": "Expected oldest expiring lot to be selected.",
                    },
                ],
            },
        )

    context = await SlackClient(
        Settings(
            slack_bot_token="xoxb-token",
            slack_api_base_url="https://slack.local/api",
        ),
        transport=httpx.MockTransport(handler),
    ).fetch_thread_context(channel="C123", thread_ts="1710000000.000000")

    assert captured_request is not None
    assert str(captured_request.url) == (
        "https://slack.local/api/conversations.replies?"
        "channel=C123&ts=1710000000.000000"
    )
    assert captured_request.headers["authorization"] == "Bearer xoxb-token"
    assert context is not None
    assert context.title == "FEFO allocation picks the wrong lot in staging."
    assert context.transcript == (
        "U111: FEFO allocation picks the wrong lot in staging.\n"
        "U222: Expected oldest expiring lot to be selected."
    )
    assert context.message_count == 2


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


def test_slack_app_mention_routes_repo_question_to_graph_store() -> None:
    body = json.dumps(
        {
            "type": "event_callback",
            "event": {
                "type": "app_mention",
                "channel": "C123",
                "user": "U123",
                "text": "<@BOT> repo:keychain-os-erp Where does allocation live?",
            },
        }
    ).encode("utf-8")
    graph_store = FakeGraphStore()
    client = TestClient(
        create_app(
            Settings(slack_signing_secret="secret"),
            repository=FakeRepository(),
            graph_store=graph_store,
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
        "route": "graph_repo_query",
        "repo": "keychain-os-erp",
        "answer": "Allocation lives in inventory/allocation.py.",
        "references": ["inventory/allocation.py"],
        "session_id": None,
        "message_id": None,
    }
    assert graph_store.queries == [
        GraphQuery(
            repo="keychain-os-erp",
            question="Where does allocation live?",
            metadata={"default_branch": "main"},
        )
    ]


def test_slack_create_ticket_command_creates_linear_issue_with_message_context() -> None:
    body = json.dumps(
        {
            "type": "event_callback",
            "event": {
                "type": "app_mention",
                "channel": "C123",
                "user": "U123",
                "ts": "1710000000.000100",
                "thread_ts": "1710000000.000000",
                "text": (
                    "<@BOT> /create-ticket repo:keychain-os-erp type:feature "
                    "Add FEFO allocation support | Carry over Slack context."
                ),
            },
        }
    ).encode("utf-8")
    issue_tracker = FakeIssueTracker()
    client = TestClient(
        create_app(
            Settings(slack_signing_secret="secret"),
            issue_tracker=issue_tracker,
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
        "route": "create_ticket",
        "command": "create-ticket",
        "repo": "keychain-os-erp",
        "issue_id": "issue-id-1",
        "external_id": "OS-1284",
        "url": "https://linear.app/keychain/issue/OS-1284",
        "session_id": None,
        "message_id": None,
    }
    assert issue_tracker.created == [
        IssueCreateRequest(
            title="Add FEFO allocation support",
            description=(
                "Created from channel command.\n"
                "Provider: slack\n"
                "Channel: C123\n"
                "Sender: U123\n"
                "Message timestamp: 1710000000.000100\n"
                "Thread timestamp: 1710000000.000000\n"
                "Repo: keychain-os-erp\n"
                "Template: feature\n"
                "\n"
                "Carry over Slack context."
            ),
            repo="keychain-os-erp",
            metadata={
                "provider": "slack",
                "channel": "C123",
                "sender_id": "U123",
                "message_ts": "1710000000.000100",
                "thread_ts": "1710000000.000000",
                "template": "feature",
            },
        )
    ]


def test_slack_bare_create_ticket_uses_thread_context(monkeypatch) -> None:
    async def fake_fetch_thread_context(self, channel: str, thread_ts: str):
        assert channel == "C123"
        assert thread_ts == "1710000000.000000"
        return TicketThreadContext(
            title="FEFO allocation picks wrong lot",
            transcript=(
                "U111: FEFO allocation picks wrong lot.\n"
                "U222: Expected oldest expiring lot."
            ),
            message_count=2,
        )

    monkeypatch.setattr(SlackClient, "fetch_thread_context", fake_fetch_thread_context)
    body = json.dumps(
        {
            "type": "event_callback",
            "event": {
                "type": "app_mention",
                "channel": "C123",
                "user": "U123",
                "ts": "1710000000.000100",
                "thread_ts": "1710000000.000000",
                "text": "<@BOT> /create-ticket",
            },
        }
    ).encode("utf-8")
    issue_tracker = FakeIssueTracker()
    client = TestClient(
        create_app(
            Settings(slack_signing_secret="secret", slack_bot_token="xoxb-token"),
            issue_tracker=issue_tracker,
        )
    )

    response = client.post(
        "/channels/slack/events",
        content=body,
        headers=signed_slack_headers(body, "secret"),
    )

    assert response.status_code == 200
    assert response.json()["route"] == "create_ticket"
    assert issue_tracker.created == [
        IssueCreateRequest(
            title="FEFO allocation picks wrong lot",
            description=(
                "Created from channel command.\n"
                "Provider: slack\n"
                "Channel: C123\n"
                "Sender: U123\n"
                "Message timestamp: 1710000000.000100\n"
                "Thread timestamp: 1710000000.000000\n"
                "Template: bug\n"
                "Thread messages: 2\n"
                "\n"
                "Thread context:\n"
                "U111: FEFO allocation picks wrong lot.\n"
                "U222: Expected oldest expiring lot."
            ),
            metadata={
                "provider": "slack",
                "channel": "C123",
                "sender_id": "U123",
                "message_ts": "1710000000.000100",
                "thread_ts": "1710000000.000000",
                "template": "bug",
                "thread_message_count": 2,
            },
        )
    ]
