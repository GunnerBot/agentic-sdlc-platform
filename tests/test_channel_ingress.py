from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from agentic_sdlc_platform.app import create_app
from agentic_sdlc_platform.core.config import Settings
from agentic_sdlc_platform.glue.dag_decomposer import Subtask
from agentic_sdlc_platform.persistence.models import Base
from agentic_sdlc_platform.persistence.repository import PersistenceRepository
from agentic_sdlc_platform.ports.graph_store import GraphQuery, GraphQueryResult
from agentic_sdlc_platform.ports.hermes_session import HermesSessionRequest, HermesSessionResponse
from agentic_sdlc_platform.ports.issue_tracker import IssueCreateRequest, IssueCreateResponse


class FakeRepo:
    name = "erp-service"
    metadata_json = {"linear_team_key": "OS"}
    default_branch = "main"


class FakeSession:
    status = "active"


class FakeTask:
    id = "task-1"
    external_id = "ENG-1284"
    status = "queued"
    repo = "erp-service"
    orchestrator_task_id = "multica-task-1"
    orchestrator_status = "queued"
    sessions = [FakeSession()]
    dags = []


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


class FakeRepository:
    async def get_repo_by_name(self, name: str):
        return FakeRepo() if name == "erp-service" else None

    async def find_task_by_external_id(self, external_id: str):
        return FakeTask() if external_id == "ENG-1284" else None


async def build_repository() -> PersistenceRepository:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return PersistenceRepository(async_sessionmaker(engine, expire_on_commit=False))


async def create_task_with_dag(repository: PersistenceRepository) -> str:
    event = await repository.record_inbound_event(
        source="linear",
        delivery_id="delivery-1",
        event_type="Issue",
        payload={},
    )
    task = await repository.create_task_from_event(
        event_id=event.event.id,
        source="linear",
        external_id="ENG-1284",
        title="Build allocation",
        repo="erp-service",
    )
    await repository.create_task_dag(
        task_id=task.id,
        subtasks=[
            Subtask(id="api", title="Add API"),
            Subtask(id="web", title="Add web", depends_on=("api",)),
        ],
    )
    return task.id


def test_channel_ingress_routes_questions_to_hermes_direct() -> None:
    client = TestClient(create_app(Settings(hermes_http_enabled=False)))

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
        "repo": None,
        "answer": None,
        "references": None,
        "issue_id": None,
        "external_id": None,
        "url": None,
    }


def test_channel_ingress_routes_implementation_requests_to_multica_task() -> None:
    client = TestClient(create_app(Settings()))

    response = client.post(
        "/channels/messages",
        json={
            "provider": "telegram",
            "channel": "-1001234567890",
            "sender_id": "42",
            "text": "/implement ENG-1284",
        },
    )

    assert response.status_code == 202
    assert response.json()["route"] == "multica_task"


def test_channel_ingress_create_ticket_command_creates_issue() -> None:
    issue_tracker = FakeIssueTracker()
    client = TestClient(create_app(Settings(), issue_tracker=issue_tracker))

    response = client.post(
        "/channels/messages",
        json={
            "provider": "slack",
            "channel": "C123",
            "sender_id": "U123",
            "text": "/create-ticket repo:erp-service type:bug Fix allocation bug",
        },
    )

    assert response.status_code == 202
    assert response.json()["route"] == "create_ticket"
    assert response.json()["command"] == "create-ticket"
    assert response.json()["issue_id"] == "issue-id-1"
    assert response.json()["external_id"] == "ENG-1284"
    assert response.json()["url"] == "https://linear.app/acme/issue/ENG-1284"
    assert issue_tracker.created == [
        IssueCreateRequest(
            title="Fix allocation bug",
            description=(
                "Created from channel command.\n"
                "Provider: slack\n"
                "Channel: C123\n"
                "Sender: U123\n"
                "Repo: erp-service\n"
                "Template: bug"
            ),
            repo="erp-service",
            metadata={
                "provider": "slack",
                "channel": "C123",
                "sender_id": "U123",
                "template": "bug",
            },
        )
    ]


def test_channel_ingress_invokes_hermes_for_direct_questions_when_configured() -> None:
    hermes_session = FakeHermesSession()
    client = TestClient(
        create_app(
            Settings(vendor_http_enabled=False),
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
            "repo": "erp-service",
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
        "repo": None,
        "answer": None,
        "references": None,
        "issue_id": None,
        "external_id": None,
        "url": None,
    }
    assert hermes_session.requests == [
        HermesSessionRequest(
            provider="slack",
            channel="C123",
            sender_id="U123",
            text="How does FEFO allocation work?",
            repo="erp-service",
        )
    ]


def test_channel_ingress_routes_repo_field_question_to_graph_when_enabled() -> None:
    graph_store = FakeGraphStore()
    hermes_session = FakeHermesSession()
    client = TestClient(
        create_app(
            Settings(vendor_http_enabled=True),
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
            "repo": "erp-service",
        },
    )

    assert response.status_code == 202
    assert response.json()["route"] == "graph_repo_query"
    assert response.json()["answer"] == "Allocation lives in inventory/allocation.py."
    assert hermes_session.requests == []
    assert graph_store.queries == [
        GraphQuery(
            repo="erp-service",
            question="How does FEFO allocation work?",
            metadata={"linear_team_key": "OS", "default_branch": "main"},
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


def test_channel_ingress_routes_repo_scoped_question_to_graph_store() -> None:
    graph_store = FakeGraphStore()
    client = TestClient(
        create_app(
            Settings(),
            repository=FakeRepository(),
            graph_store=graph_store,
        )
    )

    response = client.post(
        "/channels/messages",
        json={
            "provider": "slack",
            "channel": "C123",
            "sender_id": "U123",
            "text": "repo:erp-service Where does allocation live?",
        },
    )

    assert response.status_code == 202
    assert response.json()["route"] == "graph_repo_query"
    assert response.json()["repo"] == "erp-service"
    assert response.json()["answer"] == "Allocation lives in inventory/allocation.py."
    assert response.json()["references"] == ["inventory/allocation.py"]
    assert graph_store.queries == [
        GraphQuery(
            repo="erp-service",
            question="Where does allocation live?",
            metadata={"linear_team_key": "OS", "default_branch": "main"},
        )
    ]


def test_channel_ingress_task_status_command_returns_task_info() -> None:
    client = TestClient(create_app(Settings(), repository=FakeRepository()))

    response = client.post(
        "/channels/messages",
        json={
            "provider": "slack",
            "channel": "C123",
            "sender_id": "U123",
            "text": "/status ENG-1284",
        },
    )

    assert response.status_code == 202
    assert response.json()["route"] == "task_info"
    assert response.json()["command"] == "status"
    assert response.json()["task_id"] == "task-1"
    assert response.json()["answer"] == (
        "Task ENG-1284 status: queued. "
        "Orchestrator: multica-task-1 (queued). "
        "Repo: erp-service. Sessions: 1 active session. DAG: none."
    )


async def test_channel_ingress_running_why_blocked_and_node_override_commands() -> None:
    repository = await build_repository()
    task_id = await create_task_with_dag(repository)
    client = TestClient(create_app(Settings(), repository=repository))

    running = client.post(
        "/channels/messages",
        json={
            "provider": "slack",
            "channel": "C123",
            "sender_id": "U123",
            "text": "/running ENG-1284",
        },
    )
    blocked = client.post(
        "/channels/messages",
        json={
            "provider": "slack",
            "channel": "C123",
            "sender_id": "U123",
            "text": "/why-blocked ENG-1284",
        },
    )
    skipped = client.post(
        "/channels/messages",
        json={
            "provider": "slack",
            "channel": "C123",
            "sender_id": "U123",
            "text": "/skip-node ENG-1284 api duplicate work",
        },
    )

    assert running.status_code == 202
    assert running.json()["route"] == "task_info"
    assert running.json()["answer"] == "Task ENG-1284 running:\n- none"
    assert blocked.status_code == 202
    assert "- web: waiting on api" in blocked.json()["answer"]
    assert skipped.status_code == 202
    assert skipped.json()["route"] == "node_override"
    assert skipped.json()["task_id"] == task_id
    assert skipped.json()["answer"] == "Node api on ENG-1284 is now skipped."
