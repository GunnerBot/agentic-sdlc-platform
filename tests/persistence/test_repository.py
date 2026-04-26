from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from agentic_sdlc_platform.persistence.models import Base
from agentic_sdlc_platform.persistence.repository import PersistenceRepository


async def build_repository() -> PersistenceRepository:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return PersistenceRepository(async_sessionmaker(engine, expire_on_commit=False))


async def test_record_inbound_event_is_idempotent_by_source_and_delivery_id() -> None:
    repository = await build_repository()

    first = await repository.record_inbound_event(
        source="linear",
        delivery_id="delivery-1",
        event_type="Issue",
        payload={"id": "issue-1"},
    )
    second = await repository.record_inbound_event(
        source="linear",
        delivery_id="delivery-1",
        event_type="Issue",
        payload={"id": "issue-1"},
    )

    assert first.created is True
    assert second.created is False
    assert second.event.id == first.event.id


async def test_create_task_from_event_persists_task_context() -> None:
    repository = await build_repository()
    event_result = await repository.record_inbound_event(
        source="linear",
        delivery_id="delivery-1",
        event_type="Issue",
        payload={"id": "issue-1"},
    )

    task = await repository.create_task_from_event(
        event_id=event_result.event.id,
        source="linear",
        external_id="issue-1",
        title="Build webhook bridge",
        repo="GunnerBot/agentic-sdlc-platform",
    )

    assert task.source == "linear"
    assert task.external_id == "issue-1"
    assert task.repo == "GunnerBot/agentic-sdlc-platform"
    assert task.inbound_event_id == event_result.event.id


async def test_mark_task_orchestrated_persists_external_task_state() -> None:
    repository = await build_repository()
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

    updated = await repository.mark_task_orchestrated(
        task_id=task.id,
        orchestrator_task_id="multica-task-1",
        orchestrator_status="queued",
    )

    assert updated.orchestrator_task_id == "multica-task-1"
    assert updated.orchestrator_status == "queued"


async def test_record_audit_event_persists_action_metadata() -> None:
    repository = await build_repository()

    audit_event = await repository.record_audit_event(
        action="webhook.accepted",
        actor="system",
        target_type="inbound_event",
        target_id="event-1",
        metadata={"source": "linear"},
    )

    assert audit_event.action == "webhook.accepted"
    assert audit_event.actor == "system"
    assert audit_event.metadata_json == {"source": "linear"}
