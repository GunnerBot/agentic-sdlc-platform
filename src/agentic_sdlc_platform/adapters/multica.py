import httpx

from agentic_sdlc_platform.core.config import Settings
from agentic_sdlc_platform.ports.task_orchestrator import (
    TaskOrchestratorError,
    TaskRequest,
    TaskResponse,
)


class MulticaTaskOrchestrator:
    provider = "multica"

    def __init__(
        self,
        settings: Settings,
        transport: httpx.AsyncBaseTransport | httpx.BaseTransport | None = None,
    ) -> None:
        self._settings = settings
        self._transport = transport

    async def create_task(self, request: TaskRequest) -> TaskResponse:
        if not self._settings.multica_http_enabled:
            raise TaskOrchestratorError("multica HTTP is disabled")
        if not self._settings.multica_base_url:
            raise TaskOrchestratorError("multica base URL is not configured")
        if not self._settings.multica_api_key:
            raise TaskOrchestratorError("multica API key is not configured")

        payload = {
            "source": request.source,
            "external_id": request.external_id,
            "title": request.title,
            "repo": request.repo,
            "inbound_event_id": request.inbound_event_id,
        }
        try:
            async with httpx.AsyncClient(
                base_url=self._settings.multica_base_url,
                timeout=self._settings.multica_timeout_seconds,
                transport=self._transport,
            ) as client:
                response = await client.post(
                    "/api/tasks",
                    json=payload,
                    headers={"Authorization": f"Bearer {self._settings.multica_api_key}"},
                )
                response.raise_for_status()
        except httpx.HTTPError as exc:
            raise TaskOrchestratorError("multica create_task failed") from exc

        response_payload = response.json()
        external_task_id = response_payload.get("id")
        status = response_payload.get("status")
        if not isinstance(external_task_id, str) or not isinstance(status, str):
            raise TaskOrchestratorError("multica create_task returned invalid response")

        return TaskResponse(external_task_id=external_task_id, status=status)
