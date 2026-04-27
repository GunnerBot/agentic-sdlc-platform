from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import selectinload

from agentic_sdlc_platform.app import create_app
from agentic_sdlc_platform.core.config import Settings
from agentic_sdlc_platform.persistence.models import AuditEvent, Base, TaskDag
from agentic_sdlc_platform.persistence.repository import PersistenceRepository
from agentic_sdlc_platform.ports.hermes_session import (
    HermesSessionResponse,
    HermesStartSessionRequest,
)
from agentic_sdlc_platform.ports.issue_tracker import IssueTrackerReply, IssueTrackerUpdate
from agentic_sdlc_platform.ports.task_orchestrator import TaskRequest, TaskResponse


class FakeTaskOrchestrator:
    provider = "multica"

    def __init__(self) -> None:
        self.created: list[TaskRequest] = []
        self.updated: list[tuple[str, str]] = []

    async def create_task(self, request: TaskRequest) -> TaskResponse:
        self.created.append(request)
        return TaskResponse(external_task_id="multica-task-1", status="queued")

    async def update_task(self, request) -> TaskResponse:
        self.updated.append((request.external_task_id, request.status))
        return TaskResponse(external_task_id=request.external_task_id, status=request.status)


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
    await repository.upsert_repo(
        name="keychain-os-erp",
        provider="github",
        clone_url="https://github.com/atlas-tech-inc/keychain-os-erp.git",
        default_branch="main",
        metadata={},
    )
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


async def test_linear_assigned_issue_uses_registered_repo_metadata() -> None:
    repository = await build_repository()
    await repository.upsert_repo(
        name="keychain-os-erp",
        provider="github",
        clone_url="https://github.com/atlas-tech-inc/keychain-os-erp.git",
        default_branch="develop",
        metadata={"linear_team_key": "OS"},
    )
    task_orchestrator = FakeTaskOrchestrator()
    client = TestClient(
        create_app(
            Settings(linear_agent_user_id="agent-user-1"),
            repository=repository,
            task_orchestrator=task_orchestrator,
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
                "assignee": {"id": "agent-user-1"},
                "labels": {"nodes": [{"name": "repo:keychain-os-erp"}]},
            },
        },
        headers={"Linear-Delivery": "delivery-linear-registered-repo-1"},
    )

    assert response.status_code == 202
    assert task_orchestrator.created == [
        TaskRequest(
            source="linear",
            external_id="OS-1284",
            title="Build webhook bridge",
            repo="keychain-os-erp",
            inbound_event_id=task_orchestrator.created[0].inbound_event_id,
            metadata={
                "repo_provider": "github",
                "repo_clone_url": "https://github.com/atlas-tech-inc/keychain-os-erp.git",
                "repo_default_branch": "develop",
                "repo_metadata": {"linear_team_key": "OS"},
            },
        )
    ]


async def test_linear_assigned_issue_with_type_label_creates_dag_template() -> None:
    repository = await build_repository()
    await repository.upsert_repo(
        name="keychain-os-erp",
        provider="github",
        clone_url="https://github.com/atlas-tech-inc/keychain-os-erp.git",
        default_branch="main",
        metadata={},
    )
    client = TestClient(
        create_app(
            Settings(linear_agent_user_id="agent-user-1"),
            repository=repository,
            task_orchestrator=FakeTaskOrchestrator(),
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
                "assignee": {"id": "agent-user-1"},
                "labels": {
                    "nodes": [
                        {"name": "repo:keychain-os-erp"},
                        {"name": "type:bug"},
                    ]
                },
            },
        },
        headers={"Linear-Delivery": "delivery-linear-dag-template-1"},
    )

    assert response.status_code == 202
    async with repository._session_factory() as session:
        dag = (
            await session.scalars(select(TaskDag).options(selectinload(TaskDag.nodes)))
        ).one()
        audit_event = (
            await session.scalars(
                select(AuditEvent).where(AuditEvent.action == "task.dag_template_created")
            )
        ).one()

        assert [node.node_key for node in dag.nodes] == ["reproduce", "fix", "test", "review"]
        assert [node.repo for node in dag.nodes] == ["keychain-os-erp"] * 4
        assert dag.nodes[0].status == "ready"
        assert dag.nodes[1].status == "blocked"
    assert audit_event.metadata_json["template"] == "bugfix"
    assert audit_event.metadata_json["node_count"] == 4


async def test_linear_assigned_issue_blocks_when_repo_label_is_unknown() -> None:
    repository = await build_repository()
    task_orchestrator = FakeTaskOrchestrator()
    issue_tracker = FakeIssueTracker()
    client = TestClient(
        create_app(
            Settings(linear_agent_user_id="agent-user-1"),
            repository=repository,
            task_orchestrator=task_orchestrator,
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
                "labels": {"nodes": [{"name": "repo:missing-repo"}]},
            },
        },
        headers={"Linear-Delivery": "delivery-linear-unknown-repo-1"},
    )

    assert response.status_code == 202
    assert response.json()["task_id"] is not None
    assert task_orchestrator.created == []
    assert issue_tracker.updates == []
    assert issue_tracker.replies == [
        IssueTrackerReply(
            issue_id="issue-id-1",
            body=(
                "Repository missing-repo is not registered. "
                "Register it before I can work on OS-1284."
            ),
        )
    ]
    task = await repository.find_task_by_external_id("OS-1284")
    assert task is not None
    assert task.status == "blocked"


async def test_linear_assigned_issue_starts_and_persists_hermes_session() -> None:
    repository = await build_repository()
    await repository.upsert_repo(
        name="keychain-os-erp",
        provider="github",
        clone_url="https://github.com/atlas-tech-inc/keychain-os-erp.git",
        default_branch="main",
        metadata={},
    )
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
    await repository.upsert_repo(
        name="keychain-os-erp",
        provider="github",
        clone_url="https://github.com/atlas-tech-inc/keychain-os-erp.git",
        default_branch="main",
        metadata={},
    )
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


async def test_linear_comment_from_agent_user_is_ignored_to_prevent_reply_loop() -> None:
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
            },
        },
        headers={"Linear-Delivery": "delivery-linear-loop-1"},
    )
    response = client.post(
        "/webhooks/linear",
        json={
            "type": "Comment",
            "action": "create",
            "data": {
                "id": "comment-1",
                "body": "I will check inventory allocation first.",
                "user": {"id": "agent-user-1"},
                "issue": {"id": "issue-id-1", "identifier": "OS-1284"},
            },
        },
        headers={"Linear-Delivery": "delivery-linear-loop-2"},
    )

    assert assignment_response.status_code == 202
    assert response.status_code == 202
    assert response.json()["task_id"] == assignment_response.json()["task_id"]
    assert hermes_session.resumed == []
    assert issue_tracker.replies == []


async def test_linear_comment_command_updates_task_status_and_replies() -> None:
    repository = await build_repository()
    task_orchestrator = FakeTaskOrchestrator()
    issue_tracker = FakeIssueTracker()
    client = TestClient(
        create_app(
            Settings(linear_agent_user_id="agent-user-1"),
            repository=repository,
            task_orchestrator=task_orchestrator,
            hermes_session=FakeHermesSession(),
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
            },
        },
        headers={"Linear-Delivery": "delivery-linear-command-1"},
    )
    response = client.post(
        "/webhooks/linear",
        json={
            "type": "Comment",
            "action": "create",
            "data": {
                "id": "comment-1",
                "body": "/pause OS-1284 waiting for product decision",
                "user": {"id": "user-1"},
                "issue": {"id": "issue-id-1", "identifier": "OS-1284"},
            },
        },
        headers={"Linear-Delivery": "delivery-linear-command-2"},
    )

    assert assignment_response.status_code == 202
    assert response.status_code == 202
    assert response.json()["task_id"] == assignment_response.json()["task_id"]
    assert task_orchestrator.updated == [("multica-task-1", "paused")]
    assert issue_tracker.replies[-1] == IssueTrackerReply(
        issue_id="issue-id-1",
        body="Command /pause applied. Task OS-1284 is now paused.",
    )
    task = await repository.find_task_by_external_id("OS-1284")
    assert task is not None
    assert task.status == "paused"


async def test_linear_status_comment_replies_with_current_task_state() -> None:
    repository = await build_repository()
    issue_tracker = FakeIssueTracker()
    client = TestClient(
        create_app(
            Settings(linear_agent_user_id="agent-user-1"),
            repository=repository,
            task_orchestrator=FakeTaskOrchestrator(),
            hermes_session=FakeHermesSession(),
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
            },
        },
        headers={"Linear-Delivery": "delivery-linear-status-1"},
    )
    response = client.post(
        "/webhooks/linear",
        json={
            "type": "Comment",
            "action": "create",
            "data": {
                "id": "comment-1",
                "body": "/status OS-1284",
                "user": {"id": "user-1"},
                "issue": {"id": "issue-id-1", "identifier": "OS-1284"},
            },
        },
        headers={"Linear-Delivery": "delivery-linear-status-2"},
    )

    assert assignment_response.status_code == 202
    assert response.status_code == 202
    assert response.json()["task_id"] == assignment_response.json()["task_id"]
    assert issue_tracker.replies[-1] == IssueTrackerReply(
        issue_id="issue-id-1",
        body=(
            "Task OS-1284 status: queued. "
            "Orchestrator: multica-task-1 (queued). "
            "Repo: none. Sessions: 1 active session. "
            "DAG: none."
        ),
    )


async def test_linear_status_comment_replies_with_dag_progress() -> None:
    repository = await build_repository()
    await repository.upsert_repo(
        name="keychain-os-erp",
        provider="github",
        clone_url="https://github.com/atlas-tech-inc/keychain-os-erp.git",
        default_branch="main",
        metadata={},
    )
    issue_tracker = FakeIssueTracker()
    client = TestClient(
        create_app(
            Settings(linear_agent_user_id="agent-user-1"),
            repository=repository,
            task_orchestrator=FakeTaskOrchestrator(),
            hermes_session=FakeHermesSession(),
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
                "labels": {
                    "nodes": [
                        {"name": "repo:keychain-os-erp"},
                        {"name": "type:feature"},
                    ]
                },
            },
        },
        headers={"Linear-Delivery": "delivery-linear-status-dag-1"},
    )
    response = client.post(
        "/webhooks/linear",
        json={
            "type": "Comment",
            "action": "create",
            "data": {
                "id": "comment-1",
                "body": "/status OS-1284",
                "user": {"id": "user-1"},
                "issue": {"id": "issue-id-1", "identifier": "OS-1284"},
            },
        },
        headers={"Linear-Delivery": "delivery-linear-status-dag-2"},
    )

    assert assignment_response.status_code == 202
    assert response.status_code == 202
    assert response.json()["task_id"] == assignment_response.json()["task_id"]
    assert issue_tracker.replies[-1] == IssueTrackerReply(
        issue_id="issue-id-1",
        body=(
            "Task OS-1284 status: queued. "
            "Orchestrator: multica-task-1 (queued). "
            "Repo: keychain-os-erp. Sessions: 1 active session. "
            "DAG: planned, 0/5 completed, 1 ready, next: design."
        ),
    )


async def test_linear_context_comment_replies_with_repo_and_recent_events() -> None:
    repository = await build_repository()
    await repository.upsert_repo(
        name="keychain-os-erp",
        provider="github",
        clone_url="https://github.com/atlas-tech-inc/keychain-os-erp.git",
        default_branch="main",
        metadata={"owner": "platform"},
    )
    issue_tracker = FakeIssueTracker()
    client = TestClient(
        create_app(
            Settings(linear_agent_user_id="agent-user-1"),
            repository=repository,
            task_orchestrator=FakeTaskOrchestrator(),
            hermes_session=FakeHermesSession(),
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
        headers={"Linear-Delivery": "delivery-linear-context-1"},
    )
    client.post(
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
        headers={"Linear-Delivery": "delivery-linear-context-2"},
    )
    response = client.post(
        "/webhooks/linear",
        json={
            "type": "Comment",
            "action": "create",
            "data": {
                "id": "comment-2",
                "body": "/context OS-1284",
                "user": {"id": "user-1"},
                "issue": {"id": "issue-id-1", "identifier": "OS-1284"},
            },
        },
        headers={"Linear-Delivery": "delivery-linear-context-3"},
    )

    assert assignment_response.status_code == 202
    assert response.status_code == 202
    assert response.json()["task_id"] == assignment_response.json()["task_id"]
    assert issue_tracker.replies[-1] == IssueTrackerReply(
        issue_id="issue-id-1",
        body=(
            "Task OS-1284 context:\n"
            "Repo: keychain-os-erp (github, main)\n"
            "Recent events:\n"
            "- system session_started: Build webhook bridge\n"
            "- linear:user-1 comment: Please check inventory allocation first.\n"
            "- agent reply: I will check inventory allocation first."
        ),
    )


async def test_linear_agents_comment_replies_with_session_and_orchestrator_state() -> None:
    repository = await build_repository()
    issue_tracker = FakeIssueTracker()
    client = TestClient(
        create_app(
            Settings(linear_agent_user_id="agent-user-1"),
            repository=repository,
            task_orchestrator=FakeTaskOrchestrator(),
            hermes_session=FakeHermesSession(),
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
            },
        },
        headers={"Linear-Delivery": "delivery-linear-agents-1"},
    )
    response = client.post(
        "/webhooks/linear",
        json={
            "type": "Comment",
            "action": "create",
            "data": {
                "id": "comment-1",
                "body": "/agents OS-1284",
                "user": {"id": "user-1"},
                "issue": {"id": "issue-id-1", "identifier": "OS-1284"},
            },
        },
        headers={"Linear-Delivery": "delivery-linear-agents-2"},
    )

    assert assignment_response.status_code == 202
    assert response.status_code == 202
    assert response.json()["task_id"] == assignment_response.json()["task_id"]
    reply = issue_tracker.replies[-1]
    assert reply.issue_id == "issue-id-1"
    assert reply.body.startswith(
        "Task OS-1284 agents:\n"
        "Orchestrator: multica-task-1 (queued)\n"
        "- linear session "
    )
    assert ": status active, repo none, hermes hermes-session-1, events 2" in reply.body
