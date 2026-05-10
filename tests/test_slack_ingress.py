import hmac
import json
import time
from hashlib import sha256
from types import SimpleNamespace

import httpx
from fastapi.testclient import TestClient

from agentic_sdlc_platform.adapters.slack import SlackClient
from agentic_sdlc_platform.app import create_app
from agentic_sdlc_platform.core.config import Settings
from agentic_sdlc_platform.glue.ticket_command import TicketThreadContext
from agentic_sdlc_platform.ports.graph_store import GraphQuery, GraphQueryResult
from agentic_sdlc_platform.ports.hermes_session import (
    HermesSessionRequest,
    HermesSessionResponse,
    HermesStartSessionRequest,
)
from agentic_sdlc_platform.ports.issue_tracker import IssueCreateRequest, IssueCreateResponse
from agentic_sdlc_platform.ports.task_orchestrator import (
    TaskCommentRequest,
    TaskCommentResponse,
)


class FakeRepo:
    name = "erp-service"
    metadata_json = {}
    default_branch = "main"


class FakeSession:
    status = "active"


class FakeNode:
    node_key = "design"
    title = "Design"
    repo = "erp-service"
    depends_on = ()
    status = "queued"
    orchestrator_task_id = "multica-node-1"
    orchestrator_status = "queued"
    metadata_json = {"expected_pr_reference": "dag/dag-1/design"}


class FakeDag:
    id = "dag-1"
    status = "planned"
    nodes = [FakeNode()]


class FakeTask:
    id = "task-1"
    external_id = "ENG-1284"
    status = "queued"
    repo = "erp-service"
    orchestrator_task_id = "multica-task-1"
    orchestrator_status = "queued"
    sessions = [FakeSession()]
    dags = [FakeDag()]


class FakeHermesSession:
    def __init__(self) -> None:
        self.requests: list[HermesSessionRequest] = []
        self.started: list[HermesStartSessionRequest] = []
        self.resumed: list[tuple[str, str, str]] = []

    async def ask(self, request: HermesSessionRequest) -> HermesSessionResponse:
        self.requests.append(request)
        return HermesSessionResponse(session_id="session-1", message_id="message-1")

    async def start_session(self, request: HermesStartSessionRequest) -> HermesSessionResponse:
        self.started.append(request)
        return HermesSessionResponse(session_id="session-1", message_id="message-1")

    async def resume_session(
        self,
        session_id: str,
        text: str,
        actor: str,
    ) -> HermesSessionResponse:
        self.resumed.append((session_id, text, actor))
        return HermesSessionResponse(
            session_id=session_id,
            message_id="message-2",
            answer="Follow-up answer.",
        )


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
            external_id="ENG-1284",
            url="https://linear.app/acme/issue/ENG-1284",
        )


class FakeTaskOrchestrator:
    provider = "multica"

    def __init__(self) -> None:
        self.comments: list[TaskCommentRequest] = []

    async def add_comment(self, request: TaskCommentRequest) -> TaskCommentResponse:
        self.comments.append(request)
        return TaskCommentResponse(
            external_task_id=request.external_task_id,
            comment_id="multica-comment-1",
            status="commented",
            metadata={"multica_comment_id": "multica-comment-1"},
        )


class FakeRepository:
    def __init__(self) -> None:
        FakeTask.dags[0].nodes[0].status = "queued"
        FakeTask.dags[0].nodes[0].orchestrator_task_id = "multica-node-1"
        FakeTask.dags[0].nodes[0].orchestrator_status = "queued"
        FakeTask.dags[0].nodes[0].metadata_json = {"expected_pr_reference": "dag/dag-1/design"}
        self.agent_sessions: dict[tuple[str, str], SimpleNamespace] = {}
        self.session_events: list[tuple[str, str, str, str, str | None]] = []
        self.artifacts: list[dict[str, object]] = []
        self.audit_events: list[dict[str, object]] = []

    async def get_repo_by_name(self, name: str):
        return FakeRepo() if name == "erp-service" else None

    async def find_task_by_external_id(self, external_id: str):
        return FakeTask() if external_id == "ENG-1284" else None

    async def record_inbound_event(self, source, delivery_id, event_type, payload):
        return SimpleNamespace(
            event=SimpleNamespace(id=f"event-{delivery_id}"),
            created=True,
        )

    async def create_task_from_event(self, event_id, source, external_id, title, repo):
        return SimpleNamespace(id=f"task-{external_id}", external_id=external_id)

    async def create_agent_session(
        self,
        task_id,
        provider,
        external_thread_id,
        hermes_session_id,
        repo,
        **kwargs,
    ):
        session = SimpleNamespace(
            id=f"session-{external_thread_id}",
            task_id=task_id,
            provider=provider,
            external_thread_id=external_thread_id,
            hermes_session_id=hermes_session_id,
            orchestrator_provider=kwargs.get("orchestrator_provider"),
            orchestrator_issue_id=kwargs.get("orchestrator_issue_id"),
            orchestrator_task_id=kwargs.get("orchestrator_task_id"),
            repo=repo,
            events=[],
        )
        self.agent_sessions[(provider, external_thread_id)] = session
        return session

    async def find_agent_session(self, provider, external_thread_id):
        return self.agent_sessions.get((provider, external_thread_id))

    async def record_session_event(
        self,
        session_id,
        direction,
        event_type,
        actor,
        message,
        metadata=None,
    ):
        self.session_events.append((session_id, direction, event_type, actor, message))
        return SimpleNamespace(id=f"event-{len(self.session_events)}")

    async def update_dag_node_status(
        self,
        dag_id,
        node_key,
        status,
        orchestrator_status=None,
        metadata=None,
    ):
        node = FakeTask.dags[0].nodes[0]
        node.status = status
        node.orchestrator_status = orchestrator_status
        if metadata:
            node.metadata_json.update(metadata)
        return node

    async def retry_dag_node(self, dag_id, node_key):
        node = FakeTask.dags[0].nodes[0]
        node.status = "ready"
        node.orchestrator_task_id = None
        node.orchestrator_status = None
        retry_count = node.metadata_json.get("retry_count", 0)
        node.metadata_json["retry_count"] = retry_count + 1
        return node

    async def mark_dag_node_skipped(self, dag_id, node_key):
        node = FakeTask.dags[0].nodes[0]
        node.status = "skipped"
        node.orchestrator_status = "skipped"
        return node

    async def list_dag_node_executions(self, dag_id, node_key=None):
        return []

    async def update_dag_node_execution(self, execution_id, status, error=None):
        return SimpleNamespace(id=execution_id, status=status, error=error)

    async def update_dag_node_metadata(self, dag_id, node_key, metadata):
        node = FakeTask.dags[0].nodes[0]
        node.metadata_json.update(metadata)
        return node

    async def create_task_artifact(
        self,
        task_id,
        kind,
        name,
        content,
        metadata=None,
        dag_id=None,
        node_key=None,
        execution_id=None,
    ):
        artifact = {
            "task_id": task_id,
            "kind": kind,
            "name": name,
            "content": content,
            "metadata": metadata or {},
            "dag_id": dag_id,
            "node_key": node_key,
            "execution_id": execution_id,
        }
        self.artifacts.append(artifact)
        return SimpleNamespace(id=f"artifact-{len(self.artifacts)}", **artifact)

    async def record_audit_event(
        self,
        action,
        actor,
        target_type,
        target_id,
        metadata=None,
    ):
        event = {
            "action": action,
            "actor": actor,
            "target_type": target_type,
            "target_id": target_id,
            "metadata": metadata or {},
        }
        self.audit_events.append(event)
        return SimpleNamespace(id=f"audit-{len(self.audit_events)}", **event)


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
        "https://slack.local/api/conversations.replies?channel=C123&ts=1710000000.000000"
    )
    assert captured_request.headers["authorization"] == "Bearer xoxb-token"
    assert context is not None
    assert context.title == "FEFO allocation picks the wrong lot in staging."
    assert context.transcript == (
        "U111: FEFO allocation picks the wrong lot in staging.\n"
        "U222: Expected oldest expiring lot to be selected."
    )
    assert context.message_count == 2


async def test_slack_client_posts_thread_reply() -> None:
    captured_request: httpx.Request | None = None

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal captured_request
        captured_request = request
        return httpx.Response(status_code=200, json={"ok": True, "ts": "1710000001.000000"})

    message_ts = await SlackClient(
        Settings(
            slack_bot_token="xoxb-token",
            slack_api_base_url="https://slack.local/api",
        ),
        transport=httpx.MockTransport(handler),
    ).post_thread_reply(
        channel="C123",
        thread_ts="1710000000.000000",
        text="Agent found the answer.",
    )

    assert captured_request is not None
    assert str(captured_request.url) == "https://slack.local/api/chat.postMessage"
    assert captured_request.headers["authorization"] == "Bearer xoxb-token"
    assert json.loads(captured_request.content) == {
        "channel": "C123",
        "thread_ts": "1710000000.000000",
        "text": "Agent found the answer.",
    }
    assert message_ts == "1710000001.000000"


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
    repository = FakeRepository()
    client = TestClient(
        create_app(
            Settings(slack_signing_secret="secret"),
            hermes_session=hermes_session,
            repository=repository,
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
    assert hermes_session.started == [
        HermesStartSessionRequest(
            task_id="task-C123:root",
            provider="slack",
            external_thread_id="C123:root",
            text="How does FEFO allocation work?",
            repo=None,
        )
    ]
    assert repository.session_events == [
        (
            "session-C123:root",
            "outbound",
            "session_started",
            "system",
            "How does FEFO allocation work?",
        )
    ]


def test_slack_thread_followup_resumes_stored_session() -> None:
    repository = FakeRepository()
    existing = SimpleNamespace(
        id="session-C123:1710000000.000000",
        task_id="task-1",
        provider="slack",
        external_thread_id="C123:1710000000.000000",
        hermes_session_id="hermes-session-1",
        repo="erp-service",
    )
    repository.agent_sessions[("slack", "C123:1710000000.000000")] = existing
    hermes_session = FakeHermesSession()
    body = json.dumps(
        {
            "type": "event_callback",
            "event": {
                "type": "message",
                "channel": "C123",
                "user": "U123",
                "thread_ts": "1710000000.000000",
                "text": "Which class has the default mismatch?",
            },
        }
    ).encode("utf-8")
    client = TestClient(
        create_app(
            Settings(slack_signing_secret="secret"),
            repository=repository,
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
        "session_id": "hermes-session-1",
        "message_id": "message-2",
    }
    assert hermes_session.resumed == [
        (
            "hermes-session-1",
            "Which class has the default mismatch?",
            "slack:U123",
        )
    ]
    assert repository.session_events == [
        (
            "session-C123:1710000000.000000",
            "inbound",
            "comment",
            "slack:U123",
            "Which class has the default mismatch?",
        ),
        (
            "session-C123:1710000000.000000",
            "outbound",
            "reply",
            "agent",
            "Follow-up answer.",
        ),
    ]


def test_slack_thread_followup_on_multica_session_adds_multica_comment() -> None:
    repository = FakeRepository()
    existing = SimpleNamespace(
        id="session-C123:1710000000.000000",
        task_id="task-1",
        provider="slack",
        external_thread_id="C123:1710000000.000000",
        hermes_session_id=None,
        orchestrator_provider="multica",
        orchestrator_issue_id="multica-issue-1",
        orchestrator_task_id="multica-task-1",
        repo="erp-service",
    )
    repository.agent_sessions[("slack", "C123:1710000000.000000")] = existing
    task_orchestrator = FakeTaskOrchestrator()
    body = json.dumps(
        {
            "type": "event_callback",
            "event": {
                "type": "message",
                "channel": "C123",
                "user": "U123",
                "thread_ts": "1710000000.000000",
                "text": "What exact class has the dryRun mismatch?",
            },
        }
    ).encode("utf-8")
    client = TestClient(
        create_app(
            Settings(slack_signing_secret="secret"),
            repository=repository,
            task_orchestrator=task_orchestrator,
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
        "session_id": None,
        "message_id": "multica-comment-1",
    }
    assert task_orchestrator.comments == [
        TaskCommentRequest(
            external_task_id="multica-task-1",
            body="What exact class has the dryRun mismatch?",
            actor="slack:U123",
            metadata={
                "multica_issue_id": "multica-issue-1",
                "provider": "slack",
                "external_thread_id": "C123:1710000000.000000",
            },
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
                "text": "<@BOT> repo:erp-service Where does allocation live?",
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
        "repo": "erp-service",
        "answer": "Allocation lives in inventory/allocation.py.",
        "references": ["inventory/allocation.py"],
        "session_id": None,
        "message_id": None,
    }
    assert graph_store.queries == [
        GraphQuery(
            repo="erp-service",
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
                    "<@BOT> /create-ticket repo:erp-service type:feature "
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
        "repo": "erp-service",
        "issue_id": "issue-id-1",
        "external_id": "ENG-1284",
        "url": "https://linear.app/acme/issue/ENG-1284",
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
                "Repo: erp-service\n"
                "Template: feature\n"
                "\n"
                "Carry over Slack context."
            ),
            repo="erp-service",
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
                "U111: FEFO allocation picks wrong lot.\nU222: Expected oldest expiring lot."
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


def test_slack_app_mention_task_nodes_command_returns_task_info() -> None:
    body = json.dumps(
        {
            "type": "event_callback",
            "event": {
                "type": "app_mention",
                "channel": "C123",
                "user": "U123",
                "text": "<@BOT> /nodes ENG-1284",
            },
        }
    ).encode("utf-8")
    client = TestClient(
        create_app(
            Settings(slack_signing_secret="secret"),
            repository=FakeRepository(),
        )
    )

    response = client.post(
        "/channels/slack/events",
        content=body,
        headers=signed_slack_headers(body, "secret"),
    )

    assert response.status_code == 200
    assert response.json()["route"] == "task_info"
    assert response.json()["command"] == "nodes"
    assert response.json()["task_id"] == "task-1"
    assert response.json()["answer"] == (
        "Task ENG-1284 nodes:\n"
        "Next runnable: none\n"
        "- design: queued; repo erp-service; depends_on none; "
        "orchestrator multica-node-1; pr none; failure none"
    )


def test_slack_revise_node_records_feedback_and_retries_node() -> None:
    repository = FakeRepository()
    body = json.dumps(
        {
            "type": "event_callback",
            "event": {
                "type": "app_mention",
                "channel": "C123",
                "user": "U123",
                "thread_ts": "1710000000.000000",
                "text": (
                    "<@BOT> /revise-node ENG-1284 design "
                    "Please address the thread feedback before retrying."
                ),
            },
        }
    ).encode("utf-8")
    client = TestClient(
        create_app(
            Settings(slack_signing_secret="secret"),
            repository=repository,
        )
    )

    response = client.post(
        "/channels/slack/events",
        content=body,
        headers=signed_slack_headers(body, "secret"),
    )

    assert response.status_code == 200
    assert response.json()["route"] == "node_override"
    assert response.json()["command"] == "revise-node"
    assert response.json()["answer"] == "Node design on ENG-1284 is now ready."
    assert repository.artifacts[0]["kind"] == "dag_node_revision_request"
    assert repository.artifacts[0]["content"] == {
        "external_id": "ENG-1284",
        "dag_id": "dag-1",
        "node_key": "design",
        "feedback": "Please address the thread feedback before retrying.",
        "actor": "U123",
        "channel": "C123:1710000000.000000",
    }
    assert repository.audit_events[0]["action"] == "human_override.revise-node"
