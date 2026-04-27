import json

import httpx
import pytest

from agentic_sdlc_platform.adapters.multica import MulticaTaskOrchestrator
from agentic_sdlc_platform.core.config import Settings
from agentic_sdlc_platform.ports.task_orchestrator import (
    TaskOrchestratorError,
    TaskRequest,
    TaskUpdateRequest,
)


async def test_multica_adapter_blocks_when_http_disabled() -> None:
    orchestrator = MulticaTaskOrchestrator(Settings(multica_http_enabled=False))

    with pytest.raises(TaskOrchestratorError, match="multica HTTP is disabled"):
        await orchestrator.create_task(
            TaskRequest(
                source="linear",
                external_id="OS-1284",
                title="Build webhook bridge",
                repo="keychain-os-erp",
            )
        )


async def test_multica_adapter_posts_internal_task_request() -> None:
    captured_request: httpx.Request | None = None

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal captured_request
        captured_request = request
        return httpx.Response(
            status_code=201,
            json={"id": "multica-task-1", "status": "queued"},
        )

    orchestrator = MulticaTaskOrchestrator(
        Settings(
            multica_http_enabled=True,
            multica_base_url="https://multica.local",
            multica_api_key="test-key",
        ),
        transport=httpx.MockTransport(handler),
    )

    response = await orchestrator.create_task(
        TaskRequest(
            source="linear",
            external_id="OS-1284",
            title="Build webhook bridge",
            repo="keychain-os-erp",
            inbound_event_id="event-1",
        )
    )

    assert response.external_task_id == "multica-task-1"
    assert response.status == "queued"
    assert captured_request is not None
    assert str(captured_request.url) == "https://multica.local/api/tasks"
    assert captured_request.headers["authorization"] == "Bearer test-key"
    assert captured_request.headers["content-type"] == "application/json"
    assert json.loads(captured_request.content) == {
        "source": "linear",
        "external_id": "OS-1284",
        "title": "Build webhook bridge",
        "repo": "keychain-os-erp",
        "inbound_event_id": "event-1",
        "metadata": {},
    }


async def test_multica_adapter_raises_structured_error_for_api_failure() -> None:
    orchestrator = MulticaTaskOrchestrator(
        Settings(
            multica_http_enabled=True,
            multica_base_url="https://multica.local",
            multica_api_key="test-key",
        ),
        transport=httpx.MockTransport(
            lambda request: httpx.Response(status_code=500, json={"error": "boom"})
        ),
    )

    with pytest.raises(TaskOrchestratorError, match="multica create_task failed"):
        await orchestrator.create_task(
            TaskRequest(
                source="github",
                external_id="GunnerBot/agentic-sdlc-platform#42",
                title="Add channel router",
                repo="GunnerBot/agentic-sdlc-platform",
            )
        )


async def test_multica_adapter_retries_transient_create_task_failure() -> None:
    attempts = 0
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            return httpx.Response(status_code=503, json={"error": "try again"})
        return httpx.Response(status_code=201, json={"id": "multica-task-1", "status": "queued"})

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    orchestrator = MulticaTaskOrchestrator(
        Settings(
            multica_http_enabled=True,
            multica_base_url="https://multica.local",
            multica_api_key="test-key",
            multica_max_retries=2,
            multica_retry_backoff_seconds=0.25,
        ),
        transport=httpx.MockTransport(handler),
        sleep=sleep,
    )

    response = await orchestrator.create_task(
        TaskRequest(
            source="linear",
            external_id="OS-1284",
            title="Build webhook bridge",
            repo="keychain-os-erp",
        )
    )

    assert response.external_task_id == "multica-task-1"
    assert attempts == 3
    assert sleeps == [0.25, 0.5]


async def test_multica_adapter_does_not_retry_permanent_create_task_failure() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(status_code=400, json={"error": "bad request"})

    orchestrator = MulticaTaskOrchestrator(
        Settings(
            multica_http_enabled=True,
            multica_base_url="https://multica.local",
            multica_api_key="test-key",
            multica_max_retries=2,
        ),
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(TaskOrchestratorError, match="multica create_task failed"):
        await orchestrator.create_task(
            TaskRequest(
                source="linear",
                external_id="OS-1284",
                title="Build webhook bridge",
            )
        )

    assert attempts == 1


async def test_multica_adapter_patches_task_status_update() -> None:
    captured_request: httpx.Request | None = None

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal captured_request
        captured_request = request
        return httpx.Response(status_code=200, json={"id": "multica-task-1", "status": "pr_open"})

    orchestrator = MulticaTaskOrchestrator(
        Settings(
            multica_http_enabled=True,
            multica_base_url="https://multica.local",
            multica_api_key="test-key",
        ),
        transport=httpx.MockTransport(handler),
    )

    response = await orchestrator.update_task(
        TaskUpdateRequest(
            external_task_id="multica-task-1",
            status="pr_open",
            metadata={"pull_request": 17},
        )
    )

    assert response.external_task_id == "multica-task-1"
    assert response.status == "pr_open"
    assert captured_request is not None
    assert captured_request.method == "PATCH"
    assert str(captured_request.url) == "https://multica.local/api/tasks/multica-task-1"
    assert json.loads(captured_request.content) == {
        "status": "pr_open",
        "metadata": {"pull_request": 17},
    }
