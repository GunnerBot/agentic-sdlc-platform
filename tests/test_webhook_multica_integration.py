from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from agentic_sdlc_platform.app import create_app
from agentic_sdlc_platform.core.config import Settings
from agentic_sdlc_platform.persistence.models import AuditEvent, Base, Task
from agentic_sdlc_platform.persistence.repository import PersistenceRepository
from agentic_sdlc_platform.ports.task_orchestrator import TaskRequest, TaskResponse


class FakeTaskOrchestrator:
    provider = "multica"

    def __init__(self) -> None:
        self.requests: list[TaskRequest] = []

    async def create_task(self, request: TaskRequest) -> TaskResponse:
        self.requests.append(request)
        return TaskResponse(external_task_id="multica-task-1", status="queued")


async def build_repository() -> PersistenceRepository:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return PersistenceRepository(async_sessionmaker(engine, expire_on_commit=False))


async def test_actionable_linear_webhook_creates_multica_task_when_configured() -> None:
    repository = await build_repository()
    task_orchestrator = FakeTaskOrchestrator()
    client = TestClient(
        create_app(
            Settings(),
            repository=repository,
            task_orchestrator=task_orchestrator,
        )
    )

    response = client.post(
        "/webhooks/linear",
        json={
            "type": "Issue",
            "action": "update",
            "data": {
                "id": "issue-id-1",
                "identifier": "OS-1284",
                "title": "Build webhook bridge",
                "labels": {"nodes": [{"name": "repo:keychain-os-erp"}]},
            },
        },
        headers={"Linear-Delivery": "delivery-multica-1"},
    )

    assert response.status_code == 202
    assert response.json()["task_id"] is not None
    async with repository._session_factory() as session:
        task = (await session.scalars(select(Task))).one()
        audit_actions = {
            event.action for event in (await session.scalars(select(AuditEvent))).all()
        }

    assert task_orchestrator.requests == [
        TaskRequest(
            source="linear",
            external_id="OS-1284",
            title="Build webhook bridge",
            repo="keychain-os-erp",
            inbound_event_id=task.inbound_event_id,
        )
    ]
    assert task.orchestrator_task_id == "multica-task-1"
    assert task.orchestrator_status == "queued"
    assert "task.orchestrated" in audit_actions
