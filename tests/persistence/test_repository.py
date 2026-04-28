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


async def test_find_and_update_task_status_by_external_id() -> None:
    repository = await build_repository()
    event_result = await repository.record_inbound_event(
        source="linear",
        delivery_id="delivery-1",
        event_type="Issue",
        payload={"id": "issue-1"},
    )
    created = await repository.create_task_from_event(
        event_id=event_result.event.id,
        source="linear",
        external_id="OS-1284",
        title="Build webhook bridge",
        repo="keychain-os-erp",
    )

    found = await repository.find_task_by_external_id("OS-1284")
    updated = await repository.update_task_status(task_id=created.id, status="pr_open")

    assert found is not None
    assert found.id == created.id
    assert updated.status == "pr_open"


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


async def test_task_artifacts_are_persisted_and_filterable() -> None:
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

    artifact = await repository.create_task_artifact(
        task_id=task.id,
        kind="hydrated_spec",
        name="OS-1284:hydrated-spec",
        content={"text_sources": [{"title": "Linear description", "text": "Spec"}]},
        metadata={"asset_count": 0},
    )
    filtered = await repository.list_task_artifacts(task_id=task.id, kind="hydrated_spec")

    assert [item.id for item in filtered] == [artifact.id]
    assert filtered[0].content_json["text_sources"][0]["text"] == "Spec"
    assert filtered[0].metadata_json == {"asset_count": 0}
