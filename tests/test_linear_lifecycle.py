from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from agentic_sdlc_platform.app import create_app
from agentic_sdlc_platform.core.config import Settings
from agentic_sdlc_platform.persistence.models import Base
from agentic_sdlc_platform.persistence.repository import PersistenceRepository
from agentic_sdlc_platform.ports.issue_tracker import IssueTrackerUpdate
from agentic_sdlc_platform.ports.task_orchestrator import TaskRequest, TaskResponse


class FakeTaskOrchestrator:
    provider = "multica"

    async def create_task(self, request: TaskRequest) -> TaskResponse:
        return TaskResponse(external_task_id="multica-task-1", status="queued")


class FakeIssueTracker:
    def __init__(self) -> None:
        self.updates: list[IssueTrackerUpdate] = []

    async def mark_task_queued(self, update: IssueTrackerUpdate) -> None:
        self.updates.append(update)


async def build_repository() -> PersistenceRepository:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return PersistenceRepository(async_sessionmaker(engine, expire_on_commit=False))


async def test_linear_assigned_issue_comments_when_agent_task_is_queued() -> None:
    repository = await build_repository()
    issue_tracker = FakeIssueTracker()
    client = TestClient(
        create_app(
            Settings(linear_agent_user_id="agent-user-1"),
            repository=repository,
            task_orchestrator=FakeTaskOrchestrator(),
            issue_tracker=issue_tracker,
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
                "assignee": {"id": "agent-user-1"},
                "labels": {"nodes": [{"name": "repo:keychain-os-erp"}]},
            },
        },
        headers={"Linear-Delivery": "delivery-linear-lifecycle-1"},
    )

    assert response.status_code == 202
    assert response.json()["task_id"] is not None
    assert issue_tracker.updates == [
        IssueTrackerUpdate(
            issue_id="issue-id-1",
            external_id="OS-1284",
            internal_task_id=response.json()["task_id"],
            orchestrator_task_id="multica-task-1",
        )
    ]


async def test_linear_issue_assigned_to_other_user_is_not_actionable() -> None:
    repository = await build_repository()
    issue_tracker = FakeIssueTracker()
    client = TestClient(
        create_app(
            Settings(linear_agent_user_id="agent-user-1"),
            repository=repository,
            task_orchestrator=FakeTaskOrchestrator(),
            issue_tracker=issue_tracker,
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
                "assignee": {"id": "someone-else"},
            },
        },
        headers={"Linear-Delivery": "delivery-linear-lifecycle-2"},
    )

    assert response.status_code == 202
    assert response.json()["task_id"] is None
    assert issue_tracker.updates == []
