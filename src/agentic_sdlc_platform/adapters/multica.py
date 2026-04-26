import asyncio
from collections.abc import Awaitable, Callable

import httpx

from agentic_sdlc_platform.core.config import Settings
from agentic_sdlc_platform.ports.task_orchestrator import (
    TaskOrchestratorError,
    TaskRequest,
    TaskResponse,
    TaskUpdateRequest,
)


class MulticaTaskOrchestrator:
    provider = "multica"

    def __init__(
        self,
        settings: Settings,
        transport: httpx.AsyncBaseTransport | httpx.BaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._settings = settings
        self._transport = transport
        self._sleep = sleep

    async def create_task(self, request: TaskRequest) -> TaskResponse:
        payload = {
            "source": request.source,
            "external_id": request.external_id,
            "title": request.title,
            "repo": request.repo,
            "inbound_event_id": request.inbound_event_id,
        }
        response = await self._request_with_retries(
            failure_message="multica create_task failed",
            method="POST",
            path="/api/tasks",
            payload=payload,
        )

        response_payload = response.json()
        external_task_id = response_payload.get("id")
        status = response_payload.get("status")
        if not isinstance(external_task_id, str) or not isinstance(status, str):
            raise TaskOrchestratorError("multica create_task returned invalid response")

        return TaskResponse(external_task_id=external_task_id, status=status)

    async def update_task(self, request: TaskUpdateRequest) -> TaskResponse:
        payload = {
            "status": request.status,
            "metadata": request.metadata or {},
        }
        response = await self._request_with_retries(
            failure_message="multica update_task failed",
            method="PATCH",
            path=f"/api/tasks/{request.external_task_id}",
            payload=payload,
        )

        response_payload = response.json()
        external_task_id = response_payload.get("id")
        status = response_payload.get("status")
        if not isinstance(external_task_id, str) or not isinstance(status, str):
            raise TaskOrchestratorError("multica update_task returned invalid response")

        return TaskResponse(external_task_id=external_task_id, status=status)

    async def _request_with_retries(
        self,
        failure_message: str,
        method: str,
        path: str,
        payload: dict[str, object | None],
    ) -> httpx.Response:
        if not self._settings.multica_http_enabled:
            raise TaskOrchestratorError("multica HTTP is disabled")
        if not self._settings.multica_base_url:
            raise TaskOrchestratorError("multica base URL is not configured")
        if not self._settings.multica_api_key:
            raise TaskOrchestratorError("multica API key is not configured")

        attempts = self._settings.multica_max_retries + 1
        try:
            async with httpx.AsyncClient(
                base_url=self._settings.multica_base_url,
                timeout=self._settings.multica_timeout_seconds,
                transport=self._transport,
            ) as client:
                for attempt in range(attempts):
                    response = await client.request(
                        method,
                        path,
                        json=payload,
                        headers={"Authorization": f"Bearer {self._settings.multica_api_key}"},
                    )
                    if response.status_code < 500:
                        response.raise_for_status()
                        return response
                    if attempt == attempts - 1:
                        response.raise_for_status()
                    await self._sleep(
                        self._settings.multica_retry_backoff_seconds * (2**attempt)
                    )
        except httpx.HTTPError as exc:
            raise TaskOrchestratorError(failure_message) from exc
        raise TaskOrchestratorError(failure_message)
