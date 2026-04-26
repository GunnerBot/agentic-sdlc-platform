import httpx

from agentic_sdlc_platform.core.config import Settings
from agentic_sdlc_platform.ports.hermes_session import (
    HermesSessionError,
    HermesSessionRequest,
    HermesSessionResponse,
)


class HermesAgentAdapter:
    provider = "hermes"

    def __init__(
        self,
        settings: Settings,
        transport: httpx.AsyncBaseTransport | httpx.BaseTransport | None = None,
    ) -> None:
        self._settings = settings
        self._transport = transport

    async def ask(self, request: HermesSessionRequest) -> HermesSessionResponse:
        if not self._settings.hermes_http_enabled:
            raise HermesSessionError("hermes HTTP is disabled")
        if not self._settings.hermes_base_url:
            raise HermesSessionError("hermes base URL is not configured")
        if not self._settings.hermes_api_key:
            raise HermesSessionError("hermes API key is not configured")

        payload = {
            "provider": request.provider,
            "channel": request.channel,
            "sender_id": request.sender_id,
            "text": request.text,
            "repo": request.repo,
        }
        try:
            async with httpx.AsyncClient(
                base_url=self._settings.hermes_base_url,
                timeout=self._settings.hermes_timeout_seconds,
                transport=self._transport,
            ) as client:
                response = await client.post(
                    "/api/sessions/ask",
                    json=payload,
                    headers={"Authorization": f"Bearer {self._settings.hermes_api_key}"},
                )
                response.raise_for_status()
        except httpx.HTTPError as exc:
            raise HermesSessionError("hermes ask failed") from exc

        response_payload = response.json()
        session_id = response_payload.get("session_id")
        message_id = response_payload.get("message_id")
        answer = response_payload.get("answer")
        if not isinstance(session_id, str) or not isinstance(message_id, str):
            raise HermesSessionError("hermes ask returned invalid response")

        return HermesSessionResponse(
            session_id=session_id,
            message_id=message_id,
            answer=answer if isinstance(answer, str) else None,
        )
