from fastapi.testclient import TestClient

from agentic_sdlc_platform.app import create_app
from agentic_sdlc_platform.core.config import Settings
from agentic_sdlc_platform.ports.graph_store import GraphQuery, GraphQueryResult
from agentic_sdlc_platform.ports.hermes_session import HermesSessionRequest, HermesSessionResponse


class FakeRepo:
    name = "erp-service"
    metadata_json = {}
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


class FakeRepository:
    async def get_repo_by_name(self, name: str):
        return FakeRepo() if name == "erp-service" else None

    async def find_task_by_external_id(self, external_id: str):
        return FakeTask() if external_id == "ENG-1284" else None


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
        json={"message": {"chat": {"id": 42}, "from": {"id": 7}, "text": "/implement ENG-1284"}},
    )

    assert response.status_code == 200
    assert response.json()["route"] == "multica_task"


def test_telegram_message_routes_repo_question_to_graph_store() -> None:
    graph_store = FakeGraphStore()
    client = TestClient(
        create_app(
            Settings(),
            repository=FakeRepository(),
            graph_store=graph_store,
        )
    )

    response = client.post(
        "/channels/telegram/webhook",
        json={
            "message": {
                "chat": {"id": -1001234567890},
                "from": {"id": 7},
                "text": "repo:erp-service Where does allocation live?",
            }
        },
    )

    assert response.status_code == 200
    assert response.json()["route"] == "graph_repo_query"
    assert response.json()["repo"] == "erp-service"
    assert response.json()["answer"] == "Allocation lives in inventory/allocation.py."
    assert response.json()["references"] == ["inventory/allocation.py"]
    assert graph_store.queries == [
        GraphQuery(
            repo="erp-service",
            question="Where does allocation live?",
            metadata={"default_branch": "main"},
        )
    ]


def test_telegram_task_status_command_returns_task_info() -> None:
    client = TestClient(create_app(Settings(), repository=FakeRepository()))

    response = client.post(
        "/channels/telegram/webhook",
        json={
            "message": {
                "chat": {"id": -1001234567890},
                "from": {"id": 7},
                "text": "/status ENG-1284",
            }
        },
    )

    assert response.status_code == 200
    assert response.json()["route"] == "task_info"
    assert response.json()["command"] == "status"
    assert response.json()["task_id"] == "task-1"
