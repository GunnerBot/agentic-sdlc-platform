from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from agentic_sdlc_platform.app import create_app
from agentic_sdlc_platform.core.config import Settings
from agentic_sdlc_platform.persistence.models import AuditEvent, Base, Task
from agentic_sdlc_platform.persistence.repository import PersistenceRepository
from agentic_sdlc_platform.ports.task_orchestrator import (
    TaskRequest,
    TaskResponse,
    TaskUpdateRequest,
)


class FakeTaskOrchestrator:
    provider = "multica"

    def __init__(self) -> None:
        self.requests: list[TaskRequest] = []
        self.updates: list[TaskUpdateRequest] = []

    async def create_task(self, request: TaskRequest) -> TaskResponse:
        self.requests.append(request)
        return TaskResponse(external_task_id="multica-task-1", status="queued")

    async def update_task(self, request: TaskUpdateRequest) -> TaskResponse:
        self.updates.append(request)
        return TaskResponse(external_task_id=request.external_task_id, status=request.status)


async def build_repository() -> PersistenceRepository:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return PersistenceRepository(async_sessionmaker(engine, expire_on_commit=False))


async def test_actionable_linear_webhook_creates_multica_task_when_configured() -> None:
    repository = await build_repository()
    await repository.upsert_repo(
        name="keychain-os-erp",
        provider="github",
        clone_url="https://github.com/atlas-tech-inc/keychain-os-erp.git",
        default_branch="main",
        metadata={},
    )
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
            metadata={
                "repo_provider": "github",
                "repo_clone_url": "https://github.com/atlas-tech-inc/keychain-os-erp.git",
                "repo_default_branch": "main",
                "repo_metadata": {},
            },
        )
    ]
    assert task.orchestrator_task_id == "multica-task-1"
    assert task.orchestrator_status == "queued"
    assert "task.orchestrated" in audit_actions


async def test_github_pull_request_webhook_updates_existing_multica_task() -> None:
    repository = await build_repository()
    inbound_event = await repository.record_inbound_event(
        source="linear",
        delivery_id="delivery-linear-1",
        event_type="Issue",
        payload={"id": "issue-1"},
    )
    task = await repository.create_task_from_event(
        event_id=inbound_event.event.id,
        source="linear",
        external_id="OS-1284",
        title="Build webhook bridge",
        repo="GunnerBot/agentic-sdlc-platform",
    )
    await repository.mark_task_orchestrated(
        task_id=task.id,
        orchestrator_task_id="multica-task-1",
        orchestrator_status="queued",
    )
    task_orchestrator = FakeTaskOrchestrator()
    client = TestClient(
        create_app(
            Settings(),
            repository=repository,
            task_orchestrator=task_orchestrator,
        )
    )

    response = client.post(
        "/webhooks/github",
        json={
            "action": "opened",
            "pull_request": {
                "number": 17,
                "title": "OS-1284 Build webhook bridge",
                "html_url": "https://github.com/GunnerBot/agentic-sdlc-platform/pull/17",
                "head": {"ref": "agent/OS-1284-build-webhook-bridge"},
                "body": "Implements OS-1284.",
                "merged": False,
            },
            "repository": {"full_name": "GunnerBot/agentic-sdlc-platform"},
        },
        headers={
            "X-GitHub-Event": "pull_request",
            "X-GitHub-Delivery": "delivery-pr-1",
        },
    )

    assert response.status_code == 202
    assert response.json()["task_id"] == task.id
    assert task_orchestrator.updates == [
        TaskUpdateRequest(
            external_task_id="multica-task-1",
            status="pr_open",
            metadata={
                "source": "github",
                "event_type": "pull_request",
                "external_id": "OS-1284",
                "pull_request": 17,
                "url": "https://github.com/GunnerBot/agentic-sdlc-platform/pull/17",
            },
        )
    ]
    async with repository._session_factory() as session:
        updated_task = (await session.scalars(select(Task))).one()
        audit_actions = {
            event.action for event in (await session.scalars(select(AuditEvent))).all()
        }

    assert updated_task.status == "pr_open"
    assert updated_task.orchestrator_status == "pr_open"
    assert "task.updated_from_github" in audit_actions
