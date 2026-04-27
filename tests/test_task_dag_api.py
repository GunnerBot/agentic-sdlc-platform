from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from agentic_sdlc_platform.app import create_app
from agentic_sdlc_platform.core.config import Settings
from agentic_sdlc_platform.persistence.models import Base, TaskDag
from agentic_sdlc_platform.persistence.repository import PersistenceRepository
from agentic_sdlc_platform.ports.model_provider import ModelRequest, ModelResponse
from agentic_sdlc_platform.ports.task_orchestrator import TaskRequest, TaskResponse


class FakePlannerModel:
    def __init__(self) -> None:
        self.requests: list[ModelRequest] = []

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        return ModelResponse(
            provider="fake",
            model="fake-model",
            content="""
[
  {"id": "api", "title": "Add API contract", "repo": "erp-api"},
  {"id": "web", "title": "Consume API", "repo": "erp-web", "depends_on": ["api"]}
]
""",
        )


class FakeTaskOrchestrator:
    provider = "multica"

    def __init__(self) -> None:
        self.requests: list[TaskRequest] = []

    async def create_task(self, request: TaskRequest) -> TaskResponse:
        self.requests.append(request)
        return TaskResponse(
            external_task_id=f"multica-{request.external_id}",
            status="queued",
        )


async def build_repository() -> PersistenceRepository:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return PersistenceRepository(async_sessionmaker(engine, expire_on_commit=False))


async def create_parent_task(repository: PersistenceRepository) -> str:
    event_result = await repository.record_inbound_event(
        source="linear",
        delivery_id="delivery-1",
        event_type="Issue",
        payload={"id": "issue-1"},
    )
    task = await repository.create_task_from_event(
        event_id=event_result.event.id,
        source="linear",
        external_id="OS-1284",
        title="Build agentic SDLC platform",
        repo="keychain-os-erp",
    )
    return task.id


async def test_create_task_dag_endpoint_persists_planner_output() -> None:
    repository = await build_repository()
    task_id = await create_parent_task(repository)
    model_provider = FakePlannerModel()
    client = TestClient(
        create_app(Settings(), repository=repository, model_provider=model_provider)
    )

    response = client.post(
        f"/tasks/{task_id}/dag",
        json={"spec_markdown": "# Feature\nBuild cross-repo workflow."},
    )

    assert response.status_code == 201
    assert response.json()["task_id"] == task_id
    assert [node["node_key"] for node in response.json()["nodes"]] == ["api", "web"]
    assert response.json()["nodes"][1]["depends_on"] == ["api"]
    assert model_provider.requests[0].role == "plan_agent"
    async with repository._session_factory() as session:
        dags = (await session.scalars(select(TaskDag))).all()

    assert len(dags) == 1


async def test_complete_dag_node_endpoint_returns_newly_ready_nodes() -> None:
    repository = await build_repository()
    task_id = await create_parent_task(repository)
    model_provider = FakePlannerModel()
    client = TestClient(
        create_app(Settings(), repository=repository, model_provider=model_provider)
    )
    created = client.post(
        f"/tasks/{task_id}/dag",
        json={"spec_markdown": "# Feature\nBuild cross-repo workflow."},
    )
    dag_id = created.json()["id"]

    response = client.post(f"/tasks/{task_id}/dag/{dag_id}/nodes/api/complete")

    assert response.status_code == 200
    assert response.json() == {
        "completed_node": "api",
        "ready_nodes": [
            {
                "node_key": "web",
                "title": "Consume API",
                "repo": "erp-web",
                "depends_on": ["api"],
                "status": "ready",
            }
        ],
    }


async def test_complete_dag_node_endpoint_enqueues_newly_ready_nodes() -> None:
    repository = await build_repository()
    task_id = await create_parent_task(repository)
    model_provider = FakePlannerModel()
    task_orchestrator = FakeTaskOrchestrator()
    client = TestClient(
        create_app(
            Settings(),
            repository=repository,
            model_provider=model_provider,
            task_orchestrator=task_orchestrator,
        )
    )
    created = client.post(
        f"/tasks/{task_id}/dag",
        json={"spec_markdown": "# Feature\nBuild cross-repo workflow."},
    )
    dag_id = created.json()["id"]

    response = client.post(
        f"/tasks/{task_id}/dag/{dag_id}/nodes/api/complete",
        params={"enqueue_ready": "true"},
    )

    assert response.status_code == 200
    assert response.json()["ready_nodes"][0]["status"] == "queued"
    assert task_orchestrator.requests == [
        TaskRequest(
            source="dag",
            external_id=f"{dag_id}:web",
            title="Consume API",
            repo="erp-web",
            inbound_event_id=None,
        )
    ]
