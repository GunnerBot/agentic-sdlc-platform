from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from agentic_sdlc_platform.glue.conversation_sync import ConversationSyncService
from agentic_sdlc_platform.glue.dag_decomposer import Subtask
from agentic_sdlc_platform.persistence.models import AuditEvent, Base
from agentic_sdlc_platform.persistence.repository import PersistenceRepository
from agentic_sdlc_platform.ports.issue_tracker import IssueTrackerReply
from agentic_sdlc_platform.ports.task_orchestrator import (
    TaskConversationMessage,
    TaskReadRequest,
    TaskRequest,
    TaskResponse,
)


class FakeTaskOrchestrator:
    provider = "multica"

    def __init__(self) -> None:
        self.comments = [
            TaskConversationMessage(
                id="multica-comment-1",
                body="Agent found the answer.",
                actor="agent",
                metadata={"multica_issue_id": "issue-1"},
            )
        ]
        self.listed: list[tuple[str, dict[str, object]]] = []

    async def list_comments(self, external_task_id: str, metadata=None):
        self.listed.append((external_task_id, metadata or {}))
        return self.comments


class FakeDagTaskOrchestrator(FakeTaskOrchestrator):
    def __init__(self) -> None:
        super().__init__()
        self.reads: list[TaskReadRequest] = []
        self.requests: list[TaskRequest] = []

    async def read_task(self, request: TaskReadRequest) -> TaskResponse:
        self.reads.append(request)
        return TaskResponse(
            external_task_id=request.external_task_id,
            status="completed",
            metadata={
                "result_output": "Audit completed. No implementation changes were made.",
                "multica_runtime_provider": "hermes",
            },
        )

    async def create_task(self, request: TaskRequest) -> TaskResponse:
        self.requests.append(request)
        return TaskResponse(
            external_task_id="next-multica-task",
            status="queued",
            metadata={
                "multica_issue_id": "next-issue",
                "multica_task_id": "next-multica-task",
                "multica_runtime_provider": "hermes",
            },
        )


class FakeIssueTracker:
    def __init__(self) -> None:
        self.replies: list[IssueTrackerReply] = []

    async def reply(self, reply: IssueTrackerReply) -> None:
        self.replies.append(reply)


class FakeSlackClient:
    def __init__(self) -> None:
        self.replies: list[tuple[str, str, str]] = []

    async def post_thread_reply(self, channel: str, thread_ts: str, text: str):
        self.replies.append((channel, thread_ts, text))
        return "1710000001.000000"


class FailingSlackClient:
    async def post_thread_reply(self, channel: str, thread_ts: str, text: str):
        raise RuntimeError("slack unavailable")


class FakeTelegramClient:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []

    async def send_message(self, chat_id: str, text: str):
        self.messages.append((chat_id, text))
        return 42


async def build_repository() -> PersistenceRepository:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return PersistenceRepository(async_sessionmaker(engine, expire_on_commit=False))


async def create_task(repository: PersistenceRepository) -> str:
    event_result = await repository.record_inbound_event(
        source="linear",
        delivery_id="delivery-1",
        event_type="Issue",
        payload={"id": "issue-1"},
    )
    task = await repository.create_task_from_event(
        event_id=event_result.event.id,
        source="linear",
        external_id="ENG-1284",
        title="Build webhook bridge",
        repo="erp-service",
    )
    return task.id


async def test_conversation_sync_service_records_and_mirrors_new_comments_once() -> None:
    repository = await build_repository()
    task_id = await create_task(repository)
    session = await repository.create_agent_session(
        task_id=task_id,
        provider="linear",
        external_thread_id="issue-id-1",
        hermes_session_id=None,
        repo="erp-service",
        orchestrator_provider="multica",
        orchestrator_issue_id="issue-1",
        orchestrator_task_id="multica-task-1",
    )
    orchestrator = FakeTaskOrchestrator()
    issue_tracker = FakeIssueTracker()
    service = ConversationSyncService(
        repository=repository,
        task_orchestrator=orchestrator,
        issue_tracker=issue_tracker,
    )

    first = await service.sync_session(session.id)
    second = await service.sync_session(session.id)

    assert first.new_messages == 1
    assert second.new_messages == 0
    assert orchestrator.listed == [
        (
            "multica-task-1",
            {"multica_issue_id": "issue-1", "orchestrator_issue_id": "issue-1"},
        ),
        (
            "multica-task-1",
            {"multica_issue_id": "issue-1", "orchestrator_issue_id": "issue-1"},
        ),
    ]
    assert issue_tracker.replies == [
        IssueTrackerReply(issue_id="issue-id-1", body="Agent found the answer.")
    ]
    events = await repository.list_session_events(session.id)
    assert [(event.event_type, event.message) for event in events] == [
        ("orchestrator_reply", "Agent found the answer.")
    ]
    async with repository._session_factory() as db_session:
        audit = (
            await db_session.scalars(
                select(AuditEvent).where(AuditEvent.action == "agent_session.conversation_synced")
            )
        ).one()
    assert audit.metadata_json["new_messages"] == 1


async def test_conversation_sync_service_syncs_all_active_orchestrator_sessions() -> None:
    repository = await build_repository()
    task_id = await create_task(repository)
    await repository.create_agent_session(
        task_id=task_id,
        provider="linear",
        external_thread_id="issue-id-1",
        hermes_session_id=None,
        repo="erp-service",
        orchestrator_provider="multica",
        orchestrator_issue_id="issue-1",
        orchestrator_task_id="multica-task-1",
    )
    await repository.create_agent_session(
        task_id=task_id,
        provider="slack",
        external_thread_id="C123:1",
        hermes_session_id="hermes-session-1",
        repo="erp-service",
    )
    service = ConversationSyncService(
        repository=repository,
        task_orchestrator=FakeTaskOrchestrator(),
        issue_tracker=FakeIssueTracker(),
    )

    results = await service.sync_active_sessions()

    assert [(result.provider, result.new_messages) for result in results] == [("linear", 1)]


async def test_conversation_sync_service_advances_completed_dag_nodes() -> None:
    repository = await build_repository()
    task_id = await create_task(repository)
    dag = await repository.create_task_dag(
        task_id=task_id,
        subtasks=[
            Subtask(
                "audit_existing_flow",
                "Audit existing flow",
                repo="erp-service",
                metadata={"execution_mode": "planning_only"},
            ),
            Subtask(
                "implement_sync_client",
                "Implement sync client with tests",
                repo="erp-service",
                depends_on=("audit_existing_flow",),
            ),
        ],
    )
    await repository.mark_dag_node_orchestrated(
        dag_id=dag.id,
        node_key="audit_existing_flow",
        orchestrator_task_id="audit-task",
        orchestrator_status="queued",
        metadata={"execution_mode": "planning_only", "multica_issue_id": "issue-1"},
    )
    orchestrator = FakeDagTaskOrchestrator()
    service = ConversationSyncService(
        repository=repository,
        task_orchestrator=orchestrator,
    )

    results = await service.sync_active_dag_nodes()
    refreshed = await repository.get_task_dag(dag.id)
    nodes = {node.node_key: node for node in refreshed.nodes}

    assert [(result.node_key, result.status, result.queued_nodes) for result in results] == [
        ("audit_existing_flow", "completed", ("implement_sync_client",))
    ]
    assert nodes["audit_existing_flow"].status == "completed"
    assert nodes["implement_sync_client"].status == "queued"
    assert nodes["implement_sync_client"].orchestrator_task_id == "next-multica-task"
    assert orchestrator.requests[0].metadata["execution_mode"] == "write_pr"
    assert orchestrator.requests[0].metadata["execution_policy"]["github_write_enabled"] is True
    assert (
        orchestrator.requests[0]
        .metadata["expected_branch"]
        .startswith(f"agent/dag/eng-1284/{dag.id}/implement_sync_client")
    )


async def test_conversation_sync_service_mirrors_slack_thread_replies() -> None:
    repository = await build_repository()
    task_id = await create_task(repository)
    session = await repository.create_agent_session(
        task_id=task_id,
        provider="slack",
        external_thread_id="C123:1710000000.000000",
        hermes_session_id=None,
        repo="erp-service",
        orchestrator_provider="multica",
        orchestrator_issue_id="issue-1",
        orchestrator_task_id="multica-task-1",
    )
    slack_client = FakeSlackClient()
    service = ConversationSyncService(
        repository=repository,
        task_orchestrator=FakeTaskOrchestrator(),
        slack_client=slack_client,
    )

    result = await service.sync_session(session.id)

    assert result.new_messages == 1
    assert slack_client.replies == [("C123", "1710000000.000000", "Agent found the answer.")]


async def test_conversation_sync_service_mirrors_telegram_messages() -> None:
    repository = await build_repository()
    task_id = await create_task(repository)
    session = await repository.create_agent_session(
        task_id=task_id,
        provider="telegram",
        external_thread_id="-1001234567890",
        hermes_session_id=None,
        repo="erp-service",
        orchestrator_provider="multica",
        orchestrator_issue_id="issue-1",
        orchestrator_task_id="multica-task-1",
    )
    telegram_client = FakeTelegramClient()
    service = ConversationSyncService(
        repository=repository,
        task_orchestrator=FakeTaskOrchestrator(),
        telegram_client=telegram_client,
    )

    result = await service.sync_session(session.id)

    assert result.new_messages == 1
    assert telegram_client.messages == [("-1001234567890", "Agent found the answer.")]


async def test_conversation_sync_does_not_mark_comment_seen_when_channel_mirror_fails() -> None:
    repository = await build_repository()
    task_id = await create_task(repository)
    session = await repository.create_agent_session(
        task_id=task_id,
        provider="slack",
        external_thread_id="C123:1710000000.000000",
        hermes_session_id=None,
        repo="erp-service",
        orchestrator_provider="multica",
        orchestrator_issue_id="issue-1",
        orchestrator_task_id="multica-task-1",
    )
    service = ConversationSyncService(
        repository=repository,
        task_orchestrator=FakeTaskOrchestrator(),
        slack_client=FailingSlackClient(),
    )

    results = await service.sync_active_sessions()

    assert results == []
    assert await repository.list_session_events(session.id) == []
    async with repository._session_factory() as db_session:
        audit = (
            await db_session.scalars(
                select(AuditEvent).where(
                    AuditEvent.action == "agent_session.conversation_sync_failed"
                )
            )
        ).one()
    assert audit.metadata_json["error"] == "slack unavailable"
