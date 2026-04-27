import httpx

from agentic_sdlc_platform.core.config import Settings
from agentic_sdlc_platform.ports.hermes_session import (
    HermesSessionError,
    HermesSessionRequest,
    HermesSessionResponse,
    HermesStartSessionRequest,
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
        payload = {
            "provider": request.provider,
            "channel": request.channel,
            "sender_id": request.sender_id,
            "text": request.text,
            "repo": request.repo,
            **_agent_policy_payload(),
        }
        response = await self._post(
            path="/api/sessions/ask",
            payload=payload,
            failure_message="hermes ask failed",
        )
        return self._session_response(
            response,
            failure_message="hermes ask returned invalid response",
        )

    async def start_session(self, request: HermesStartSessionRequest) -> HermesSessionResponse:
        payload = {
            "task_id": request.task_id,
            "provider": request.provider,
            "external_thread_id": request.external_thread_id,
            "text": request.text,
            "repo": request.repo,
            **_agent_policy_payload(),
        }
        response = await self._post(
            path="/api/sessions",
            payload=payload,
            failure_message="hermes start_session failed",
        )
        return self._session_response(
            response,
            failure_message="hermes start_session returned invalid response",
        )

    async def resume_session(
        self,
        session_id: str,
        text: str,
        actor: str,
    ) -> HermesSessionResponse:
        response = await self._post(
            path=f"/api/sessions/{session_id}/messages",
            payload={"text": text, "actor": actor, **_agent_policy_payload()},
            failure_message="hermes resume_session failed",
        )
        return self._session_response(
            response,
            failure_message="hermes resume_session returned invalid response",
        )

    async def _post(
        self,
        path: str,
        payload: dict[str, object | None],
        failure_message: str,
    ) -> dict[str, object]:
        if not self._settings.hermes_http_enabled:
            raise HermesSessionError("hermes HTTP is disabled")
        if not self._settings.hermes_base_url:
            raise HermesSessionError("hermes base URL is not configured")
        if not self._settings.hermes_api_key:
            raise HermesSessionError("hermes API key is not configured")

        try:
            async with httpx.AsyncClient(
                base_url=self._settings.hermes_base_url,
                timeout=self._settings.hermes_timeout_seconds,
                transport=self._transport,
            ) as client:
                response = await client.post(
                    path,
                    json=payload,
                    headers={"Authorization": f"Bearer {self._settings.hermes_api_key}"},
                )
                response.raise_for_status()
        except httpx.HTTPError as exc:
            raise HermesSessionError(failure_message) from exc

        return response.json()

    def _session_response(
        self,
        response_payload: dict[str, object],
        failure_message: str,
    ) -> HermesSessionResponse:
        session_id = response_payload.get("session_id")
        message_id = response_payload.get("message_id")
        answer = response_payload.get("answer")
        if not isinstance(session_id, str) or not isinstance(message_id, str):
            raise HermesSessionError(failure_message)

        return HermesSessionResponse(
            session_id=session_id,
            message_id=message_id,
            answer=answer if isinstance(answer, str) else None,
        )


def _agent_policy_payload() -> dict[str, object]:
    return {
        "runtime_policy": {
            "shell_command_prefix": "rtk",
            "use_rtk_for_terminal_commands": True,
        },
        "repo_context_policy": {
            "preferred_context_source": "graphify",
            "verify_graph_context_against_source": True,
            "avoid_repeated_broad_scans_when_indexed_context_is_available": True,
        },
    }
