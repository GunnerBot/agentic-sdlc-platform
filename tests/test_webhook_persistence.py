from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from agentic_sdlc_platform.app import create_app
from agentic_sdlc_platform.core.config import Settings
from agentic_sdlc_platform.persistence.models import AuditEvent, Base, InboundEvent
from agentic_sdlc_platform.persistence.repository import PersistenceRepository


async def build_repository() -> PersistenceRepository:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return PersistenceRepository(async_sessionmaker(engine, expire_on_commit=False))


async def test_linear_webhook_persists_inbound_event_and_audit_event() -> None:
    repository = await build_repository()
    client = TestClient(create_app(Settings(), repository=repository))

    response = client.post(
        "/webhooks/linear",
        content=b'{"type":"Issue","data":{"id":"issue-1"}}',
        headers={"Linear-Delivery": "delivery-1"},
    )

    assert response.status_code == 202
    assert response.json() == {
        "accepted": True,
        "source": "linear",
        "task_id": None,
        "delivery_id": "delivery-1",
        "duplicate": False,
    }
    async with repository._session_factory() as session:
        inbound_events = (await session.scalars(select(InboundEvent))).all()
        audit_events = (await session.scalars(select(AuditEvent))).all()

    assert len(inbound_events) == 1
    assert inbound_events[0].source == "linear"
    assert inbound_events[0].delivery_id == "delivery-1"
    assert inbound_events[0].event_type == "Issue"
    assert inbound_events[0].payload_json["data"] == {"id": "issue-1"}
    assert len(audit_events) == 1
    assert audit_events[0].action == "webhook.accepted"
    assert audit_events[0].target_id == inbound_events[0].id


async def test_linear_webhook_duplicate_delivery_is_idempotent() -> None:
    repository = await build_repository()
    client = TestClient(create_app(Settings(), repository=repository))
    headers = {"Linear-Delivery": "delivery-1"}

    first = client.post("/webhooks/linear", content=b'{"type":"Issue"}', headers=headers)
    second = client.post("/webhooks/linear", content=b'{"type":"Issue"}', headers=headers)

    assert first.status_code == 202
    assert second.status_code == 202
    assert first.json()["duplicate"] is False
    assert second.json()["duplicate"] is True


async def test_github_webhook_persists_delivery_id() -> None:
    repository = await build_repository()
    client = TestClient(create_app(Settings(), repository=repository))

    response = client.post(
        "/webhooks/github",
        content=b'{"action":"opened"}',
        headers={
            "X-GitHub-Event": "pull_request",
            "X-GitHub-Delivery": "delivery-2",
        },
    )

    assert response.status_code == 202
    assert response.json()["source"] == "github:pull_request"
    assert response.json()["delivery_id"] == "delivery-2"
    assert response.json()["duplicate"] is False
