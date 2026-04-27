from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from agentic_sdlc_platform.app import create_app
from agentic_sdlc_platform.core.config import Settings
from agentic_sdlc_platform.persistence.models import Base
from agentic_sdlc_platform.persistence.repository import PersistenceRepository
from agentic_sdlc_platform.ports.hermes_session import (
    HermesSessionResponse,
    HermesStartSessionRequest,
)
from agentic_sdlc_platform.ports.issue_tracker import IssueTrackerReply, IssueTrackerUpdate
from agentic_sdlc_platform.ports.task_orchestrator import TaskRequest, TaskResponse


class FakeTaskOrchestrator:
    provider = "multica"

    async def create_task(self, request: TaskRequest) -> TaskResponse:
        return TaskResponse(external_task_id="multica-task-1", status="queued")


class FakeIssueTracker:
    def __init__(self) -> None:
        self.updates: list[IssueTrackerUpdate] = []
        self.replies: list[IssueTrackerReply] = []

    async def mark_task_queued(self, update: IssueTrackerUpdate) -> None:
        self.updates.append(update)

    async def reply(self, reply: IssueTrackerReply) -> None:
        self.replies.append(reply)


class FakeHermesSession:
    def __init__(self) -> None:
        self.started: list[HermesStartSessionRequest] = []
        self.resumed: list[tuple[str, str, str]] = []

    async def start_session(self, request: HermesStartSessionRequest) -> HermesSessionResponse:
        self.started.append(request)
        return HermesSessionResponse(session_id="hermes-session-1", message_id="message-1")

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
            answer="I will check inventory allocation first.",
        )


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


async def test_linear_assigned_issue_starts_and_persists_hermes_session() -> None:
    repository = await build_repository()
    hermes_session = FakeHermesSession()
    client = TestClient(
        create_app(
            Settings(linear_agent_user_id="agent-user-1"),
            repository=repository,
            task_orchestrator=FakeTaskOrchestrator(),
            hermes_session=hermes_session,
            issue_tracker=FakeIssueTracker(),
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
                "description": "Create the bridge.",
                "assignee": {"id": "agent-user-1"},
                "labels": {"nodes": [{"name": "repo:keychain-os-erp"}]},
            },
        },
        headers={"Linear-Delivery": "delivery-linear-session-1"},
    )

    assert response.status_code == 202
    assert hermes_session.started == [
        HermesStartSessionRequest(
            task_id=response.json()["task_id"],
            provider="linear",
            external_thread_id="issue-id-1",
            text="Build webhook bridge\n\nCreate the bridge.",
            repo="keychain-os-erp",
        )
    ]
    persisted = await repository.find_agent_session(
        provider="linear",
        external_thread_id="issue-id-1",
    )
    assert persisted is not None
    assert persisted.hermes_session_id == "hermes-session-1"


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


async def test_linear_comment_resumes_session_and_replies_in_thread() -> None:
    repository = await build_repository()
    hermes_session = FakeHermesSession()
    issue_tracker = FakeIssueTracker()
    client = TestClient(
        create_app(
            Settings(linear_agent_user_id="agent-user-1"),
            repository=repository,
            task_orchestrator=FakeTaskOrchestrator(),
            hermes_session=hermes_session,
            issue_tracker=issue_tracker,
        )
    )

    assignment_response = client.post(
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
        headers={"Linear-Delivery": "delivery-linear-session-comment-1"},
    )

    response = client.post(
        "/webhooks/linear",
        json={
            "type": "Comment",
            "action": "create",
            "data": {
                "id": "comment-1",
                "body": "Please check inventory allocation first.",
                "user": {"id": "user-1"},
                "issue": {"id": "issue-id-1", "identifier": "OS-1284"},
            },
        },
        headers={"Linear-Delivery": "delivery-linear-session-comment-2"},
    )

    assert assignment_response.status_code == 202
    assert response.status_code == 202
    assert response.json()["task_id"] == assignment_response.json()["task_id"]
    assert hermes_session.resumed == [
        (
            "hermes-session-1",
            "Please check inventory allocation first.",
            "linear:user-1",
        )
    ]
    assert issue_tracker.replies == [
        IssueTrackerReply(
            issue_id="issue-id-1",
            body="I will check inventory allocation first.",
        )
    ]
    persisted = await repository.find_agent_session(
        provider="linear",
        external_thread_id="issue-id-1",
    )
    assert persisted is not None
    events = await repository.list_session_events(persisted.id)
    recorded_events = [
        (event.direction, event.event_type, event.actor, event.message) for event in events
    ]
    assert recorded_events == [
        ("outbound", "session_started", "system", "Build webhook bridge"),
        ("inbound", "comment", "linear:user-1", "Please check inventory allocation first."),
        ("outbound", "reply", "agent", "I will check inventory allocation first."),
    ]
