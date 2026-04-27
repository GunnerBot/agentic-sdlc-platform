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
        external_id="OS-1284",
        title="Build webhook bridge",
        repo="keychain-os-erp",
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
        repo="keychain-os-erp",
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
        repo="keychain-os-erp",
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
