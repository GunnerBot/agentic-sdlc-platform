import hmac
import json
import time
from hashlib import sha256

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from agentic_sdlc_platform.app import create_app
from agentic_sdlc_platform.core.config import Settings
from agentic_sdlc_platform.persistence.models import AuditEvent, Base, Task
from agentic_sdlc_platform.persistence.repository import PersistenceRepository
from agentic_sdlc_platform.ports.task_orchestrator import TaskResponse, TaskUpdateRequest


class FakeTaskOrchestrator:
    provider = "multica"

    def __init__(self) -> None:
        self.updates: list[TaskUpdateRequest] = []

    async def update_task(self, request: TaskUpdateRequest) -> TaskResponse:
        self.updates.append(request)
        return TaskResponse(external_task_id=request.external_task_id, status=request.status)


async def build_repository() -> PersistenceRepository:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return PersistenceRepository(async_sessionmaker(engine, expire_on_commit=False))


async def create_task(repository: PersistenceRepository) -> Task:
    event_result = await repository.record_inbound_event(
        source="linear",
        delivery_id="delivery-1",
        event_type="Issue",
        payload={"id": "issue-1"},
    )
    return await repository.create_task_from_event(
        event_id=event_result.event.id,
        source="linear",
        external_id="OS-1284",
        title="Build webhook bridge",
        repo="keychain-os-erp",
    )


def signed_slack_headers(body: bytes, secret: str) -> dict[str, str]:
    timestamp = str(int(time.time()))
    base = b"v0:" + timestamp.encode("utf-8") + b":" + body
    digest = hmac.new(secret.encode("utf-8"), base, sha256).hexdigest()
    return {
        "X-Slack-Request-Timestamp": timestamp,
        "X-Slack-Signature": f"v0={digest}",
        "Content-Type": "application/json",
    }


async def test_pause_command_updates_task_status_and_audit_log() -> None:
    repository = await build_repository()
    task = await create_task(repository)
    client = TestClient(create_app(Settings(), repository=repository))

    response = client.post(
        "/channels/messages",
        json={
            "provider": "slack",
            "channel": "C123",
            "sender_id": "U123",
            "text": "/pause OS-1284 waiting for product clarification",
        },
    )

    assert response.status_code == 202
    assert response.json()["route"] == "human_override"
    assert response.json()["task_id"] == task.id
    assert response.json()["command"] == "pause"
    async with repository._session_factory() as session:
        updated_task = (await session.scalars(select(Task))).one()
        audit_event = (await session.scalars(select(AuditEvent))).one()

    assert updated_task.status == "paused"
    assert audit_event.action == "human_override.pause"
    assert audit_event.actor == "U123"
    assert audit_event.metadata_json["reason"] == "waiting for product clarification"


async def test_resume_command_updates_multica_task_when_orchestrated() -> None:
    repository = await build_repository()
    task = await create_task(repository)
    await repository.mark_task_orchestrated(
        task_id=task.id,
        orchestrator_task_id="multica-task-1",
        orchestrator_status="paused",
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
        "/channels/messages",
        json={
            "provider": "telegram",
            "channel": "-1001234567890",
            "sender_id": "7",
            "text": "/resume OS-1284",
        },
    )

    assert response.status_code == 202
    assert response.json()["command"] == "resume"
    assert task_orchestrator.updates == [
        TaskUpdateRequest(
            external_task_id="multica-task-1",
            status="queued",
            metadata={
                "command": "resume",
                "actor": "7",
                "channel": "-1001234567890",
                "reason": None,
            },
        )
    ]
    async with repository._session_factory() as session:
        updated_task = (await session.scalars(select(Task))).one()

    assert updated_task.status == "queued"
    assert updated_task.orchestrator_status == "queued"


async def test_override_command_for_unknown_task_returns_404() -> None:
    repository = await build_repository()
    client = TestClient(create_app(Settings(), repository=repository))

    response = client.post(
        "/channels/messages",
        json={
            "provider": "slack",
            "channel": "C123",
            "sender_id": "U123",
            "text": "/reject OS-999 not valid",
        },
    )

    assert response.status_code == 404


async def test_slack_override_command_updates_task_status() -> None:
    repository = await build_repository()
    task = await create_task(repository)
    body = json.dumps(
        {
            "type": "event_callback",
            "event": {
                "type": "app_mention",
                "channel": "C123",
                "user": "U123",
                "text": "<@BOT> /takeover OS-1284 handling manually",
            },
        }
    ).encode("utf-8")
    client = TestClient(
        create_app(
            Settings(slack_signing_secret="secret"),
            repository=repository,
        )
    )

    response = client.post(
        "/channels/slack/events",
        content=body,
        headers=signed_slack_headers(body, "secret"),
    )

    assert response.status_code == 200
    assert response.json()["route"] == "human_override"
    assert response.json()["task_id"] == task.id
    async with repository._session_factory() as session:
        updated_task = (await session.scalars(select(Task))).one()

    assert updated_task.status == "human_takeover"


async def test_telegram_override_command_updates_task_status() -> None:
    repository = await build_repository()
    await create_task(repository)
    client = TestClient(
        create_app(
            Settings(telegram_secret_token="secret"),
            repository=repository,
        )
    )

    response = client.post(
        "/channels/telegram/webhook",
        json={
            "message": {
                "chat": {"id": -1001234567890},
                "from": {"id": 7},
                "text": "/context OS-1284 need more details",
            }
        },
        headers={"X-Telegram-Bot-Api-Secret-Token": "secret"},
    )

    assert response.status_code == 200
    assert response.json()["route"] == "human_override"
    assert response.json()["command"] == "context"
    async with repository._session_factory() as session:
        updated_task = (await session.scalars(select(Task))).one()

    assert updated_task.status == "context_requested"
