from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from agentic_sdlc_platform.persistence.models import Base
from agentic_sdlc_platform.persistence.repository import PersistenceRepository


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


async def test_create_agent_session_persists_channel_and_hermes_ids() -> None:
    repository = await build_repository()
    task_id = await create_task(repository)

    session = await repository.create_agent_session(
        task_id=task_id,
        provider="linear",
        external_thread_id="issue-id-1",
        hermes_session_id="hermes-session-1",
        repo="erp-service",
    )

    assert session.task_id == task_id
    assert session.provider == "linear"
    assert session.external_thread_id == "issue-id-1"
    assert session.hermes_session_id == "hermes-session-1"
    assert session.status == "active"


async def test_find_agent_session_by_thread_and_record_events() -> None:
    repository = await build_repository()
    task_id = await create_task(repository)
    session = await repository.create_agent_session(
        task_id=task_id,
        provider="linear",
        external_thread_id="issue-id-1",
        hermes_session_id="hermes-session-1",
        repo="erp-service",
    )

    found = await repository.find_agent_session(provider="linear", external_thread_id="issue-id-1")
    event = await repository.record_session_event(
        session_id=session.id,
        direction="inbound",
        event_type="comment",
        actor="user-1",
        message="Please check inventory allocation first.",
        metadata={"comment_id": "comment-1"},
    )

    assert found is not None
    assert found.id == session.id
    assert event.session_id == session.id
    assert event.message == "Please check inventory allocation first."
    assert event.metadata_json == {"comment_id": "comment-1"}


async def test_list_orchestrator_backed_sessions_only_returns_active_backed_sessions() -> None:
    repository = await build_repository()
    task_id = await create_task(repository)
    backed = await repository.create_agent_session(
        task_id=task_id,
        provider="linear",
        external_thread_id="issue-id-1",
        hermes_session_id=None,
        repo="erp-service",
        orchestrator_provider="multica",
        orchestrator_issue_id="issue-1",
        orchestrator_task_id="task-1",
    )
    await repository.create_agent_session(
        task_id=task_id,
        provider="slack",
        external_thread_id="C123:1",
        hermes_session_id="hermes-session-1",
        repo="erp-service",
    )

    sessions = await repository.list_orchestrator_backed_agent_sessions()

    assert [session.id for session in sessions] == [backed.id]
    assert sessions[0].task is not None
